"""Phone/PC controller client — PIN + QR pairing, virtual Wiimote."""

from __future__ import annotations

import socket
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from typing import List, Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dolphinestreme.protocol import (
    BTN_A,
    BTN_B,
    BTN_DPAD_DOWN,
    BTN_DPAD_LEFT,
    BTN_DPAD_RIGHT,
    BTN_DPAD_UP,
    BTN_HOME,
    BTN_MINUS,
    BTN_ONE,
    BTN_PLUS,
    BTN_RECENTER,
    BTN_TWO,
    DEFAULT_DISCOVERY_PORT,
    ROLE_HUB,
    ROLE_PHONE,
    ControllerFrame,
    ConnectInfo,
    DeviceAnnounce,
    new_device_id,
    pack_frame,
    pack_hello,
    pack_pair,
    parse_qr_uri,
    unpack_announce,
    unpack_hello_ack,
    unpack_pair_ack,
)

BG = "#1a1a1a"
PANEL = "#242424"
FG = "#d0d0d0"
MUTED = "#888"
ACCENT = "#6dce8a"


class ControllerClient:
    def __init__(self, device_name: Optional[str] = None) -> None:
        self.device_name = device_name or socket.gethostname()
        self.device_id = new_device_id()
        self.frame = ControllerFrame()
        self.player_id = 0
        self.connected = False
        self.host_label = ""
        self.last_error: Optional[str] = None
        self._sock: Optional[socket.socket] = None
        self._addr: Optional[tuple[str, int]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seq = 0
        self._lock = threading.Lock()

    def connect(self, info: ConnectInfo) -> bool:
        self.disconnect()
        try:
            token = bytes.fromhex(info.token_hex) if info.token_hex else b"\0" * 16
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(2.5)
            # Prefer pair with PIN, then hello
            if info.pin:
                pair = pack_pair(ROLE_PHONE, info.pin, self.device_id, self.device_name, stream_port=0)
                sock.sendto(pair, (info.ip, info.port))
                try:
                    data, _ = sock.recvfrom(128)
                    ack = unpack_pair_ack(data)
                    if ack and not ack[0]:
                        raise RuntimeError(ack[1] or "Pair rejected")
                    # May also get hello-ack
                    if data[5:6] == bytes([2]) or (ack and ack[0]):
                        pass
                except socket.timeout:
                    pass

            hello = pack_hello(self.device_id, token, self.device_name, info.pin or "")
            sock.sendto(hello, (info.ip, info.port))
            data, _ = sock.recvfrom(64)
            # Could be pair_ack or hello_ack
            ha = unpack_hello_ack(data)
            if not ha:
                # try once more
                sock.sendto(hello, (info.ip, info.port))
                data, _ = sock.recvfrom(64)
                ha = unpack_hello_ack(data)
            if not ha:
                raise RuntimeError("No HelloAck — check PIN")
            self.player_id = ha.player_id
            self.frame.player_id = self.player_id
            self.host_label = info.name
            self._sock = sock
            self._addr = (info.ip, info.port)
            self.connected = True
            self.last_error = None
            self._stop.clear()
            self._thread = threading.Thread(target=self._stream_loop, daemon=True)
            self._thread.start()
            return True
        except Exception as e:
            self.last_error = str(e)
            self.connected = False
            return False

    def disconnect(self) -> None:
        self._stop.set()
        self.connected = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def set_button(self, mask: int, down: bool) -> None:
        with self._lock:
            if down:
                self.frame.buttons |= mask
            else:
                self.frame.buttons &= ~mask

    def set_tilt(self, pitch: float, yaw: float) -> None:
        import math

        half_y = yaw * 0.35 * 0.5
        half_p = pitch * 0.35 * 0.5
        cy, sy = math.cos(half_y), math.sin(half_y)
        cp, sp = math.cos(half_p), math.sin(half_p)
        with self._lock:
            self.frame.qw = cy * cp
            self.frame.qx = cy * sp
            self.frame.qy = sy * cp
            self.frame.qz = -sy * sp
            self.frame.gx = pitch * 0.5
            self.frame.gy = yaw * 0.5

    def _stream_loop(self) -> None:
        while not self._stop.is_set() and self.connected and self._sock and self._addr:
            with self._lock:
                self._seq += 1
                self.frame.sequence = self._seq
                self.frame.client_timestamp_us = time.time_ns() // 1000
                self.frame.player_id = self.player_id
                payload = pack_frame(self.frame)
            try:
                self._sock.sendto(payload, self._addr)
            except OSError as e:
                self.last_error = str(e)
                self.connected = False
                break
            time.sleep(0.01)  # 100 Hz — lighter than 125


def discover_hubs(timeout: float = 2.0) -> List[DeviceAnnounce]:
    found: dict[str, DeviceAnnounce] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", DEFAULT_DISCOVERY_PORT))
    except OSError:
        sock.bind(("0.0.0.0", 0))
    sock.settimeout(0.3)
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, addr = sock.recvfrom(256)
            ann = unpack_announce(data, address=addr[0])
            if ann and ann.role == ROLE_HUB:
                found[f"{addr[0]}:{ann.control_port}"] = ann
        except socket.timeout:
            continue
        except OSError:
            break
    sock.close()
    return list(found.values())


class ClientApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Dolphinestreme Controller · DEV")
        self.geometry("400x700")
        self.configure(bg=BG)
        self.client = ControllerClient()
        self.hubs: List[DeviceAnnounce] = []
        self.status_var = tk.StringVar(value="Not connected")
        self.uri_var = tk.StringVar()
        self.pin_var = tk.StringVar()
        self._build_connect()
        self._build_controller()
        self.show_connect()
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_connect(self) -> None:
        self.cf = tk.Frame(self, bg=BG, padx=16, pady=16)
        tk.Label(self.cf, text="CONTROLLER  ·  DEV", bg=BG, fg=ACCENT, font=("Consolas", 14, "bold")).pack(
            anchor=tk.W
        )
        tk.Label(self.cf, textvariable=self.status_var, bg=BG, fg=FG).pack(anchor=tk.W, pady=8)
        tk.Button(self.cf, text="Discover PCs", command=self._discover, bg=PANEL, fg=FG, relief=tk.FLAT).pack(
            fill=tk.X, pady=4
        )
        self.host_list = tk.Listbox(self.cf, bg=PANEL, fg=FG, height=4, relief=tk.FLAT, highlightthickness=0)
        self.host_list.pack(fill=tk.X)
        tk.Label(self.cf, text="PIN (from PC):", bg=BG, fg=MUTED).pack(anchor=tk.W, pady=(12, 2))
        tk.Entry(self.cf, textvariable=self.pin_var, bg=PANEL, fg=ACCENT, relief=tk.FLAT, font=("Consolas", 16)).pack(
            fill=tk.X
        )
        tk.Button(self.cf, text="Connect selected + PIN", command=self._connect_sel, bg=ACCENT, fg="#111", relief=tk.FLAT).pack(
            fill=tk.X, pady=8
        )
        tk.Label(self.cf, text="Or paste QR URI:", bg=BG, fg=MUTED).pack(anchor=tk.W, pady=(8, 2))
        tk.Entry(self.cf, textvariable=self.uri_var, bg=PANEL, fg=FG, relief=tk.FLAT).pack(fill=tk.X)
        tk.Button(self.cf, text="Connect via URI", command=self._connect_uri, bg=PANEL, fg=FG, relief=tk.FLAT).pack(
            fill=tk.X, pady=6
        )

    def _build_controller(self) -> None:
        self.pf = tk.Frame(self, bg=BG, padx=12, pady=12)
        top = tk.Frame(self.pf, bg=BG)
        top.pack(fill=tk.X)
        self.plabel = tk.StringVar(value="P?")
        tk.Label(top, textvariable=self.plabel, bg=BG, fg=ACCENT, font=("Consolas", 12, "bold")).pack(side=tk.LEFT)
        tk.Button(top, text="Leave", command=self._leave, bg=PANEL, fg=FG, relief=tk.FLAT).pack(side=tk.RIGHT)

        dpad = tk.Frame(self.pf, bg=BG)
        dpad.pack(pady=10)
        self._btn(dpad, "▲", BTN_DPAD_UP).grid(row=0, column=1)
        self._btn(dpad, "◀", BTN_DPAD_LEFT).grid(row=1, column=0)
        self._btn(dpad, "▶", BTN_DPAD_RIGHT).grid(row=1, column=2)
        self._btn(dpad, "▼", BTN_DPAD_DOWN).grid(row=2, column=1)

        face = tk.Frame(self.pf, bg=BG)
        face.pack(pady=12)
        self._btn(face, "B", BTN_B, "#a05050", 8).pack(side=tk.LEFT, padx=10)
        self._btn(face, "A", BTN_A, ACCENT, 10).pack(side=tk.LEFT, padx=10)

        row = tk.Frame(self.pf, bg=BG)
        row.pack()
        for t, m in (("1", BTN_ONE), ("2", BTN_TWO), ("+", BTN_PLUS), ("-", BTN_MINUS), ("H", BTN_HOME), ("◎", BTN_RECENTER)):
            self._btn(row, t, m, PANEL, 4).pack(side=tk.LEFT, padx=3)

        tk.Label(self.pf, text="tilt yaw / pitch", bg=BG, fg=MUTED).pack(pady=(16, 2))
        self.yaw = tk.Scale(self.pf, from_=-1, to=1, resolution=0.01, orient=tk.HORIZONTAL, bg=PANEL, fg=FG, highlightthickness=0, command=self._tilt)
        self.yaw.pack(fill=tk.X)
        self.pitch = tk.Scale(self.pf, from_=-1, to=1, resolution=0.01, orient=tk.HORIZONTAL, bg=PANEL, fg=FG, highlightthickness=0, command=self._tilt)
        self.pitch.pack(fill=tk.X)

    def _btn(self, parent, text, mask, color=PANEL, w=5):
        b = tk.Button(parent, text=text, bg=color, fg="#111" if color == ACCENT else FG, relief=tk.FLAT, width=w)
        b.bind("<ButtonPress-1>", lambda e, m=mask: self.client.set_button(m, True))
        b.bind("<ButtonRelease-1>", lambda e, m=mask: self.client.set_button(m, False))
        return b

    def show_connect(self) -> None:
        self.pf.pack_forget()
        self.cf.pack(fill=tk.BOTH, expand=True)

    def show_pad(self) -> None:
        self.cf.pack_forget()
        self.pf.pack(fill=tk.BOTH, expand=True)
        self.plabel.set(f"P{self.client.player_id + 1} → {self.client.host_label}")

    def _discover(self) -> None:
        self.hubs = discover_hubs()
        self.host_list.delete(0, tk.END)
        for h in self.hubs:
            self.host_list.insert(tk.END, f"{h.name}  {h.address}")
        self.status_var.set(f"{len(self.hubs)} PC(s)" if self.hubs else "None — start PC app")

    def _connect_sel(self) -> None:
        sel = self.host_list.curselection()
        if not sel or not self.hubs:
            self.status_var.set("Select a PC")
            return
        h = self.hubs[sel[0]]
        info = ConnectInfo(
            name=h.name,
            ip=h.address,
            port=h.control_port,
            token_hex=h.token.hex(),
            pin=self.pin_var.get().strip(),
        )
        self._go(info)

    def _connect_uri(self) -> None:
        info = parse_qr_uri(self.uri_var.get().strip())
        if not info:
            self.status_var.set("Bad URI")
            return
        if not info.pin:
            info.pin = self.pin_var.get().strip()
        else:
            self.pin_var.set(info.pin)
        self._go(info)

    def _go(self, info: ConnectInfo) -> None:
        self.status_var.set("Connecting…")
        self.update_idletasks()
        if self.client.connect(info):
            self.show_pad()
        else:
            self.status_var.set(self.client.last_error or "fail")

    def _tilt(self, _=None) -> None:
        self.client.set_tilt(float(self.pitch.get()), float(self.yaw.get()))

    def _leave(self) -> None:
        self.client.disconnect()
        self.status_var.set("Disconnected")
        self.show_connect()

    def _close(self) -> None:
        self.client.disconnect()
        self.destroy()


def main() -> None:
    ClientApp().mainloop()


if __name__ == "__main__":
    main()
