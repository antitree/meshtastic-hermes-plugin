"""Bridge between Meshtastic packets and a normalized chat model.

Shared by the Hermes platform adapter ([meshtastic_platform]) and the REPL
simulator, so both map inbound radio text and outbound replies identically. These
are pure functions — no radio and no Hermes imports — so the routing/reply policy
is unit-testable without hardware.

`chat_id` scheme (a stable conversation identifier the agent/gateway keys on):
  - Direct message  -> the peer node id, e.g. "!a696579c"
  - Channel message -> "ch:<index>",   e.g. "ch:0"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# A responder turns inbound text (+ context) into a reply string. In the Hermes
# adapter the gateway/LLM plays this role; in the simulator it's a local stub.
Responder = Callable[[str, dict], str]


def normalize_node(num: Any) -> str:
    """Normalize a numeric node address to Meshtastic's !hex form."""
    if num is None:
        return ""
    if isinstance(num, str):
        return num
    try:
        return f"!{int(num):08x}"
    except (ValueError, TypeError):
        return str(num)


def inbound_from_packet(packet: dict, my_node_id: str | None) -> dict | None:
    """Normalize a received packet into an inbound message, or None to ignore it.

    Returns None for: non-text, undecodable/encrypted frames, and our own
    transmissions (loop guard). Only messages we are entitled to read surface here.
    """
    if "decoded" not in packet:
        return None  # encrypted / not for us — never auto-reply to opaque traffic
    decoded = packet.get("decoded") or {}
    if decoded.get("portnum") != "TEXT_MESSAGE_APP":
        return None

    from_id = packet.get("fromId") or normalize_node(packet.get("from"))
    if my_node_id and from_id == my_node_id:
        return None  # ignore our own echoed messages

    text = decoded.get("text")
    if text is None:
        payload = decoded.get("payload")
        if isinstance(payload, (bytes, bytearray)):
            text = payload.decode("utf-8", "replace")
    if not text:
        return None

    to_id = packet.get("toId") or normalize_node(packet.get("to"))
    is_dm = bool(my_node_id) and to_id == my_node_id
    return {
        "text": text,
        "from_id": from_id,
        "to_id": to_id,
        "channel": packet.get("channel") or 0,
        "is_dm": is_dm,
        "message_id": str(packet.get("id") or ""),
    }


def chat_id_for(inbound: dict) -> str:
    """Stable conversation id: peer node for DMs, 'ch:<index>' for channels."""
    return inbound["from_id"] if inbound["is_dm"] else f"ch:{inbound['channel']}"


def outbound_target(chat_id: str) -> dict:
    """Map a chat_id back to radio send params: dest_id, channel_index, pki.

    DM chat ids (node ids) reply end-to-end encrypted (pki); channel ids reply as
    a channel broadcast.
    """
    if chat_id.startswith("ch:"):
        try:
            channel_index = int(chat_id[3:])
        except ValueError:
            channel_index = 0
        return {"dest_id": None, "channel_index": channel_index, "pki": False}
    return {"dest_id": chat_id, "channel_index": 0, "pki": True}


# Sentinel for "reply on every channel" (vs. None = no channels, or a set of indices).
ALL_CHANNELS = "__all__"

# How a user targets the Primary channel by name. Meshtastic leaves the Primary
# channel's name EMPTY on the wire and clients display the LoRa preset (or simply
# "Primary") instead. An empty name must never match a config value — otherwise a
# typo'd channel name would silently resolve to the *public* Primary channel, which
# is exactly the mis-transmission this feature exists to prevent. So we accept these
# explicit aliases (case-insensitively) for an unnamed PRIMARY-role channel only.
PRIMARY_ALIASES = frozenset({"primary", "longfast"})


@dataclass(frozen=True)
class ChannelSpec:
    """A parsed, *unresolved* channel allowlist: names and/or legacy indices.

    Names are the safe way to configure the allowlist and cannot be resolved
    without the radio's channel table, so parsing and resolution are separate:
    :func:`parse_channel_spec` is pure, and :func:`resolve_channel_spec` is applied
    against a channel table at (re)connect time.
    """

    names: tuple[str, ...] = ()
    indices: frozenset[int] = field(default_factory=frozenset)

    def __bool__(self) -> bool:
        return bool(self.names or self.indices)


def parse_channel_spec(spec: Any) -> ChannelSpec | str | None:
    """Parse a channel-allowlist spec into None | ChannelSpec | ALL_CHANNELS.

    - None or ""             -> None          (DMs only)
    - "all" (any case)       -> ALL_CHANNELS  (every channel)
    - "in.secure"            -> ChannelSpec(names=("in.secure",))
    - "1,2"                  -> ChannelSpec(indices={1, 2})   (legacy; see below)
    - "in.secure, 2"         -> ChannelSpec(names=("in.secure",), indices={2})

    Channel NAMES are the primary configuration surface. Numeric indices are
    accepted for backwards compatibility but are *slots*, not identities: editing
    or reordering channels on the radio changes which channel a given index refers
    to, so an index-based allowlist can start transmitting on the wrong channel.
    Callers should warn when :attr:`ChannelSpec.indices` is non-empty.

    Splitting is on commas ONLY, and case is preserved — Meshtastic channel names
    are case-sensitive and may contain dots and internal spaces (``in.secure``,
    ``my channel``). Only surrounding whitespace is trimmed.
    """
    if spec is None:
        return None
    text = str(spec).strip()
    if not text:
        return None
    if text.lower() == "all":
        return ALL_CHANNELS

    names: list[str] = []
    indices: set[int] = set()
    for raw in text.split(","):
        part = raw.strip()
        if not part:
            continue
        try:
            indices.add(int(part))
        except ValueError:
            if part not in names:
                names.append(part)
    if not names and not indices:
        return None
    return ChannelSpec(names=tuple(names), indices=frozenset(indices))


