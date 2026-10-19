"""Locate Dolphin (incl. portable bundle), profiles, launch games."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

GAME_EXTS = {".iso", ".gcm", ".wbfs", ".wia", ".rvz", ".wad", ".dol", ".elf", ".json"}


@dataclass
class DolphinInstall:
    exe: Optional[Path]
    user_dir: Path

    @property
    def found(self) -> bool:
        return self.exe is not None and self.exe.is_file()


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_user_dir() -> Path:
    return Path(os.path.expanduser("~/Documents")) / "Dolphin Emulator"


def locate_dolphin(override: Optional[str] = None) -> DolphinInstall:
    if override:
        p = Path(override)
        if p.is_file():
            return DolphinInstall(exe=p, user_dir=_user_for_exe(p))

    root = project_root()
    candidates = [
        # Portable copy shipped next to this project
        root / "dolphin-2606-x64" / "Dolphin-x64" / "Dolphin.exe",
        root / "Dolphin" / "Dolphin.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Dolphin" / "Dolphin.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Dolphin" / "Dolphin.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Dolphin Emulator" / "Dolphin.exe",
    ]
    for c in candidates:
        if c.is_file():
            return DolphinInstall(exe=c, user_dir=_user_for_exe(c))
    return DolphinInstall(exe=None, user_dir=default_user_dir())


def _user_for_exe(exe: Path) -> Path:
    portable = exe.parent / "User"
    if portable.is_dir():
        return portable
    return default_user_dir()


def list_games(folder: Path) -> List[Path]:
    if not folder.is_dir():
        return []
    games: List[Path] = []
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() in GAME_EXTS:
            games.append(p)
    return games


WIIMOTE_TEMPLATE = """# Dolphinestreme DEV profile
[Wiimote1]
Device = Keyboard Mouse2/0/Keyboard Mouse2
Source = 1
Buttons/A = `X`
Buttons/B = `Z`
Buttons/1 = `1`
Buttons/2 = `2`
Buttons/+ = `EQUALS`
Buttons/- = `MINUS`
Buttons/Home = `HOME`
D-Pad/Up = `UP`
D-Pad/Down = `DOWN`
D-Pad/Left = `LEFT`
D-Pad/Right = `RIGHT`
IR/Up = `Cursor Y-`
IR/Down = `Cursor Y+`
IR/Left = `Cursor X-`
IR/Right = `Cursor X+`
Extension = None
[Wiimote2]
Source = 1
Extension = None
[Wiimote3]
Source = 1
Extension = None
[Wiimote4]
Source = 1
Extension = None
"""


def apply_wiimote_profile(user_dir: Path) -> Path:
    cfg = user_dir / "Config"
    cfg.mkdir(parents=True, exist_ok=True)
    path = cfg / "WiimoteNew.ini"
    if path.is_file():
        bak = cfg / "WiimoteNew.ini.bak-dolphinestreme"
        if not bak.exists():
            shutil.copy2(path, bak)
    path.write_text(WIIMOTE_TEMPLATE, encoding="utf-8")
    return path


def launch_dolphin(
    exe: Path,
    game: Optional[Path] = None,
    user_dir: Optional[Path] = None,
    batch: bool = False,
) -> subprocess.Popen:
    args = [str(exe)]
    if user_dir:
        args.extend(["-u", str(user_dir)])
    if batch:
        args.append("-b")
    if game and game.is_file():
        args.extend(["-e", str(game)])
    # Prefer borderless-ish: user can fullscreen in Dolphin
    return subprocess.Popen(
        args,
        cwd=str(exe.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
