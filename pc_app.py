"""PC Console app — pair TV + phones, pick game, stream Dolphin (DEV UI)."""

from __future__ import annotations

import io
import socket
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import List, Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dolphinestreme.dolphin_bridge import (
    apply_wiimote_profile,
    launch_dolphin,
    list_games,
    locate_dolphin,
)
from dolphinestreme.input_inject import KeyboardMouseInjector
from dolphinestreme.protocol import local_ip
from dolphinestreme.session import PairedTV, SessionServer
from dolphinestreme.stream import StreamServer

# DEV palette — lightweight flat UI
BG = "#1a1a1a"
PANEL = "#242424"
FG = "#d0d0d0"
MUTED = "#888888"
ACCENT = "#6dce8a"
WARN = "#e0b060"
ERR = "#e07070"
FONT = ("Consolas", 10)
FONT_B = ("Consolas", 11, "bold")
FONT_PIN = ("Consolas", 28, "bold")


class PcConsoleApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Dolphinestreme PC · DEV")
        self.geometry("880x640")
        self.minsize(760, 560)
        self.configure(bg=BG)
        self.option_add("*Font", FONT)

        self.session = SessionServer(host_name=socket.gethostname() + "-PC")
        self.injector = KeyboardMouseInjector()
        self.stream = StreamServer(max_width=960, jpeg_quality=40, fps=20)
        self.dolphin = locate_dolphin()
        self.games_dir = _ROOT / "games"
        self.games_dir.mkdir(exist_ok=True)
        self.games: List[Path] = []
        self.selected_game: Optional[Path] = None
        self._dolphin_proc: Optional[subprocess.Popen] = None
        self._log_q: list[str] = []
        self._tv_scan: list = []

        self.session.on_log = lambda m: self._log_q.append(m)
        self.stream.on_log = lambda m: self._log_q.append(m)
        self.session.on_player = lambda s: self.after(0, self._refresh_phones)
        self.session.on_tv_paired = lambda t: self.after(0, lambda: self._on_tv_paired(t))
        self.session.on_frame = self._on_frame

        self._build()
        try:
            self.session.start()
            apply_wiimote_profile(self.dolphin.user_dir)
            self._refresh_all()
            self._log("DEV console online. Pair TV → pair phones → pick game → START")
        except OSError as e:
            messagebox.showerror("PC", f"Bind failed: {e}")

        self.after(100, self._poll)
        self.after(400, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ── UI ──────────────────────────────────────────────────────────

    def _build(self) -> None:
        top = tk.Frame(self, bg=BG)
        top.pack(fill=tk.X, padx=10, pady=8)
        tk.Label(top, text="DOLPHINESTREME  PC  ·  DEV BUILD", bg=BG, fg=ACCENT, font=FONT_B).pack(
            side=tk.LEFT
        )
        tk.Label(top, text="lightweight · target <2GB", bg=BG, fg=MUTED).pack(side=tk.RIGHT)

        body = tk.Frame(self, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        # Left column
        left = tk.Frame(body, bg=PANEL, padx=10, pady=10)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        tk.Label(left, text="SESSION PIN  (enter on TV & phones)", bg=PANEL, fg=MUTED).pack(anchor=tk.W)
        pin_row = tk.Frame(left, bg=PANEL)
        pin_row.pack(fill=tk.X, pady=4)
        self.pin_var = tk.StringVar(value=self.session.pin)
        tk.Label(pin_row, textvariable=self.pin_var, bg=PANEL, fg=ACCENT, font=FONT_PIN).pack(side=tk.LEFT)
        tk.Button(pin_row, text="new pin", command=self._new_pin, bg=BG, fg=FG, relief=tk.FLAT).pack(
            side=tk.LEFT, padx=12
        )

        tk.Label(left, text="Phone QR / URI", bg=PANEL, fg=MUTED).pack(anchor=tk.W, pady=(12, 2))
        self.qr_label = tk.Label(left, bg="white")
        self.qr_label.pack(pady=4)
        self.uri_var = tk.StringVar()
        tk.Entry(left, textvariable=self.uri_var, bg=BG, fg=MUTED, relief=tk.FLAT, font=("Consolas", 8)).pack(
            fill=tk.X
        )

        tk.Label(left, text="Phones", bg=PANEL, fg=MUTED).pack(anchor=tk.W, pady=(12, 2))
        self.phone_list = tk.Listbox(left, bg=BG, fg=FG, height=5, relief=tk.FLAT, highlightthickness=0)
        self.phone_list.pack(fill=tk.X)

        # Right column
        right = tk.Frame(body, bg=PANEL, padx=10, pady=10)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5, 0))

        tk.Label(right, text="TV SCREEN", bg=PANEL, fg=MUTED).pack(anchor=tk.W)
        self.tv_status = tk.StringVar(value="No TV paired")
        tk.Label(right, textvariable=self.tv_status, bg=PANEL, fg=WARN).pack(anchor=tk.W)
        tv_btns = tk.Frame(right, bg=PANEL)
        tv_btns.pack(fill=tk.X, pady=6)
        tk.Button(tv_btns, text="Pair TV…", command=self._pair_tv, bg=ACCENT, fg="#111", relief=tk.FLAT).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        tk.Button(tv_btns, text="Unpair", command=self._unpair_tv, bg=BG, fg=FG, relief=tk.FLAT).pack(
            side=tk.LEFT
        )
        self.tv_list = tk.Listbox(right, bg=BG, fg=FG, height=4, relief=tk.FLAT, highlightthickness=0)
        self.tv_list.pack(fill=tk.X)
        tk.Label(
            right,
            text="1) Run start_tv.bat on the TV machine\n2) Pair TV… → select → enter PIN on TV",
            bg=PANEL,
            fg=MUTED,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=4)

        tk.Label(right, text="GAMES", bg=PANEL, fg=MUTED).pack(anchor=tk.W, pady=(12, 2))
        gbtn = tk.Frame(right, bg=PANEL)
        gbtn.pack(fill=tk.X)
        tk.Button(gbtn, text="Games folder…", command=self._pick_games_dir, bg=BG, fg=FG, relief=tk.FLAT).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        tk.Button(gbtn, text="Refresh", command=self._refresh_games, bg=BG, fg=FG, relief=tk.FLAT).pack(
            side=tk.LEFT
        )
        tk.Button(gbtn, text="Add file…", command=self._add_game, bg=BG, fg=FG, relief=tk.FLAT).pack(
            side=tk.LEFT, padx=6
        )
        self.game_list = tk.Listbox(right, bg=BG, fg=FG, height=8, relief=tk.FLAT, highlightthickness=0)
        self.game_list.pack(fill=tk.BOTH, expand=True, pady=4)
        self.game_list.bind("<<ListboxSelect>>", self._on_game_sel)
        self.games_path_var = tk.StringVar(value="(no game selected — opens Dolphin menu)")
        tk.Label(right, textvariable=self.game_path_var, bg=PANEL, fg=MUTED, wraplength=360, justify=tk.LEFT).pack(
            anchor=tk.W
        )

        # Bottom actions
        bot = tk.Frame(self, bg=BG)
        bot.pack(fill=tk.X, padx=10, pady=8)
        self.start_btn = tk.Button(
            bot,
            text="▶  START  (Dolphin + stream to TV)",
            command=self._start,
            bg=ACCENT,
            fg="#111",
            relief=tk.FLAT,
            font=FONT_B,
            padx=12,
            pady=8,
        )
        self.start_btn.pack(side=tk.LEFT)
        tk.Button(bot, text="■ STOP", command=self._stop, bg=ERR, fg="#111", relief=tk.FLAT, padx=12, pady=8).pack(
            side=tk.LEFT, padx=8
        )
        tk.Button(bot, text="Browse Dolphin.exe", command=self._browse_dolphin, bg=PANEL, fg=FG, relief=tk.FLAT).pack(
            side=tk.LEFT
        )

        self.dolphin_var = tk.StringVar()
        tk.Label(self, textvariable=self.dolphin_var, bg=BG, fg=MUTED, anchor=tk.W).pack(fill=tk.X, padx=12)

        self.log = tk.Text(self, height=6, bg="#111", fg=MUTED, relief=tk.FLAT, font=("Consolas", 9))
        self.log.pack(fill=tk.X, padx=10, pady=(0, 10))
        self.log.configure(state=tk.DISABLED)

    # ── actions ─────────────────────────────────────────────────────

    def _new_pin(self) -> None:
        self.session.regenerate_pin()
        self.pin_var.set(self.session.pin)
        self._refresh_qr()
        self._log(f"New PIN: {self.session.pin}")

    def _refresh_qr(self) -> None:
        uri = self.session.qr_uri()
        self.uri_var.set(uri)
        try:
            import qrcode
            from PIL import Image, ImageTk

            img = qrcode.make(uri, box_size=4, border=1)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            pil = Image.open(buf).resize((140, 140))
            self._qr_img = ImageTk.PhotoImage(pil)
            self.qr_label.configure(image=self._qr_img)
        except Exception:
            self.qr_label.configure(text="[QR needs: pip install qrcode Pillow]", fg=MUTED, bg=PANEL)

    def _refresh_phones(self) -> None:
        self.phone_list.delete(0, tk.END)
        for s in self.session.slots:
            if s.connected:
                self.phone_list.insert(tk.END, f"P{s.player_id + 1}  {s.device_name}  ok")
            else:
                self.phone_list.insert(tk.END, f"P{s.player_id + 1}  —")

    def _refresh_games(self) -> None:
        self.games = list_games(self.games_dir)
        self.game_list.delete(0, tk.END)
        for g in self.games:
            self.game_list.insert(tk.END, g.name)
        if not self.games:
            self.game_list.insert(tk.END, "(drop games into /games or Add file…)")

    def _refresh_all(self) -> None:
        self.pin_var.set(self.session.pin)
        self._refresh_qr()
        self._refresh_phones()
        self._refresh_games()
        if self.dolphin.found:
            self.dolphin_var.set(f"Dolphin: {self.dolphin.exe}")
        else:
            self.dolphin_var.set("Dolphin: NOT FOUND — Browse Dolphin.exe")
        if self.session.paired_tv:
            t = self.session.paired_tv
            self.tv_status.set(f"Paired: {t.name} @ {t.address}:{t.stream_port}")

    def _pair_tv(self) -> None:
        self._log("Scanning for TVs on LAN…")
        self.update_idletasks()
        tvs = self.session.scan_tvs(2.2)
        self._tv_scan = tvs
        self.tv_list.delete(0, tk.END)
        if not tvs:
            self.tv_list.insert(tk.END, "No TVs found — run start_tv.bat on the TV PC")
            self._log("No TVs found")
            return
        for ann in tvs:
            self.tv_list.insert(
                tk.END,
                f"{ann.name}  {ann.address}:{ann.stream_port}",
            )
        self._log(f"Found {len(tvs)} TV(s). On the TV app enter PIN {self.session.pin}")
        messagebox.showinfo(
            "Pair TV",
            f"Found {len(tvs)} TV(s).\n\n"
            f"On the TV app:\n"
            f"  1. Select this PC if listed (or it auto-finds)\n"
            f"  2. Enter PIN:  {self.session.pin}\n"
            f"  3. Tap Pair\n\n"
            f"This window will update when paired.",
        )

    def _on_tv_paired(self, tv: PairedTV) -> None:
        self.tv_status.set(f"Paired: {tv.name} @ {tv.address}:{tv.stream_port}")
        self._log(f"TV ready: {tv.address}:{tv.stream_port}")

    def _unpair_tv(self) -> None:
        self.session.paired_tv = None
        self.tv_status.set("No TV paired")
        self._log("TV unpaired")

    def _pick_games_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=str(self.games_dir))
        if d:
            self.games_dir = Path(d)
            self._refresh_games()

    def _add_game(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[
                ("Wii/GC", "*.iso;*.rvz;*.wbfs;*.wia;*.gcm;*.wad"),
                ("All", "*.*"),
            ]
        )
        if path:
            self.selected_game = Path(path)
            self.game_path_var.set(str(self.selected_game))
            self._log(f"Game: {self.selected_game.name}")

    def _on_game_sel(self, _evt=None) -> None:
        sel = self.game_list.curselection()
        if not sel or not self.games:
            return
        idx = sel[0]
        if idx < len(self.games):
            self.selected_game = self.games[idx]
            self.game_path_var.set(str(self.selected_game))

    def _browse_dolphin(self) -> None:
        p = filedialog.askopenfilename(filetypes=[("Dolphin", "Dolphin.exe"), ("All", "*.*")])
        if p:
            self.dolphin = locate_dolphin(p)
            self._refresh_all()

    def _start(self) -> None:
        if not self.session.paired_tv:
            messagebox.showwarning("START", "Pair a TV first.")
            return
        if not self.dolphin.found or not self.dolphin.exe:
            messagebox.showwarning("START", "Dolphin.exe not found.")
            return
        tv = self.session.paired_tv
        try:
            apply_wiimote_profile(self.dolphin.user_dir)
            self._dolphin_proc = launch_dolphin(
                self.dolphin.exe,
                game=self.selected_game,
                user_dir=self.dolphin.user_dir,
            )
            self.stream.set_target(tv.address, tv.stream_port)
            self.stream.start()
            self._log("STARTED — focus Dolphin window for best capture + controls")
            self.start_btn.configure(state=tk.DISABLED)
        except Exception as e:
            messagebox.showerror("START", str(e))

    def _stop(self) -> None:
        try:
            self.stream.stop()
        except Exception:
            pass
        if self._dolphin_proc and self._dolphin_proc.poll() is None:
            try:
                self._dolphin_proc.terminate()
            except Exception:
                pass
        self._dolphin_proc = None
        self.injector.release_all()
        self.start_btn.configure(state=tk.NORMAL)
        self._log("Stopped")

    def _on_frame(self, slot, motion) -> None:
        if slot.player_id == 0 and self.injector.available:
            self.injector.apply(motion)

    def _poll(self) -> None:
        while self._log_q:
            self._append_log(self._log_q.pop(0))
        self.after(100, self._poll)

    def _tick(self) -> None:
        self._refresh_phones()
        extra = ""
        if self.stream.running:
            extra = f"  stream frames={self.stream.frames_sent}"
        if self.session.paired_tv:
            self.tv_status.set(
                f"Paired: {self.session.paired_tv.name} @ "
                f"{self.session.paired_tv.address}:{self.session.paired_tv.stream_port}{extra}"
            )
        self.after(500, self._tick)

    def _log(self, msg: str) -> None:
        self._log_q.append(msg)

    def _append_log(self, msg: str) -> None:
        from datetime import datetime

        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, f"[{datetime.now():%H:%M:%S}] {msg}\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _close(self) -> None:
        self._stop()
        self.session.stop()
        self.destroy()


def main() -> None:
    PcConsoleApp().mainloop()


if __name__ == "__main__":
    main()
