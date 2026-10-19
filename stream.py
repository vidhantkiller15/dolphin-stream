"""Lightweight Dolphin window capture → JPEG/UDP stream (low RAM).

Targets ~20 FPS at ~960px wide, quality ~40, single-frame buffers.
Avoids heavy media stacks; uses mss + Pillow only.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Callable, Optional, Tuple

from .protocol import MAX_UDP, pack_video_chunk

# Defaults tuned for <2 GB system RAM usage
DEFAULT_MAX_WIDTH = 960
DEFAULT_JPEG_QUALITY = 40
DEFAULT_FPS = 20
CHUNK_PAYLOAD = MAX_UDP - 32


def find_dolphin_window_rect() -> Optional[Tuple[int, int, int, int]]:
    """Return (left, top, width, height) of Dolphin render window on Windows."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found: list[Tuple[int, int, int, int]] = []

        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            # Prefer game window / Dolphin
            if "dolphin" in title.lower() or "| " in title:
                rect = wintypes.RECT()
                user32.GetClientRect(hwnd, ctypes.byref(rect))
                pt = wintypes.POINT(0, 0)
                user32.ClientToScreen(hwnd, ctypes.byref(pt))
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w >= 160 and h >= 120:
                    found.append((pt.x, pt.y, w, h))
            return True

        user32.EnumWindows(EnumWindowsProc(callback), 0)
        if not found:
            return None
        # Largest visible Dolphin-ish window
        found.sort(key=lambda r: r[2] * r[3], reverse=True)
        return found[0]
    except Exception:
        return None


class StreamServer:
    """Capture Dolphin (or fallback region) and send JPEG chunks to paired TV."""

    def __init__(
        self,
        max_width: int = DEFAULT_MAX_WIDTH,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
        fps: int = DEFAULT_FPS,
    ) -> None:
        self.max_width = max_width
        self.jpeg_quality = jpeg_quality
        self.fps = max(5, min(30, fps))
        self._target: Optional[tuple[str, int]] = None
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._frame_id = 0
        self.on_log: Callable[[str], None] = lambda m: None
        self.frames_sent = 0
        self.running = False

    def set_target(self, ip: str, port: int) -> None:
        self._target = (ip, port)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self._target:
            raise RuntimeError("No TV target set")
        self._stop.clear()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Larger send buffer but not huge
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
        except OSError:
            pass
        self._thread = threading.Thread(target=self._loop, name="dest-stream", daemon=True)
        self._thread.start()
        self.running = True
        self.on_log(f"Stream → {self._target[0]}:{self._target[1]} @ {self.fps}fps q{self.jpeg_quality}")

    def stop(self) -> None:
        self._stop.set()
        self.running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self.on_log("Stream stopped")

    def _loop(self) -> None:
        try:
            import mss
            from PIL import Image
        except ImportError as e:
            self.on_log(f"Stream deps missing: {e} (pip install mss Pillow)")
            self.running = False
            return

        period = 1.0 / self.fps
        with mss.mss() as sct:
            while not self._stop.is_set():
                t0 = time.perf_counter()
                try:
                    self._send_one(sct, Image)
                except Exception as e:
                    self.on_log(f"Stream frame error: {e}")
                    time.sleep(0.5)
                elapsed = time.perf_counter() - t0
                time.sleep(max(0.0, period - elapsed))

    def _send_one(self, sct, Image) -> None:
        assert self._sock and self._target
        rect = find_dolphin_window_rect()
        if rect:
            left, top, w, h = rect
            mon = {"left": left, "top": top, "width": w, "height": h}
        else:
            # Fallback: primary monitor center crop (dev)
            mon0 = sct.monitors[1]
            w = min(1280, mon0["width"])
            h = min(720, mon0["height"])
            mon = {
                "left": mon0["left"] + (mon0["width"] - w) // 2,
                "top": mon0["top"] + (mon0["height"] - h) // 2,
                "width": w,
                "height": h,
            }

        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        # Downscale for RAM/bandwidth
        if img.width > self.max_width:
            ratio = self.max_width / img.width
            nh = max(1, int(img.height * ratio))
            img = img.resize((self.max_width, nh), Image.BILINEAR)

        import io

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self.jpeg_quality, optimize=False)
        jpeg = buf.getvalue()
        # Free references ASAP
        del img, shot, buf

        self._frame_id = (self._frame_id + 1) & 0xFFFFFFFF
        chunks = []
        for i in range(0, len(jpeg), CHUNK_PAYLOAD):
            chunks.append(jpeg[i : i + CHUNK_PAYLOAD])
        if not chunks:
            return
        count = len(chunks)
        w, h = mon["width"], mon["height"]
        # Use scaled size if we resized — approximate from last known
        # (TV only needs aspect; uses full JPEG dimensions internally)
        for idx, payload in enumerate(chunks):
            pkt = pack_video_chunk(self._frame_id, idx, count, w, h, payload)
            self._sock.sendto(pkt, self._target)
        self.frames_sent += 1


class StreamReceiver:
    """Reassemble JPEG frames from UDP for the TV app."""

    def __init__(self, bind_port: int) -> None:
        self.bind_port = bind_port
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._partial: dict[int, dict] = {}
        self.latest_jpeg: Optional[bytes] = None
        self.latest_size: Tuple[int, int] = (0, 0)
        self.frames_recv = 0
        self.on_frame: Callable[[], None] = lambda: None
        self.on_log: Callable[[str], None] = lambda m: None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512 * 1024)
        except OSError:
            pass
        self._sock.bind(("0.0.0.0", self.bind_port))
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(target=self._loop, name="dest-recv-vid", daemon=True)
        self._thread.start()
        self.on_log(f"TV stream listening UDP {self.bind_port}")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def pop_jpeg(self) -> Optional[bytes]:
        with self._lock:
            data = self.latest_jpeg
            self.latest_jpeg = None
            return data

    def _loop(self) -> None:
        from .protocol import unpack_video_chunk

        assert self._sock
        while not self._stop.is_set():
            try:
                data, _addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            parsed = unpack_video_chunk(data)
            if not parsed:
                continue
            fid, cidx, ccnt, w, h, payload = parsed
            bucket = self._partial.get(fid)
            if bucket is None:
                # Keep only a couple partial frames to limit RAM
                if len(self._partial) > 2:
                    oldest = min(self._partial.keys())
                    self._partial.pop(oldest, None)
                bucket = {"count": ccnt, "parts": {}, "w": w, "h": h}
                self._partial[fid] = bucket
            bucket["parts"][cidx] = payload
            if len(bucket["parts"]) >= bucket["count"]:
                ordered = b"".join(bucket["parts"][i] for i in range(bucket["count"]))
                self._partial.pop(fid, None)
                with self._lock:
                    self.latest_jpeg = ordered
                    self.latest_size = (w, h)
                self.frames_recv += 1
                self.on_frame()
