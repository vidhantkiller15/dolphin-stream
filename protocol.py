"""Binary UDP protocol — controllers, pairing, discovery, lightweight video."""

from __future__ import annotations

import base64
import json
import random
import socket
import struct
import uuid
from dataclasses import dataclass
from typing import Optional

MAGIC = 0x44455354  # "DEST"
VERSION = 1

# Message types
TYPE_HELLO = 1
TYPE_HELLO_ACK = 2
TYPE_FRAME = 10
TYPE_FEEDBACK = 20
TYPE_DISCOVER = 30  # device announce / beacon
TYPE_PAIR = 31  # PIN pair request (TV or phone → PC)
TYPE_PAIR_ACK = 32
TYPE_VIDEO = 40  # JPEG video chunk

ROLE_HUB = 1
ROLE_TV = 2
ROLE_PHONE = 3

DEFAULT_CONTROL_PORT = 27015
DEFAULT_FEEDBACK_PORT = 27016
DEFAULT_DISCOVERY_PORT = 27017
DEFAULT_STREAM_PORT = 27018
DEFAULT_TV_PORT = 27019

FRAME_SIZE = 80
MAX_UDP = 55_000

# Buttons
BTN_A = 1 << 0
BTN_B = 1 << 1
BTN_ONE = 1 << 2
BTN_TWO = 1 << 3
BTN_PLUS = 1 << 4
BTN_MINUS = 1 << 5
BTN_HOME = 1 << 6
BTN_DPAD_UP = 1 << 7
BTN_DPAD_DOWN = 1 << 8
BTN_DPAD_LEFT = 1 << 9
BTN_DPAD_RIGHT = 1 << 10
BTN_C = 1 << 11
BTN_Z = 1 << 12
BTN_RECENTER = 1 << 13
BTN_SHAKE = 1 << 14


def new_device_id() -> bytes:
    return uuid.uuid4().bytes


def new_session_token() -> bytes:
    return uuid.uuid4().bytes


def generate_pin() -> str:
    """6-digit PIN shown on PC."""
    return f"{random.randint(0, 999999):06d}"


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ── Controller frames ──────────────────────────────────────────────


@dataclass
class ControllerFrame:
    player_id: int = 0
    flags: int = 0
    sequence: int = 0
    client_timestamp_us: int = 0
    buttons: int = 0
    qx: float = 0.0
    qy: float = 0.0
    qz: float = 0.0
    qw: float = 1.0
    gx: float = 0.0
    gy: float = 0.0
    gz: float = 0.0
    ax: float = 0.0
    ay: float = 0.0
    az: float = 9.81
    stick_x: float = 0.0
    stick_y: float = 0.0
    battery: float = 1.0


@dataclass
class HelloAck:
    player_id: int
    server_time_us: int
    flags: int = 0


def pack_hello(device_id: bytes, token: bytes, name: str, pin: str = "") -> bytes:
    """Hello with optional 6-char PIN (ASCII, padded)."""
    name_b = name.encode("utf-8")[:200]
    pin_b = (pin or "").encode("ascii")[:6].ljust(6, b"\0")
    return (
        struct.pack("<IBB", MAGIC, VERSION, TYPE_HELLO)
        + device_id[:16].ljust(16, b"\0")
        + token[:16].ljust(16, b"\0")
        + pin_b
        + bytes([len(name_b)])
        + name_b
    )


def unpack_hello(data: bytes) -> Optional[tuple[bytes, bytes, str, str]]:
    """Returns device_id, token, pin, name. Supports legacy hello without pin."""
    if len(data) < 39:
        return None
    magic, ver, typ = struct.unpack_from("<IBB", data, 0)
    if magic != MAGIC or ver != VERSION or typ != TYPE_HELLO:
        return None
    device_id = data[6:22]
    token = data[22:38]
    # New format: pin at 38:44, name_len at 44
    if len(data) >= 45 and data[44] < 200:
        pin = data[38:44].split(b"\0", 1)[0].decode("ascii", errors="ignore")
        name_len = data[44]
        if len(data) < 45 + name_len:
            return None
        name = data[45 : 45 + name_len].decode("utf-8", errors="replace")
        return device_id, token, pin, name
    # Legacy: name_len at 38
    name_len = data[38]
    if len(data) < 39 + name_len:
        return None
    name = data[39 : 39 + name_len].decode("utf-8", errors="replace")
    return device_id, token, "", name


