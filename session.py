"""UDP controller session + PIN pairing for phones and TVs."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .motion import MotionEngine, ProcessedMotion
from .protocol import (
    DEFAULT_CONTROL_PORT,
    DEFAULT_DISCOVERY_PORT,
    DEFAULT_STREAM_PORT,
    MAGIC,
    ROLE_HUB,
    ROLE_PHONE,
    ROLE_TV,
    TYPE_DISCOVER,
    TYPE_FRAME,
    TYPE_HELLO,
    TYPE_PAIR,
    VERSION,
    build_qr_uri,
    generate_pin,
    local_ip,
    new_session_token,
    pack_announce,
    pack_hello_ack,
    pack_pair_ack,
    unpack_announce,
    unpack_frame,
    unpack_hello,
    unpack_pair,
)


@dataclass
class PlayerSlot:
    player_id: int
    device_id: bytes = b""
    device_name: str = ""
    endpoint: Optional[tuple[str, int]] = None
    last_seen: float = 0.0
    last_sequence: int = 0
    battery: float = 1.0
    motion_engine: MotionEngine = field(default_factory=MotionEngine)
    last_motion: Optional[ProcessedMotion] = None

    @property
    def connected(self) -> bool:
        return self.endpoint is not None and (time.time() - self.last_seen) < 3.0

    def reset(self) -> None:
        self.device_id = b""
        self.device_name = ""
        self.endpoint = None
        self.last_sequence = 0
        self.battery = 1.0
        self.last_motion = None
        self.motion_engine = MotionEngine()


@dataclass
class PairedTV:
    name: str
    address: str
    control_port: int
    stream_port: int
    device_id: bytes
    paired_at: float = field(default_factory=time.time)


class SessionServer:
    def __init__(
        self,
        host_name: Optional[str] = None,
        control_port: int = DEFAULT_CONTROL_PORT,
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
        stream_port: int = DEFAULT_STREAM_PORT,
        max_players: int = 4,
    ) -> None:
        self.host_name = host_name or socket.gethostname()
        self.control_port = control_port
        self.discovery_port = discovery_port
        self.stream_port = stream_port
        self.token = new_session_token()
        self.pin = generate_pin()
        self.slots: List[PlayerSlot] = [PlayerSlot(player_id=i) for i in range(max_players)]
        self.paired_tv: Optional[PairedTV] = None
        self._device_map: Dict[bytes, int] = {}
        self._sock: Optional[socket.socket] = None
        self._disc_sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._lock = threading.Lock()
        self._found_tvs: Dict[str, object] = {}

        self.on_log: Callable[[str], None] = lambda m: None
        self.on_player: Callable[[PlayerSlot], None] = lambda s: None
        self.on_frame: Callable[[PlayerSlot, ProcessedMotion], None] = lambda s, m: None
        self.on_tv_paired: Callable[[PairedTV], None] = lambda t: None

    def regenerate_pin(self) -> str:
        self.pin = generate_pin()
        return self.pin

    def start(self) -> None:
        if self._sock is not None:
            return
        self._stop.clear()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", self.control_port))
        self._sock.settimeout(0.5)

        self._disc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._disc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._disc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        t1 = threading.Thread(target=self._recv_loop, name="dest-recv", daemon=True)
        t2 = threading.Thread(target=self._beacon_loop, name="dest-beacon", daemon=True)
        self._threads = [t1, t2]
        t1.start()
        t2.start()
        self.on_log(f"Console listening :{self.control_port}  PIN={self.pin}")

    def stop(self) -> None:
        self._stop.set()
        for s in (self._sock, self._disc_sock):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
        self._sock = None
        self._disc_sock = None
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads.clear()

    def qr_uri(self, ip: Optional[str] = None) -> str:
        return build_qr_uri(
            self.host_name,
            ip or local_ip(),
            self.control_port,
            self.token,
            self.pin,
        )

    def scan_tvs(self, timeout: float = 2.0) -> list:
        """Listen for TV announce packets."""
        found: Dict[str, object] = {}
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", self.discovery_port))
        except OSError:
            sock.bind(("0.0.0.0", 0))
        sock.settimeout(0.3)
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = sock.recvfrom(256)
                ann = unpack_announce(data, address=addr[0])
                if ann and ann.role == ROLE_TV:
                    key = f"{addr[0]}:{ann.control_port}"
                    found[key] = ann
            except socket.timeout:
                continue
            except OSError:
                break
        sock.close()
        self._found_tvs = found
        return list(found.values())

    def _recv_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 6:
                continue
            magic = int.from_bytes(data[0:4], "little")
            ver, typ = data[4], data[5]
            if magic != MAGIC or ver != VERSION:
                continue
            if typ == TYPE_HELLO:
                self._handle_hello(data, addr)
            elif typ == TYPE_FRAME:
                self._handle_frame(data, addr)
            elif typ == TYPE_PAIR:
                self._handle_pair(data, addr)
            elif typ == TYPE_DISCOVER:
                pass  # hub announces; ignore

    def _beacon_loop(self) -> None:
        assert self._disc_sock is not None
        while not self._stop.is_set():
            try:
                payload = pack_announce(
                    ROLE_HUB,
                    self.host_name,
                    self.control_port,
                    self.stream_port,
                    self.token,
                )
                self._disc_sock.sendto(payload, ("255.255.255.255", self.discovery_port))
            except OSError as e:
                self.on_log(f"Beacon: {e}")
            self._stop.wait(1.0)

    def _pin_ok(self, pin: str) -> bool:
        return (pin or "").strip() == self.pin

    def _handle_pair(self, data: bytes, addr: tuple[str, int]) -> None:
        parsed = unpack_pair(data)
        if not parsed:
            return
        role, pin, device_id, name, stream_port = parsed
        if not self._pin_ok(pin):
            ack = pack_pair_ack(False, "Wrong PIN")
            try:
                self._sock.sendto(ack, addr)  # type: ignore
            except OSError:
                pass
            self.on_log(f"Pair rejected (PIN) from {addr[0]} ({name})")
            return

        if role == ROLE_TV:
            if not stream_port:
                stream_port = DEFAULT_STREAM_PORT
                for ann in self._found_tvs.values():
                    if getattr(ann, "address", None) == addr[0]:
                        stream_port = getattr(ann, "stream_port", stream_port)
                        break
            tv = PairedTV(
                name=name or "TV",
                address=addr[0],
                control_port=addr[1],
                stream_port=stream_port,
                device_id=device_id,
            )
            self.paired_tv = tv
            ack = pack_pair_ack(True, f"Paired as TV → stream :{tv.stream_port}")
            try:
                self._sock.sendto(ack, addr)  # type: ignore
            except OSError:
                pass
            self.on_log(f"TV paired: {tv.name} @ {tv.address}:{tv.stream_port}")
            self.on_tv_paired(tv)
            return

        # Phone pairing via TYPE_PAIR also works (slot reserve)
        if role == ROLE_PHONE:
            with self._lock:
                free = next((s for s in self.slots if not s.device_id or not s.connected), None)
                if free is None:
                    ack = pack_pair_ack(False, "No free slots")
                    try:
                        self._sock.sendto(ack, addr)  # type: ignore
                    except OSError:
                        pass
                    return
                free.reset()
                free.device_id = device_id
                free.device_name = name or addr[0]
                free.endpoint = addr
                free.last_seen = time.time()
                self._device_map[device_id] = free.player_id
            ack = pack_pair_ack(True, f"P{free.player_id + 1}")
            try:
                self._sock.sendto(ack, addr)  # type: ignore
            except OSError:
                pass
            # Also send hello-ack style so client can stream
            hack = pack_hello_ack(free.player_id, int(time.time() * 1_000_000))
            try:
                self._sock.sendto(hack, addr)  # type: ignore
            except OSError:
                pass
            self.on_log(f"Phone paired: P{free.player_id + 1} {name}")
            self.on_player(free)

    def _handle_hello(self, data: bytes, addr: tuple[str, int]) -> None:
        parsed = unpack_hello(data)
        if not parsed:
            return
        device_id, token, pin, name = parsed
        if token != self.token and token != b"\0" * 16:
            # Allow empty token if PIN is correct (manual connect)
            if not self._pin_ok(pin):
                self.on_log(f"Hello rejected (token/PIN) {addr[0]}")
                return
        elif not self._pin_ok(pin) and pin:
            self.on_log(f"Hello rejected (PIN) {addr[0]}")
            return
        elif not pin and token != self.token:
            self.on_log(f"Hello rejected (auth) {addr[0]}")
            return
        # If pin empty but token matches (from QR which embeds pin too) — QR includes pin
        if pin and not self._pin_ok(pin):
            self.on_log(f"Hello rejected (bad PIN) {addr[0]}")
            return
        if not pin and token == self.token:
            pass  # token-only from old clients
        elif pin and self._pin_ok(pin):
            pass
        else:
            # Require pin for phones
            if not self._pin_ok(pin):
                self.on_log(f"Hello rejected — enter PIN {self.pin}")
                return

        with self._lock:
            if device_id in self._device_map:
                slot = self.slots[self._device_map[device_id]]
            else:
                free = next((s for s in self.slots if not s.device_id or not s.connected), None)
                if free is None:
                    self.on_log("No free player slots")
                    return
                free.reset()
                free.device_id = device_id
                idx = free.player_id
                self._device_map[device_id] = idx
                slot = free
            slot.device_name = name or addr[0]
            slot.endpoint = addr
            slot.last_seen = time.time()

        ack = pack_hello_ack(slot.player_id, int(time.time() * 1_000_000))
        try:
            self._sock.sendto(ack, addr)  # type: ignore
        except OSError:
            pass
        self.on_log(f"P{slot.player_id + 1} connected: {slot.device_name}")
        self.on_player(slot)

    def _handle_frame(self, data: bytes, addr: tuple[str, int]) -> None:
        frame = unpack_frame(data)
        if frame is None or frame.player_id >= len(self.slots):
            return
        with self._lock:
            slot = self.slots[frame.player_id]
            if (
                frame.sequence
                and frame.sequence <= slot.last_sequence
                and slot.last_sequence - frame.sequence < 1_000_000
            ):
                return
            slot.last_sequence = frame.sequence
            slot.last_seen = time.time()
            slot.battery = frame.battery
            slot.endpoint = addr
            motion = slot.motion_engine.process(frame)
            slot.last_motion = motion
        self.on_frame(slot, motion)
