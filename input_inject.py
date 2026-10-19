"""Map processed motion to keyboard + mouse for Dolphin Emulated Wiimote (MVP)."""

from __future__ import annotations

from typing import Optional

from .motion import ProcessedMotion
from .protocol import (
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
    BTN_TWO,
)

# Dolphin default-ish mapping via ProfileWriter keys
BUTTON_KEYS = {
    BTN_A: "x",
    BTN_B: "z",
    BTN_ONE: "1",
    BTN_TWO: "2",
    BTN_PLUS: "=",
    BTN_MINUS: "-",
    BTN_HOME: "home",
    BTN_DPAD_UP: "up",
    BTN_DPAD_DOWN: "down",
    BTN_DPAD_LEFT: "left",
    BTN_DPAD_RIGHT: "right",
}


class KeyboardMouseInjector:
    def __init__(self, mouse_gain: float = 800.0) -> None:
        self.mouse_gain = mouse_gain
        self._last_buttons = 0
        self._last_u = 0.5
        self._last_v = 0.5
        self._keyboard = None
        self._mouse = None
        self._enabled = False
        self._error: Optional[str] = None
        self._try_import()

    def _try_import(self) -> None:
        try:
            from pynput.keyboard import Controller as KeyCtrl
            from pynput.keyboard import Key
            from pynput.mouse import Controller as MouseCtrl

            self._keyboard = KeyCtrl()
            self._mouse = MouseCtrl()
            self._Key = Key
            self._enabled = True
        except Exception as e:
            self._error = str(e)
            self._enabled = False

    @property
    def available(self) -> bool:
        return self._enabled

    @property
    def error(self) -> Optional[str]:
        return self._error

    def apply(self, motion: ProcessedMotion) -> None:
        if not self._enabled:
            return

        for bit, key_name in BUTTON_KEYS.items():
            was = bool(self._last_buttons & bit)
            now = bool(motion.buttons & bit)
            if was == now:
                continue
            key = self._resolve(key_name)
            try:
                if now:
                    self._keyboard.press(key)
                else:
                    self._keyboard.release(key)
            except Exception:
                pass

        du = motion.pointer_u - self._last_u
        dv = motion.pointer_v - self._last_v
        self._last_u = motion.pointer_u
        self._last_v = motion.pointer_v
        dx = int(du * self.mouse_gain)
        dy = int(dv * self.mouse_gain)
        if dx or dy:
            try:
                self._mouse.move(dx, dy)
            except Exception:
                pass

        self._last_buttons = motion.buttons

    def release_all(self) -> None:
        if not self._enabled:
            return
        for bit, key_name in BUTTON_KEYS.items():
            if self._last_buttons & bit:
                try:
                    self._keyboard.release(self._resolve(key_name))
                except Exception:
                    pass
        self._last_buttons = 0

    def _resolve(self, name: str):
        special = {
            "up": self._Key.up,
            "down": self._Key.down,
            "left": self._Key.left,
            "right": self._Key.right,
            "home": self._Key.home,
        }
        if name in special:
            return special[name]
        return name