def channel_table_entry(index: int, name: str, role: Any = None) -> dict:
    """Build one normalized channel-table row (see :func:`resolve_channel_spec`)."""
    return {"index": index, "name": name, "role": role}


def resolve_channel_spec(
    spec: ChannelSpec | str | None,
    channel_table: list[dict] | None,
    *,
    log: logging.Logger | None = None,
) -> tuple[set[int] | str | None, dict[str, int]]:
    """Resolve a :class:`ChannelSpec` against the radio's channel table.

    ``channel_table`` is a list of ``{"index": int, "name": str, "role": Any}``
    rows (see :func:`meshtastic_hermes.connection.ConnectionManager.channel_table`).
    Pure: it takes the table as data so it stays unit-testable without a radio.

    Returns ``(allowed_channels, resolved_mapping)`` where ``allowed_channels`` is
    the ``None`` | ``set[int]`` | ``ALL_CHANNELS`` value :func:`should_reply` wants,
    and ``resolved_mapping`` maps each configured name to the index it resolved to
    (for operator-facing logging).

    A name that is not present in the channel table is a WARNING and is skipped —
    the rest of the allowlist still applies. It is never silently dropped, and it
    never falls back to an index.
    """
    log = log or logger
    if spec is None or spec == ALL_CHANNELS:
        return spec, {}
    if not isinstance(spec, ChannelSpec):  # defensive: legacy set[int] callers
        return spec, {}

    table = channel_table or []
    by_name: dict[str, int] = {}
    primary_index: int | None = None
    for row in table:
        idx = row.get("index")
        if idx is None:
            continue
        name = row.get("name") or ""
        if name and name not in by_name:
            by_name[name] = idx
        if _is_primary(row) and primary_index is None:
            primary_index = idx

    allowed: set[int] = set(spec.indices)
    resolved: dict[str, int] = {}
    for name in spec.names:
        if name in by_name:
            idx = by_name[name]
        elif primary_index is not None and name.lower() in PRIMARY_ALIASES:
            # Only an *unnamed* PRIMARY-role channel is reachable via an alias; a
            # radio whose primary has a real name is matched by that name above.
            idx = primary_index
        else:
            log.warning(
                "Meshtastic reply channel %r is not on the radio's channel table "
                "(known channels: %s) — skipping it; the rest of the allowlist still "
                "applies. Check the name (they are case-sensitive) with the "
                "meshtastic_list_channels tool.",
                name,
                _known_names(table) or "<none>",
            )
            continue
        allowed.add(idx)
        resolved[name] = idx

    if not allowed:
        return None, resolved
    return allowed, resolved


def _is_primary(row: dict) -> bool:
    """True for a PRIMARY-role channel with no name of its own."""
    if row.get("name"):
        return False
    role = row.get("role")
    return role == 1 or (isinstance(role, str) and role.upper() == "PRIMARY")


def _known_names(table: list[dict]) -> str:
    parts = []
    for row in table:
        name = row.get("name") or ""
        label = repr(name) if name else "<unnamed primary>"
        parts.append(f"{row.get('index')}={label}")
    return ", ".join(parts)


def should_reply(inbound: dict, *, allowed_channels: set[int] | str | None = None) -> bool:
    """Reply policy: always reply to DMs; reply on a channel only if it's allowed.

    ``allowed_channels``: None = DMs only; a set of indices = DMs + those channels
    (e.g. your private channels, excluding public Primary); ALL_CHANNELS = any channel.
    This keeps the public Primary channel (index 0) silent unless explicitly opted in.
    """
    if inbound["is_dm"]:
        return True
    if allowed_channels is None:
        return False
    if allowed_channels == ALL_CHANNELS:
        return True
    return inbound["channel"] in allowed_channels


def process_inbound(
    packet: dict,
    my_node_id: str | None,
    responder: Responder,
    *,
    allowed_channels: set[int] | str | None = None,
) -> dict | None:
    """End-to-end routing decision for one packet (pure; no I/O).

    Returns None to ignore, or a dict with ``action``:
      - {"action": "skip", "inbound": ...}  — readable but policy says don't reply
      - {"action": "reply", "inbound", "chat_id", "reply", "target"} — should reply
    """
    inbound = inbound_from_packet(packet, my_node_id)
    if inbound is None:
        return None
    if not should_reply(inbound, allowed_channels=allowed_channels):
        return {"action": "skip", "inbound": inbound}
    chat_id = chat_id_for(inbound)
    return {
        "action": "reply",
        "inbound": inbound,
        "chat_id": chat_id,
        "reply": responder(inbound["text"], inbound),
        "target": outbound_target(chat_id),
    }
