"""Bridge between Meshtastic packets and a normalized chat model.

Shared by the Hermes platform adapter ([meshtastic_platform]) and the REPL
simulator, so both map inbound radio text and outbound replies identically. These
are pure functions — no radio and no Hermes imports — so the routing/reply policy
is unit-testable without hardware.

`chat_id` scheme (a stable conversation identifier the agent/gateway keys on):
  - Direct message  -> the peer node id, e.g. "!deadbeef"
  - Channel message -> "ch:<index>",   e.g. "ch:0"
"""

from __future__ import annotations

import logging
import re
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


# ── mention gating ───────────────────────────────────────────────────────
#
# Safety rationale: a channel allowlist alone makes the bot answer EVERY message
# on that channel. Two such bots on the same channel answer each other forever,
# and on a shared, legally regulated RF medium that is an unbounded transmission
# loop, not just noise. Requiring the message to be ADDRESSED to us — by short
# name, long name, or node id, at the START of the text — means an ordinary
# conversation between humans on an allowlisted channel never triggers a
# transmission, and a bot's own reply (which does not open with our name) does
# not bounce back.
#
# DMs are exempt: a direct message is already addressed to this node.

# A mention may be followed by end-of-string, whitespace, or a ':'/',' separator,
# which is consumed along with the mention. This is what stops a short name
# "MES" from matching "MESHNET weather" or "MESHY is down".
_MENTION_TAIL = re.compile(r"^(?:[\s:,]+|$)")


def _mention_candidates(
    short_name: str | None, long_name: str | None, node_id: str | None
) -> list[str]:
    """Every literal string that counts as naming this node (unsorted, deduped).

    The node id is accepted with and without its leading ``!`` so both
    ``!deadbeef weather`` and ``deadbeef weather`` address us.
    """
    out: list[str] = []

    def add(value: Any) -> None:
        text = str(value).strip() if value is not None else ""
        if text and text not in out:
            out.append(text)

    add(short_name)
    add(long_name)
    add(node_id)
    if node_id and str(node_id).startswith("!"):
        add(str(node_id)[1:])
    return out


def match_mention(
    text: str,
    *,
    short_name: str | None = None,
    long_name: str | None = None,
    node_id: str | None = None,
) -> str | None:
    """If *text* STARTS WITH a mention of this node, return the remainder.

    Pure: no radio, no env, no logging. Returns ``None`` when the text does not
    open with a mention (including when no identifier at all is known).

    Matching rules:
      - Case-insensitive for all three identifiers.
      - An optional leading ``@`` is accepted and consumed.
      - The node id matches with or without its leading ``!``.
      - The mention must be followed by end-of-string, whitespace, or a ``:``/``,``
        separator, which is consumed. A mention mid-sentence never matches, and a
        longer word that merely *starts with* an identifier ("MESHNET", "MESHY")
        never matches.
      - The long name is matched literally (case-insensitively), spaces and
        punctuation included — never token-by-token. Names are compared with
        casefolded ``str.startswith``, never interpolated into a pattern, so a
        name containing regex metacharacters ("M.SH", "a+b") matches only itself
        and can never become a wildcard.
      - When several identifiers match, the LONGEST wins, so a long name that
        begins with the short name ("MESHTASTIC Bot" vs "MESH") strips the whole thing
        rather than leaving a stray word behind.

    The returned remainder is stripped of surrounding whitespace, so a bare
    mention with no question returns ``""`` (an empty string, which is falsey but
    NOT ``None``: the caller can and does distinguish "addressed us, said nothing"
    from "did not address us").
    """
    if not text:
        return None
    candidates = _mention_candidates(short_name, long_name, node_id)
    if not candidates:
        return None

    body = text.lstrip()
    if body.startswith("@"):
        body = body[1:].lstrip()
    if not body:
        return None

    lowered = body.lower()
    best: int | None = None
    for cand in candidates:
        if not lowered.startswith(cand.lower()):
            continue
        rest = body[len(cand):]
        if not _MENTION_TAIL.match(rest):
            continue  # a longer word, not a mention: "MESHNET" for short name "MES"
        if best is None or len(cand) > best:
            best = len(cand)

    if best is None:
        return None
    return body[best:].lstrip(" \t:,").strip()


@dataclass(frozen=True)
class Identity:
    """This node's own names, as the radio reports them.

    Any field may be ``None``: the radio's node DB often has not delivered the
    ``user`` record yet in the first seconds after connect, so ``short_name`` and
    ``long_name`` arrive late while ``node_id`` is available immediately.
    """

    node_id: str | None = None
    short_name: str | None = None
    long_name: str | None = None

    @classmethod
    def from_status(cls, status: dict | None) -> Identity:
        """Build from ``ConnectionManager.local_node_identity()`` (or any dict)."""
        status = status or {}
        return cls(
            node_id=status.get("node_id"),
            short_name=status.get("short_name"),
            long_name=status.get("long_name"),
        )

    def __bool__(self) -> bool:
        """True when at least one identifier is known — i.e. gating is possible."""
        return bool(self.node_id or self.short_name or self.long_name)

    @property
    def is_degraded(self) -> bool:
        """True when we can gate, but on fewer identifiers than we would like.

        Node id always works; a human on the mesh will usually type the SHORT
        NAME, so a missing short/long name means real mentions may be missed
        until the node DB catches up. Worth logging, never worth failing open.
        """
        return bool(self) and not (self.short_name and self.long_name)


