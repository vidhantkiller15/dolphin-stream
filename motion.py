"""Motion processing — Mode 1 pointer, shake, calibration."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List

from .protocol import BTN_RECENTER, BTN_SHAKE, ControllerFrame


def _norm_quat(x: float, y: float, z: float, w: float) -> tuple[float, float, float, float]:
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n < 1e-8:
        return 0.0, 0.0, 0.0, 1.0
    return x / n, y / n, z / n, w / n


def _quat_conj(x: float, y: float, z: float, w: float) -> tuple[float, float, float, float]:
    return -x, -y, -z, w


def _quat_mul(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def yaw_pitch_roll(q: tuple[float, float, float, float]) -> tuple[float, float, float]:
    x, y, z, w = _norm_quat(*q)
    sinr = 2 * (w * x + y * z)
    cosr = 1 - 2 * (x * x + y * y)
    roll = math.atan2(sinr, cosr)

    sinp = 2 * (w * y - z * x)
    if abs(sinp) >= 1:
        pitch = math.copysign(math.pi / 2, sinp)
    else:
        pitch = math.asin(sinp)

    siny = 2 * (w * z + x * y)
    cosy = 1 - 2 * (y * y + z * z)
    yaw = math.atan2(siny, cosy)
    return yaw, pitch, roll


@dataclass
class Calibration:
    neutral: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    gyro_bias: tuple[float, float, float] = (0.0, 0.0, 0.0)
    sensitivity: float = 1.0
    pointer_gain: float = 1.0
    deadzone_gyro: float = 0.02
    max_yaw: float = 0.7
    max_pitch: float = 0.5


@dataclass
class ProcessedMotion:
    player_id: int
    sequence: int
    buttons: int
    pointer_u: float
    pointer_v: float
    gx: float
    gy: float
    gz: float
    ax: float
    ay: float
    az: float
    shake: bool
    battery: float
    play_quat: tuple[float, float, float, float]


class MotionEngine:
    def __init__(self, calib: Calibration | None = None) -> None:
        self.calib = calib or Calibration()
        self._energy: List[float] = [0.0] * 32
        self._ei = 0
        self._efilled = False

    def set_neutral(self, qx: float, qy: float, qz: float, qw: float) -> None:
        self.calib.neutral = _norm_quat(qx, qy, qz, qw)

    def process(self, frame: ControllerFrame) -> ProcessedMotion:
        q_raw = _norm_quat(frame.qx, frame.qy, frame.qz, frame.qw)
        q_play = _quat_mul(_quat_conj(*self.calib.neutral), q_raw)
        q_play = _norm_quat(*q_play)

        bx, by, bz = self.calib.gyro_bias
        gx = (frame.gx - bx) * self.calib.sensitivity
        gy = (frame.gy - by) * self.calib.sensitivity
        gz = (frame.gz - bz) * self.calib.sensitivity
        gmag = math.sqrt(gx * gx + gy * gy + gz * gz)
        if gmag < self.calib.deadzone_gyro:
            gx = gy = gz = 0.0

        lin = math.sqrt(frame.ax * frame.ax + frame.ay * frame.ay + frame.az * frame.az)
        shake = self._update_shake(abs(lin - 9.81))

        buttons = frame.buttons
        if shake:
            buttons |= BTN_SHAKE
        if buttons & BTN_RECENTER:
            self.set_neutral(frame.qx, frame.qy, frame.qz, frame.qw)
            q_play = (0.0, 0.0, 0.0, 1.0)

        yaw, pitch, _ = yaw_pitch_roll(q_play)
        gain = self.calib.pointer_gain
        u = 0.5 + (yaw / self.calib.max_yaw) * 0.5 * gain
        v = 0.5 + (pitch / self.calib.max_pitch) * 0.5 * gain
        u = max(0.0, min(1.0, u))
        v = max(0.0, min(1.0, v))

        return ProcessedMotion(
            player_id=frame.player_id,
            sequence=frame.sequence,
            buttons=buttons,
            pointer_u=u,
            pointer_v=v,
            gx=gx,
            gy=gy,
            gz=gz,
            ax=frame.ax,
            ay=frame.ay,
            az=frame.az,
            shake=shake,
            battery=frame.battery,
            play_quat=q_play,
        )

    def _update_shake(self, mag: float) -> bool:
        self._energy[self._ei] = mag
        self._ei = (self._ei + 1) % len(self._energy)
        if self._ei == 0:
            self._efilled = True
        n = len(self._energy) if self._efilled else max(1, self._ei)
        if n < 8:
            return False
        mean = sum(self._energy[:n]) / n
        mean_sq = sum(x * x for x in self._energy[:n]) / n
        var = mean_sq - mean * mean
        return var > 25.0
