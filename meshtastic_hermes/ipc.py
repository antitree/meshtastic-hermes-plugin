"""Versioned, bounded NDJSON IPC shared with Meshagatchi."""

from __future__ import annotations

import json
import socket
import uuid
from typing import Any

PROTOCOL_VERSION = 1
DEFAULT_MAX_MESSAGE_BYTES = 200
MAX_REQUEST_BYTES = 8192
MAX_QUEUED_REQUESTS = 32
OPERATIONS = (
    "register", "send", "event.submit", "event.schedule",
    "personality.request", "personality.proposal",
)


def hello_payload(node_id: str | None, channel_name: str, channel_index: int) -> dict[str, Any]:
    return {
        "type": "hello", "version": PROTOCOL_VERSION, "node_id": node_id,
        "channel_name": channel_name, "channel_index": channel_index,
        "capabilities": {"operations": list(OPERATIONS), "max_frame_bytes": MAX_REQUEST_BYTES,
                          "max_queued_requests": MAX_QUEUED_REQUESTS},
    }


def message_payload(inbound: dict[str, Any], channel_name: str, channel_index: int) -> dict[str, Any]:
    return {
        "type": "message", "version": PROTOCOL_VERSION,
        "text": str(inbound.get("text", "")), "sender_id": str(inbound.get("from_id", "")),
        "message_id": str(inbound.get("message_id", "")),
        "channel_name": channel_name, "channel_index": channel_index, "is_self": False,
    }


def request_id(request: Any) -> str | None:
    value = request.get("id") if isinstance(request, dict) else None
    return value if isinstance(value, str) and 1 <= len(value) <= 96 else None


def validate_envelope(request: Any, *, channel_name: str, channel_index: int) -> str | None:
    if not isinstance(request, dict):
        return "IPC request must be an object"
    if request.get("version", PROTOCOL_VERSION) != PROTOCOL_VERSION:
        return "unsupported IPC protocol version"
    if request.get("channel_name") not in (None, channel_name):
        return "channel name is not allowed"
    if request.get("channel_index") not in (None, channel_index):
        return "channel index is not allowed"
    if request.get("op") not in OPERATIONS:
        return "unsupported IPC operation"
    if request.get("op") != "register" and request_id(request) is None:
        return "request id is required"
    return None


def validate_send_request(request: Any, *, channel_name: str, channel_index: int,
                          max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> tuple[str | None, str | None]:
    if not isinstance(request, dict) or request.get("op") != "send":
        return None, "unsupported IPC operation"
    if request.get("version", PROTOCOL_VERSION) != PROTOCOL_VERSION:
        return None, "unsupported IPC protocol version"
    text = request.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, "text must be a non-empty string"
    if len(text.encode("utf-8")) > max_bytes:
        return None, f"text exceeds {max_bytes} UTF-8 bytes"
    if request.get("channel_name") != channel_name:
        return None, "channel name is not allowed"
    requested_index = request.get("channel_index")
    if isinstance(requested_index, bool) or not isinstance(requested_index, int):
        return None, "channel index must be an integer"
    if requested_index != channel_index:
        return None, "channel index is not allowed"
    return text.strip(), None


def round_trip(path: str, payload: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    """Short-lived client used by the Hermes native event tool."""
    request = {**payload, "id": str(payload.get("id") or uuid.uuid4()), "version": PROTOCOL_VERSION,
               "channel_name": "in.secure", "channel_index": 1}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(path)
        hello = _read_frame(conn)
        if hello.get("type") != "hello" or hello.get("version") != PROTOCOL_VERSION \
                or hello.get("channel_name") != "in.secure" or hello.get("channel_index") != 1:
            raise RuntimeError("Meshagatchi IPC handshake rejected")
        encoded = (json.dumps(request, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError("Meshagatchi IPC request is too large")
        conn.sendall(encoded)
        return _read_frame(conn)


def _read_frame(conn: socket.socket) -> dict[str, Any]:
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        byte = conn.recv(1)
        if not byte:
            raise ConnectionError("Meshagatchi IPC closed")
        data.extend(byte)
        if byte == b"\n":
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise ValueError("IPC frame is not an object")
            return payload
    raise ValueError("IPC frame is too large")