def pack_hello_ack(player_id: int, server_time_us: int, flags: int = 0) -> bytes:
    return struct.pack(
        "<IBBBBQ",
        MAGIC,
        VERSION,
        TYPE_HELLO_ACK,
        player_id & 0xFF,
        flags & 0xFF,
        server_time_us & 0xFFFFFFFFFFFFFFFF,
    )


def unpack_hello_ack(data: bytes) -> Optional[HelloAck]:
    if len(data) < 16:
        return None
    magic, ver, typ, player, flags, t = struct.unpack_from("<IBBBBQ", data, 0)
    if magic != MAGIC or ver != VERSION or typ != TYPE_HELLO_ACK:
        return None
    return HelloAck(player_id=player, server_time_us=t, flags=flags)


def pack_frame(frame: ControllerFrame) -> bytes:
    return struct.pack(
        "<IBBBBIQI" "ffff" "fff" "fff" "ff" "f" "I",
        MAGIC,
        VERSION,
        TYPE_FRAME,
        frame.player_id & 0xFF,
        frame.flags & 0xFF,
        frame.sequence & 0xFFFFFFFF,
        frame.client_timestamp_us & 0xFFFFFFFFFFFFFFFF,
        frame.buttons & 0xFFFFFFFF,
        frame.qx,
        frame.qy,
        frame.qz,
        frame.qw,
        frame.gx,
        frame.gy,
        frame.gz,
        frame.ax,
        frame.ay,
        frame.az,
        frame.stick_x,
        frame.stick_y,
        frame.battery,
        0,
    )


def unpack_frame(data: bytes) -> Optional[ControllerFrame]:
    if len(data) < FRAME_SIZE:
        return None
    vals = struct.unpack_from("<IBBBBIQI" "ffff" "fff" "fff" "ff" "f" "I", data, 0)
    magic, ver, typ, player, flags, seq, ts, buttons = vals[:8]
    if magic != MAGIC or ver != VERSION or typ != TYPE_FRAME:
        return None
    (
        qx,
        qy,
        qz,
        qw,
        gx,
        gy,
        gz,
        ax,
        ay,
        az,
        sx,
        sy,
        bat,
        _res,
    ) = vals[8:]
    return ControllerFrame(
        player_id=player,
        flags=flags,
        sequence=seq,
        client_timestamp_us=ts,
        buttons=buttons,
        qx=qx,
        qy=qy,
        qz=qz,
        qw=qw,
        gx=gx,
        gy=gy,
        gz=gz,
        ax=ax,
        ay=ay,
        az=az,
        stick_x=sx,
        stick_y=sy,
        battery=bat,
    )


# ── Discovery / pairing ────────────────────────────────────────────


@dataclass
class DeviceAnnounce:
    role: int
    name: str
    control_port: int
    stream_port: int
    token: bytes
    address: str = ""
    pin_required: bool = True


def pack_announce(
    role: int,
    name: str,
    control_port: int,
    stream_port: int,
    token: bytes,
) -> bytes:
    name_b = name.encode("utf-8")[:48]
    buf = bytearray(80)
    struct.pack_into(
        "<IBBBHH",
        buf,
        0,
        MAGIC,
        VERSION,
        TYPE_DISCOVER,
        role & 0xFF,
        control_port & 0xFFFF,
        stream_port & 0xFFFF,
    )
    buf[12:28] = token[:16].ljust(16, b"\0")
    buf[28] = len(name_b)
    buf[29 : 29 + len(name_b)] = name_b
    return bytes(buf)


def unpack_announce(data: bytes, address: str = "") -> Optional[DeviceAnnounce]:
    if len(data) < 30:
        return None
    magic, ver, typ, role, cport, sport = struct.unpack_from("<IBBBHH", data, 0)
    if magic != MAGIC or ver != VERSION or typ != TYPE_DISCOVER:
        return None
    token = bytes(data[12:28])
    nlen = data[28]
    if len(data) < 29 + nlen:
        return None
    name = data[29 : 29 + nlen].decode("utf-8", errors="replace")
    return DeviceAnnounce(
        role=role,
        name=name,
        control_port=cport,
        stream_port=sport,
        token=token,
        address=address,
    )


