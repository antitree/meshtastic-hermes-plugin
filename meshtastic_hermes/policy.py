"""Transmit policy for the ``meshtastic_send_text`` TOOL — remediation item 1.

The platform adapter gates *automatic replies* (channel allowlist, mention gating,
sender authorization). None of that applied to the tool the model calls directly:
``meshtastic_send_text`` accepted arbitrary ``text``/``channel_index``/``dest_id``
and handed them straight to ``iface.sendData``. Worse, an omitted ``channel_index``
silently became ``0`` — the *public* Primary channel — so a model that simply forgot
the argument broadcast in the clear to every radio in range.

This module is the tool-layer gate. It is PURE: it takes the args dict and a
plain-data channel table (``ConnectionManager.channel_table()``) and returns the
authorized destination or raises. Nothing here touches a radio, so it is fully
testable without hardware, and callers can validate *before* transmitting.

Design rules, in the order they matter:

1. **Fail closed.** With nothing configured, only a PKI direct message is allowed.
   A DM is point-to-point and end-to-end encrypted; a channel send is a broadcast
   to everyone holding that channel's key.
2. **No implicit channel 0.** A broadcast must name its channel. There is no
   default, because the default would be the public one.
3. **Reply policy does NOT authorize tool sends.** ``MESHTASTIC_REPLY_CHANNELS``
   says "you may answer someone who spoke to you here". That is a much smaller
   permission than "you may originate traffic here whenever you decide to", so
   tool sends get their own separate variables.
4. **Names, not indices.** Channel names are resolved against the radio's channel
   table via :func:`meshtastic_hermes.gateway_bridge.resolve_channel_spec` — the
   same machinery the reply allowlist uses. An index is a radio SLOT, not a channel
   identity, so a numeric entry warns exactly as it does for reply channels.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from . import gateway_bridge as gb

logger = logging.getLogger(__name__)

# Truthy spellings, mirroring connection.allow_dynamic_hosts() / MESHTASTIC_REPLY_ALL.
_TRUTHY = {"1", "true", "yes", "on"}

TOOL_SEND_CHANNELS_ENV = "MESHTASTIC_TOOL_SEND_CHANNELS"
TOOL_SEND_ALLOW_PRIMARY_ENV = "MESHTASTIC_TOOL_SEND_ALLOW_PRIMARY"
TOOL_SEND_ALLOW_BROADCAST_ENV = "MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST"


class ToolSendRejected(ValueError):
    """Raised when a tool send is not permitted by policy.

    Carries a machine-readable ``code`` so the tool layer returns a stable JSON
    error shape instead of callers parsing prose (same contract as
    :class:`meshtastic_hermes.connection.ConnectTargetRejected`).
    """

    def __init__(self, message: str, code: str = "send_not_allowed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ToolSendTarget:
    """An AUTHORIZED destination for one tool send.

    ``channel_index`` is only ever an index that policy actually resolved; it is
    never a default. For a DM it is the routing slot the caller asked for (or 0 when
    they asked for nothing), which for a PKI message is not the encryption key.
    """

    dest_id: str | None
    channel_index: int
    pki: bool

    @property
    def is_dm(self) -> bool:
        return bool(self.dest_id)


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _flag(name: str) -> bool:
    """A policy flag is on ONLY for an explicit truthy value — a typo fails closed."""
    return _env(name).lower() in _TRUTHY


def allow_broadcast() -> bool:
    """Whether the tool may originate non-DM channel broadcasts at all."""
    return _flag(TOOL_SEND_ALLOW_BROADCAST_ENV)


def allow_primary() -> bool:
    """Whether the tool may broadcast on the PRIMARY channel specifically.

    Primary is the channel whose PSK is public on a default radio, so it gets its
    own switch on top of :func:`allow_broadcast`: an operator who opens up a private
    channel has not thereby opened up the public one.
    """
    return _flag(TOOL_SEND_ALLOW_PRIMARY_ENV)


def tool_send_channel_spec():
    """Parse ``MESHTASTIC_TOOL_SEND_CHANNELS`` into an UNRESOLVED spec.

    Same grammar and same resolution behavior as ``MESHTASTIC_REPLY_CHANNELS``
    (see :func:`meshtastic_hermes.gateway_bridge.parse_channel_spec`), but read from
    its own variable: tool sends are configured SEPARATELY from replies on purpose.

    Numeric entries are accepted for symmetry with the reply allowlist and warn for
    the same reason — an index is a slot, not an identity.
    """
    spec = gb.parse_channel_spec(_env(TOOL_SEND_CHANNELS_ENV) or None)
    if isinstance(spec, gb.ChannelSpec) and spec.indices:
        logger.warning(
            "%s uses numeric channel index/indices %s. Indices are radio SLOTS, not "
            "channel identities — editing or reordering channels silently repoints "
            "them, which can transmit on the wrong (possibly public) channel. "
            'Configure channel NAMES instead, e.g. %s="in.secure" '
            "(see meshtastic_list_channels).",
            TOOL_SEND_CHANNELS_ENV,
            sorted(spec.indices),
            TOOL_SEND_CHANNELS_ENV,
        )
    return spec


def allowed_tool_send_channels(channel_table: list[dict] | None):
    """Resolve the tool-send allowlist against the radio's channel table.

    Returns the same three-valued shape ``resolve_channel_spec`` produces:
    ``None`` (no channels allowed), ``set[int]``, or ``gb.ALL_CHANNELS``.
    """
    allowed, _resolved = gb.resolve_channel_spec(
        tool_send_channel_spec(), channel_table, log=logger
    )
    return allowed


def _primary_index(channel_table: list[dict] | None) -> int | None:
    """Index of the PRIMARY-role channel, named or not, or None if unknown."""
    for row in channel_table or []:
        role = row.get("role")
        if role == 1 or (isinstance(role, str) and role.upper() == "PRIMARY"):
            idx = row.get("index")
            if isinstance(idx, int):
                return idx
    return None


def _coerce_channel_index(raw: Any) -> int:
    if isinstance(raw, bool):  # bool is an int subclass — never a channel
        raise ToolSendRejected("channel_index must be an integer.", code="invalid_channel")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ToolSendRejected(
            f"channel_index {raw!r} is not an integer.", code="invalid_channel"
        ) from None
    if value < 0:
        raise ToolSendRejected(
            f"channel_index {value} is negative.", code="invalid_channel"
        )
    return value


def validate_tool_send(args: dict, channel_table: list[dict] | None) -> ToolSendTarget:
    """Authorize one ``meshtastic_send_text`` call. Returns the allowed target.

    Raises :class:`ToolSendRejected` (with a ``code``) when the destination is not
    permitted. Pure — mutates nothing and transmits nothing, so the caller can call
    it before any ``iface.sendData``.

    Decision order:

    - ``dest_id`` present → a direct message. ``pki=true`` is required by default,
      because a non-PKI DM is only channel-PSK encrypted and is therefore readable
      by every other holder of that channel's key. A plaintext DM is treated as a
      channel send on its routing channel and must clear the broadcast gates.
    - no ``dest_id`` → a broadcast. It needs ``MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST``,
      an explicit ``channel_index`` or channel name (never a defaulted 0), that
      channel on the ``MESHTASTIC_TOOL_SEND_CHANNELS`` allowlist, and — if it is the
      Primary channel — ``MESHTASTIC_TOOL_SEND_ALLOW_PRIMARY`` as well.
    """
    args = args or {}
    dest_id = args.get("dest_id") or None
    if dest_id is not None and not isinstance(dest_id, str):
        raise ToolSendRejected(
            f"dest_id must be a string, got {type(dest_id).__name__}.", code="invalid_dest"
        )
    pki = bool(args.get("pki", False))

    if pki and not dest_id:
        raise ToolSendRejected(
            "pki=true requires dest_id — public-key encryption is point-to-point.",
            code="pki_requires_dest",
        )

    # A channel name is the preferred way to target a broadcast; an index still works
    # but is a slot, not an identity (see module docstring).
    channel_name = args.get("channel_name")
    raw_index = args.get("channel_index")

    if dest_id and pki:
        # PKI DM: end-to-end to one node. The channel is only a routing slot and is
        # NOT the encryption key, so it needs no allowlist entry.
        index = _coerce_channel_index(raw_index) if raw_index is not None else 0
        return ToolSendTarget(dest_id=dest_id, channel_index=index, pki=True)

    if dest_id and not pki:
        raise ToolSendRejected(
            f"Direct message to {dest_id} refused: pki=true is required for tool-sent "
            "direct messages. Without it the message is only channel-PSK encrypted and "
            "is readable by everyone holding that channel's key.",
            code="dm_requires_pki",
        )

    # ── broadcast ────────────────────────────────────────────────────────────
    if not allow_broadcast():
        raise ToolSendRejected(
            "Channel broadcasts from the meshtastic_send_text tool are disabled. This "
            "tool sends PKI direct messages only unless "
            f"{TOOL_SEND_ALLOW_BROADCAST_ENV}=true is set (and the target channel is "
            f"listed in {TOOL_SEND_CHANNELS_ENV}). Reply-channel policy does not "
            "authorize tool-originated sends.",
            code="broadcast_disabled",
        )

    table = channel_table or []
    allowed = allowed_tool_send_channels(table)

    if channel_name is not None and str(channel_name).strip():
        index = _resolve_name(str(channel_name).strip(), table)
    elif raw_index is not None:
        index = _coerce_channel_index(raw_index)
    else:
        # The sharp edge this item exists to remove: no silent default to channel 0.
        raise ToolSendRejected(
            "A channel broadcast must name its channel: pass channel_name (preferred) "
            "or channel_index. There is no default channel — defaulting would send on "
            "channel 0, the public Primary.",
            code="channel_required",
        )

    if allowed is None:
        raise ToolSendRejected(
            f"No tool-send channels are configured. Set {TOOL_SEND_CHANNELS_ENV} to the "
            "channel NAME(s) this tool may transmit on (see meshtastic_list_channels).",
            code="no_allowed_channels",
        )
    if allowed != gb.ALL_CHANNELS and index not in allowed:
        raise ToolSendRejected(
            f"Channel index {index} is not in {TOOL_SEND_CHANNELS_ENV}; "
            f"allowed indices: {sorted(allowed)}.",
            code="channel_not_allowed",
        )

    primary = _primary_index(table)
    if primary is not None and index == primary and not allow_primary():
        raise ToolSendRejected(
            f"Channel index {index} is the PRIMARY channel, whose key is public on a "
            f"default radio. Sending there from a tool requires "
            f"{TOOL_SEND_ALLOW_PRIMARY_ENV}=true in addition to the channel allowlist.",
            code="primary_not_allowed",
        )

    return ToolSendTarget(dest_id=None, channel_index=index, pki=False)


def _resolve_name(name: str, channel_table: list[dict]) -> int:
    """Map a channel NAME to its index using the radio's channel table.

    Reuses :func:`gateway_bridge.resolve_channel_spec` so name matching (including
    the Primary aliases for an unnamed primary channel) behaves identically to the
    reply allowlist rather than being reimplemented here.
    """
    _allowed, resolved = gb.resolve_channel_spec(
        gb.ChannelSpec(names=(name,)), channel_table, log=logger
    )
    if name not in resolved:
        raise ToolSendRejected(
            f"Channel {name!r} is not on the radio's channel table "
            f"(known channels: {gb._known_names(channel_table) or '<none>'}). Channel "
            "names are case-sensitive; list them with meshtastic_list_channels.",
            code="unknown_channel",
        )
    return resolved[name]
