"""TV Screen app — pair with PC via PIN, show lightweight Dolphin stream."""

from __future__ import annotations

import io
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
    DEFAULT_DISCOVERY_PORT,
    DEFAULT_STREAM_PORT,
    DEFAULT_TV_PORT,
    ROLE_TV,
    DeviceAnnounce,
    local_ip,
    new_device_id,
    pack_announce,
    pack_pair,
    unpack_announce,
    unpack_pair_ack,
)
from dolphinestreme.stream import StreamReceiver

BG = "#0d0d0d"
PANEL = "#1c1c1c"
FG = "#d0d0d0"
MUTED = "#777777"
ACCENT = "#6dce8a"
FONT = ("Consolas", 12)
FONT_B = ("Consolas", 14, "bold")


class TvApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Dolphinestreme TV · DEV")
        self.geometry("960x600")
        self.configure(bg=BG)
        self.option_add("*Font", FONT)

        self.device_id = new_device_id()
        self.tv_name = socket.gethostname() + "-TV"
        self.control_port = DEFAULT_TV_PORT
        self.stream_port = DEFAULT_STREAM_PORT
        self.paired = False
        self.pc_addr: Optional[str] = None
        self._announce_stop = threading.Event()
        self._announce_thread: Optional[threading.Thread] = None
        self._pcs: List[DeviceAnnounce] = []
        self._photo = None

        self.receiver = StreamReceiver(self.stream_port)
        self.receiver.on_log = lambda m: self.after(0, lambda: self._status(m))
        self.receiver.on_frame = lambda: self.after(0, self._show_frame)

        self._build()
        self._start_announce()
        try:
            self.receiver.start()
        except OSError as e:
            self._status(f"Stream bind failed: {e}")

        self.after(200, self._ui_tick)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self) -> None:
        self.setup = tk.Frame(self, bg=BG, padx=24, pady=24)
        self.setup.pack(fill=tk.BOTH, expand=True)

        tk.Label(self.setup, text="DOLPHINESTREME  TV  ·  DEV", bg=BG, fg=ACCENT, font=FONT_B).pack(
            anchor=tk.W
        )
        tk.Label(
            self.setup,
            text="This machine is the screen. Pair to the PC console with the PIN shown on the PC.",
            bg=BG,
            fg=MUTED,
        ).pack(anchor=tk.W, pady=(4, 16))

        self.status_var = tk.StringVar(value=f"Advertising as {self.tv_name}  stream :{self.stream_port}")
        tk.Label(self.setup, textvariable=self.status_var, bg=BG, fg=FG).pack(anchor=tk.W)

        tk.Button(
            self.setup,
            text="Find PCs on network",
            command=self._scan_pcs,
            bg=PANEL,
            fg=FG,
            relief=tk.FLAT,
            padx=10,
            pady=6,
        ).pack(anchor=tk.W, pady=10)

        self.pc_list = tk.Listbox(self.setup, bg=PANEL, fg=FG, height=5, relief=tk.FLAT, highlightthickness=0)
        self.pc_list.pack(fill=tk.X)

        row = tk.Frame(self.setup, bg=BG)
        row.pack(fill=tk.X, pady=12)
        tk.Label(row, text="PIN from PC:", bg=BG, fg=MUTED).pack(side=tk.LEFT)
        self.pin_entry = tk.Entry(row, bg=PANEL, fg=ACCENT, insertbackground=FG, relief=tk.FLAT, width=10, font=FONT_B)
        self.pin_entry.pack(side=tk.LEFT, padx=8)

        tk.Button(
            self.setup,
            text="PAIR TO PC",
            command=self._pair,
            bg=ACCENT,
            fg="#111",
            relief=tk.FLAT,
            font=FONT_B,
            padx=16,
            pady=10,
        ).pack(anchor=tk.W, pady=8)

        tk.Button(
            self.setup,
            text="Fullscreen stream view",
            command=self._go_stream_view,
            bg=PANEL,
            fg=FG,
            relief=tk.FLAT,
        ).pack(anchor=tk.W, pady=4)

        # Stream view (hidden until pair / manual)
        self.view = tk.Frame(self, bg="black")
        self.canvas = tk.Label(self.view, bg="black")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.view_info = tk.Label(self.view, text="Waiting for stream…", bg="black", fg=MUTED, font=FONT)
        self.view_info.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

        self.bind("<Escape>", lambda e: self._leave_fullscreen())
        self.bind("<F11>", lambda e: self._toggle_fs())

    def _start_announce(self) -> None:
        self._announce_stop.clear()
        self._announce_thread = threading.Thread(target=self._announce_loop, daemon=True)
        self._announce_thread.start()

    def _announce_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        payload = pack_announce(ROLE_TV, self.tv_name, self.control_port, self.stream_port, self.device_id)
        while not self._announce_stop.is_set():
            try:
                sock.sendto(payload, ("255.255.255.255", DEFAULT_DISCOVERY_PORT))
            except OSError:
                pass
            self._announce_stop.wait(1.0)
        sock.close()

    def _scan_pcs(self) -> None:
        self._status("Scanning for PC consoles…")
        found: dict[str, DeviceAnnounce] = {}
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", DEFAULT_DISCOVERY_PORT))
        except OSError:
            sock.bind(("0.0.0.0", 0))
        sock.settimeout(0.3)
        end = time.time() + 2.0
        from dolphinestreme.protocol import ROLE_HUB

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
        self._pcs = list(found.values())
        self.pc_list.delete(0, tk.END)
        for p in self._pcs:
            self.pc_list.insert(tk.END, f"{p.name}  {p.address}:{p.control_port}")
        self._status(f"Found {len(self._pcs)} PC(s)" if self._pcs else "No PC found — start_pc.bat first")

    def _pair(self) -> None:
        pin = self.pin_entry.get().strip()
        if len(pin) < 4:
            self._status("Enter the 6-digit PIN from the PC")
            return
        sel = self.pc_list.curselection()
        if sel:
            pc = self._pcs[sel[0]]
            ip, port = pc.address, pc.control_port
        elif self._pcs:
            pc = self._pcs[0]
            ip, port = pc.address, pc.control_port
        else:
            # Manual: try local scan default
            self._scan_pcs()
            if not self._pcs:
                self._status("Find PCs first")
                return
            pc = self._pcs[0]
            ip, port = pc.address, pc.control_port

        pkt = pack_pair(ROLE_TV, pin, self.device_id, self.tv_name, stream_port=self.stream_port)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(3.0)
            sock.sendto(pkt, (ip, port))
            data, _ = sock.recvfrom(256)
            sock.close()
            ack = unpack_pair_ack(data)
            if ack and ack[0]:
                self.paired = True
                self.pc_addr = ip
                self._status(f"PAIRED to {ip} — {ack[1]}")
                self._go_stream_view()
            else:
                msg = ack[1] if ack else "no ack"
                self._status(f"Pair failed: {msg}")
        except Exception as e:
            self._status(f"Pair error: {e}")

    def _go_stream_view(self) -> None:
        self.setup.pack_forget()
        self.view.pack(fill=tk.BOTH, expand=True)
        self.view_info.configure(text="Waiting for PC to press START…\nEsc = setup  F11 = fullscreen")

    def _leave_fullscreen(self) -> None:
        self.attributes("-fullscreen", False)
        self.view.pack_forget()
        self.setup.pack(fill=tk.BOTH, expand=True)

    def _toggle_fs(self) -> None:
        self.attributes("-fullscreen", not self.attributes("-fullscreen"))

    def _show_frame(self) -> None:
        jpeg = self.receiver.pop_jpeg()
        if not jpeg:
            return
        try:
            from PIL import Image, ImageTk

            img = Image.open(io.BytesIO(jpeg))
            # Fit window without huge copies
            cw = max(self.canvas.winfo_width(), 320)
            ch = max(self.canvas.winfo_height(), 240)
            img.thumbnail((cw, ch), Image.BILINEAR)
            self._photo = ImageTk.PhotoImage(img)
            self.canvas.configure(image=self._photo)
            self.view_info.place_forget()
            del img
        except Exception:
            pass

    def _ui_tick(self) -> None:
        if self.receiver.frames_recv and self.view.winfo_ismapped():
            self.view_info.place_forget()
        self.after(500, self._ui_tick)

    def _status(self, msg: str) -> None:
        self.status_var.set(msg)

    def _close(self) -> None:
        self._announce_stop.set()
        self.receiver.stop()
        self.destroy()


def main() -> None:
    TvApp().mainloop()


if __name__ == "__main__":
    main()