def pack_pair(role: int, pin: str, device_id: bytes, name: str, stream_port: int = 0) -> bytes:
    name_b = name.encode("utf-8")[:48]
    pin_b = pin.encode("ascii")[:6].ljust(6, b"\0")
    return (
        struct.pack("<IBB", MAGIC, VERSION, TYPE_PAIR)
        + bytes([role & 0xFF])
        + pin_b
        + device_id[:16].ljust(16, b"\0")
        + struct.pack("<H", stream_port & 0xFFFF)
        + bytes([len(name_b)])
        + name_b
    )


def unpack_pair(data: bytes) -> Optional[tuple[int, str, bytes, str, int]]:
    """role, pin, device_id, name, stream_port"""
    if len(data) < 28:
        return None
    magic, ver, typ = struct.unpack_from("<IBB", data, 0)
    if magic != MAGIC or ver != VERSION or typ != TYPE_PAIR:
        return None
    role = data[6]
    pin = data[7:13].split(b"\0", 1)[0].decode("ascii", errors="ignore")
    device_id = data[13:29]
    stream_port = struct.unpack_from("<H", data, 29)[0]
    nlen = data[31]
    if len(data) < 32 + nlen:
        return None
    name = data[32 : 32 + nlen].decode("utf-8", errors="replace")
    return role, pin, device_id, name, stream_port


def pack_pair_ack(ok: bool, message: str = "") -> bytes:
    msg = message.encode("utf-8")[:60]
    return struct.pack("<IBBB", MAGIC, VERSION, TYPE_PAIR_ACK, 1 if ok else 0) + bytes([len(msg)]) + msg


def unpack_pair_ack(data: bytes) -> Optional[tuple[bool, str]]:
    if len(data) < 8:
        return None
    magic, ver, typ, ok = struct.unpack_from("<IBBB", data, 0)
    if magic != MAGIC or ver != VERSION or typ != TYPE_PAIR_ACK:
        return None
    nlen = data[7]
    msg = data[8 : 8 + nlen].decode("utf-8", errors="replace") if len(data) >= 8 + nlen else ""
    return bool(ok), msg


# ── Video (JPEG chunks) ────────────────────────────────────────────


def pack_video_chunk(
    frame_id: int,
    chunk_idx: int,
    chunk_count: int,
    width: int,
    height: int,
    payload: bytes,
) -> bytes:
    header = struct.pack(
        "<IBB IHH HH",
        MAGIC,
        VERSION,
        TYPE_VIDEO,
        frame_id & 0xFFFFFFFF,
        chunk_idx & 0xFFFF,
        chunk_count & 0xFFFF,
        width & 0xFFFF,
        height & 0xFFFF,
    )
    return header + payload


def unpack_video_chunk(data: bytes) -> Optional[tuple[int, int, int, int, int, bytes]]:
    if len(data) < 18:
        return None
    magic, ver, typ, fid, cidx, ccnt, w, h = struct.unpack_from("<IBB IHH HH", data, 0)
    if magic != MAGIC or ver != VERSION or typ != TYPE_VIDEO:
        return None
    return fid, cidx, ccnt, w, h, data[18:]


def build_qr_uri(name: str, ip: str, port: int, token: bytes, pin: str) -> str:
    payload = {
        "v": 1,
        "name": name,
        "ip": ip,
        "port": port,
        "token": token.hex(),
        "pin": pin,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"dolphinestreme://connect?d={b64}"


@dataclass
class ConnectInfo:
    name: str
    ip: str
    port: int
    token_hex: str
    pin: str = ""


def parse_qr_uri(uri: str) -> Optional[ConnectInfo]:
    prefix = "dolphinestreme://connect?d="
    if not uri.startswith(prefix):
        return None
    b64 = uri[len(prefix) :]
    pad = "=" * (-len(b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(b64 + pad)
        data = json.loads(raw.decode("utf-8"))
        return ConnectInfo(
            name=str(data.get("name", "PC")),
            ip=str(data["ip"]),
            port=int(data["port"]),
            token_hex=str(data["token"]),
            pin=str(data.get("pin", "")),
        )
    except Exception:
        return None
