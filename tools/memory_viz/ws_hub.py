"""Thread-safe WebSocket hub — minimal RFC6455 for live cortex updates."""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import threading
from typing import Any, Callable


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept_key(sec_key: str) -> str:
    raw = (sec_key.strip() + WS_GUID).encode("utf-8")
    return base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")


def encode_text_frame(text: str) -> bytes:
    payload = text.encode("utf-8")
    n = len(payload)
    header = bytearray([0x81])  # FIN + text
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", n))
    return bytes(header) + payload


def decode_frames(buf: bytearray) -> tuple[list[str | None], bytearray]:
    """Parse complete frames. None in messages means close. Returns (messages, remaining)."""
    messages: list[str | None] = []
    while True:
        if len(buf) < 2:
            break
        b0, b1 = buf[0], buf[1]
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        idx = 2
        if length == 126:
            if len(buf) < 4:
                break
            length = struct.unpack("!H", buf[2:4])[0]
            idx = 4
        elif length == 127:
            if len(buf) < 10:
                break
            length = struct.unpack("!Q", buf[2:10])[0]
            idx = 10
        mask_len = 4 if masked else 0
        if len(buf) < idx + mask_len + length:
            break
        mask = buf[idx : idx + mask_len] if masked else b""
        idx += mask_len
        data = bytes(buf[idx : idx + length])
        del buf[: idx + length]
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode == 0x8:  # close
            messages.append(None)
            break
        if opcode == 0x9:  # ping — ignore (client rarely pings us first)
            continue
        if opcode == 0x1:  # text
            try:
                messages.append(data.decode("utf-8"))
            except Exception:
                continue
        # binary / continuation ignored
    return messages, buf


class WsHub:
    """Broadcast JSON events to connected browser clients."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._clients: list[Any] = []  # sockets
        self._on_client: Callable[[], None] | None = None

    def set_on_client(self, fn: Callable[[], None] | None) -> None:
        self._on_client = fn

    def add(self, sock) -> None:
        with self._lock:
            self._clients.append(sock)
        if self._on_client:
            try:
                self._on_client()
            except Exception:
                pass

    def remove(self, sock) -> None:
        with self._lock:
            if sock in self._clients:
                self._clients.remove(sock)

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def broadcast(self, event: dict[str, Any]) -> int:
        raw = encode_text_frame(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        dead = []
        sent = 0
        with self._lock:
            clients = list(self._clients)
        for s in clients:
            try:
                s.sendall(raw)
                sent += 1
            except Exception:
                dead.append(s)
        for s in dead:
            self.remove(s)
            try:
                s.close()
            except Exception:
                pass
        return sent


_HUB: WsHub | None = None


def get_hub() -> WsHub:
    global _HUB
    if _HUB is None:
        _HUB = WsHub()
    return _HUB


def reset_hub_for_tests() -> WsHub:
    global _HUB
    _HUB = WsHub()
    return _HUB