def apply_mention_gate(
    inbound: dict,
    identity: Identity | None,
    *,
    require_mention: bool = True,
) -> dict | None:
    """Apply mention gating to one inbound message; return it or ``None`` to drop.

    DMs pass through untouched — a direct message is already addressed to this
    node, so no mention is required (and none is stripped).

    On a CHANNEL, with ``require_mention`` on, the text must start with a mention
    of this node (see :func:`match_mention`). When it does, the mention is
    STRIPPED from ``text`` before the agent ever sees it, and the untouched
    original is preserved as ``raw_text`` alongside ``mentioned: True``. When it
    does not, ``None`` is returned and the message is ignored.

    Fail-closed on unknown identity: if the radio has reported no identifier at
    all we cannot tell whether we were addressed, so channel traffic is DROPPED
    rather than answered. We never fall through to "reply to everything".

    A bare mention with nothing after it ("MESH") yields an empty ``text``. It is
    still forwarded: the message WAS addressed to us, and an agent answering
    "yes?" is a better response to being called by name than silence. Callers
    that need a non-empty prompt can check ``inbound["text"]``.
    """
    if inbound.get("is_dm"):
        return inbound
    if not require_mention:
        return inbound
    if not identity:
        return None  # fail closed: no identifier, no way to know we were addressed

    remainder = match_mention(
        inbound.get("text") or "",
        short_name=identity.short_name,
        long_name=identity.long_name,
        node_id=identity.node_id,
    )
    if remainder is None:
        return None

    gated = dict(inbound)
    gated["raw_text"] = inbound.get("text")
    gated["text"] = remainder
    gated["mentioned"] = True
    return gated


def should_reply(
    inbound: dict,
    *,
    allowed_channels: set[int] | str | None = None,
    identity: Identity | None = None,
    require_mention: bool = False,
) -> bool:
    """Reply policy: always reply to DMs; reply on a channel only if it's allowed.

    ``allowed_channels``: None = DMs only; a set of indices = DMs + those channels
    (e.g. your private channels, excluding public Primary); ALL_CHANNELS = any channel.
    This keeps the public Primary channel (index 0) silent unless explicitly opted in.

    ``require_mention`` adds the second gate: on an allowed CHANNEL the message
    must also open with a mention of this node (``identity``). It defaults to
    False *here* so this pure predicate stays backwards compatible; the operator-
    facing default lives in the adapter's ``MESHTASTIC_REQUIRE_MENTION``, which
    defaults to ON. Prefer :func:`apply_mention_gate` when you also want the
    mention stripped from the text.
    """
    if inbound["is_dm"]:
        return True
    if allowed_channels is None:
        return False
    if allowed_channels != ALL_CHANNELS and inbound["channel"] not in allowed_channels:
        return False
    if not require_mention:
        return True
    return apply_mention_gate(inbound, identity, require_mention=True) is not None


def process_inbound(
    packet: dict,
    my_node_id: str | None,
    responder: Responder,
    *,
    allowed_channels: set[int] | str | None = None,
    identity: Identity | None = None,
    require_mention: bool = False,
) -> dict | None:
    """End-to-end routing decision for one packet (pure; no I/O).

    Returns None to ignore, or a dict with ``action``:
      - {"action": "skip", "inbound": ...}  — readable but policy says don't reply
      - {"action": "reply", "inbound", "chat_id", "reply", "target"} — should reply

    With ``require_mention`` on, a channel message that does not open with a
    mention of ``identity`` is a "skip", and one that does has the mention
    stripped from ``inbound["text"]`` before the responder sees it (the original
    is kept as ``inbound["raw_text"]``).
    """
    inbound = inbound_from_packet(packet, my_node_id)
    if inbound is None:
        return None
    if not should_reply(inbound, allowed_channels=allowed_channels):
        return {"action": "skip", "inbound": inbound}
    if require_mention:
        gated = apply_mention_gate(inbound, identity, require_mention=True)
        if gated is None:
            return {"action": "skip", "inbound": inbound}
        inbound = gated
    chat_id = chat_id_for(inbound)
    return {
        "action": "reply",
        "inbound": inbound,
        "chat_id": chat_id,
        "reply": responder(inbound["text"], inbound),
        "target": outbound_target(chat_id),
    }
