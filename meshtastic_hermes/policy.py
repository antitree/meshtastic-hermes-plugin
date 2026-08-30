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
2. **One variable, and the allowlist IS the permission.** A single setting,
   ``MESHTASTIC_TOOL_SEND_CHANNELS``, decides everything about broadcasts: unset
   means no broadcast at all, and a non-empty allowlist authorizes exactly the
   channels it names. There is deliberately no separate "may I broadcast?" switch
   on top of it — a second flag that had to agree with the first only ever produced
   configurations that looked enabled and were not.
3. **No implicit channel 0.** A broadcast must name its channel. There is no
   default, because the default would be the public one.
4. **Primary is opt-in BY NAME, never by wildcard.** The Primary channel's PSK is
   public on a default radio, so it is authorized only by naming it explicitly
   (``Primary``, or its actual name). ``all`` covers every *other* channel and
   deliberately stops short of Primary: a wildcard is a statement about the private
   channels an operator has set up, not consent to transmit in the clear.
5. **Reply policy does NOT authorize tool sends.** ``MESHTASTIC_REPLY_CHANNELS``
   says "you may answer someone who spoke to you here". That is a much smaller
   permission than "you may originate traffic here whenever you decide to", so
   tool sends keep their own separate variable.
6. **Names, not indices.** Channel names are resolved against the radio's channel
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

# Removed in 0.2.0. MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST and
# MESHTASTIC_TOOL_SEND_ALLOW_PRIMARY were separate switches that had to AGREE with
# TOOL_SEND_CHANNELS before anything could be transmitted, which meant the common
# failure was a config that named a channel and still refused to send. They are
# named here only so a stale value in an operator's .env produces a pointed warning
# instead of silently doing nothing. See _warn_removed_vars().
_REMOVED_ENV = (
    "MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST",
    "MESHTASTIC_TOOL_SEND_ALLOW_PRIMARY",
)


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


def _warn_removed_vars() -> None:
    """Tell an operator whose .env still carries the pre-0.2.0 switches.

    Silence here would be the worst outcome: someone who set
    ``MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST=true`` and nothing else would find
    broadcasts refused with no hint that the variable no longer exists.
    """
    for name in _REMOVED_ENV:
        if _env(name):
            logger.warning(
                "%s is no longer used and is ignored. %s alone now authorizes tool "
                "broadcasts: list the channel NAMES the tool may transmit on, e.g. "
                '%s="in.secure". The Primary channel is authorized only by naming it '
                "explicitly.",
                name,
                TOOL_SEND_CHANNELS_ENV,
                TOOL_SEND_CHANNELS_ENV,
            )


def allow_broadcast() -> bool:
    """Whether the tool may originate non-DM channel broadcasts at all.

    True exactly when ``MESHTASTIC_TOOL_SEND_CHANNELS`` names something. The
    allowlist IS the permission — there is no second switch to forget.
    """
    return tool_send_channel_spec() is not None


def tool_send_channel_spec(override=None):
    """Parse the tool-send allowlist into an UNRESOLVED spec.

    Same grammar and same resolution behavior as ``MESHTASTIC_REPLY_CHANNELS``
    (see :func:`meshtastic_hermes.gateway_bridge.parse_channel_spec`), but read from
    its own variable: tool sends are configured SEPARATELY from replies on purpose.

    Numeric entries are accepted for symmetry with the reply allowlist and warn for
    the same reason — an index is a slot, not an identity.

    Returns ``None`` when the variable is unset or empty, which is what "no channel
    broadcasts at all" means now that there is no separate broadcast flag.
    """
    if override is None:
        _warn_removed_vars()
        raw = _env(TOOL_SEND_CHANNELS_ENV)
    else:
        raw = str(override).strip()
    spec = gb.parse_channel_spec(raw or None)
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


def _primary_named(spec, channel_table: list[dict]) -> bool:
    """Whether the allowlist names the PRIMARY channel EXPLICITLY.

    This is the one place ``all`` is not enough. ``ALL_CHANNELS`` is a statement
    about the channels an operator configured; the Primary channel's PSK is public
    on a default radio, so transmitting there has to be written down on purpose.

    Three spellings count, matching how the radio can present Primary:

    - its real name, when the operator gave the primary channel one;
    - an alias (``Primary``/``LongFast``) when the primary is unnamed, which is how
      :func:`gateway_bridge.resolve_channel_spec` already reaches it;
    - its legacy numeric index, which is explicit even though it is a slot.
    """
    if spec is None or spec == gb.ALL_CHANNELS or not isinstance(spec, gb.ChannelSpec):
        return False

    primary = _primary_index(channel_table)
    if primary is None:
        return False
    if primary in spec.indices:
        return True

    # Resolve only the NAMES, so an `all` can never leak in through this path.
    allowed, _resolved = gb.resolve_channel_spec(
        gb.ChannelSpec(names=spec.names), channel_table, log=logger
    )
    return isinstance(allowed, set) and primary in allowed


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


def validate_tool_send(args: dict, channel_table: list[dict] | None, *, channel_spec=None) -> ToolSendTarget:
    """Authorize one ``meshtastic_send_text`` call. Returns the allowed target.

    Raises :class:`ToolSendRejected` (with a ``code``) when the destination is not
    permitted. Pure — mutates nothing and transmits nothing, so the caller can call
    it before any ``iface.sendData``.

    Decision order:

    - ``dest_id`` present → a direct message. ``pki=true`` is required by default,
      because a non-PKI DM is only channel-PSK encrypted and is therefore readable
      by every other holder of that channel's key. A plaintext DM is treated as a
      channel send on its routing channel and must clear the broadcast gates.
    - no ``dest_id`` → a broadcast. It needs an explicit ``channel_index`` or
      ``channel_name`` (never a defaulted 0) naming a channel that
      ``MESHTASTIC_TOOL_SEND_CHANNELS`` lists. That one variable is the whole
      permission, with one carve-out: the Primary channel counts as listed only when
      it is named outright, never via ``all``.
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
    table = channel_table or []
    spec = tool_send_channel_spec(channel_spec)
    if spec is None:
        raise ToolSendRejected(
            "Channel broadcasts from the meshtastic_send_text tool are disabled. This "
            "tool sends PKI direct messages only until "
            f"{TOOL_SEND_CHANNELS_ENV} lists the channel NAME(s) it may transmit on, "
            f'e.g. {TOOL_SEND_CHANNELS_ENV}="in.secure" (see meshtastic_list_channels). '
            "Reply-channel policy does not authorize tool-originated sends.",
            code="broadcast_disabled",
        )

    allowed, _resolved = gb.resolve_channel_spec(spec, table, log=logger)

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
        # Configured, but nothing in it resolved: every name in the allowlist is
        # absent from the radio's channel table. Distinct from "not configured",
        # and a different fix — check the spelling against the radio.
        raise ToolSendRejected(
            f"None of the channels in {TOOL_SEND_CHANNELS_ENV} are on the radio's "
            f"channel table (known channels: {gb._known_names(table) or '<none>'}). "
            "Channel names are case-sensitive; list them with meshtastic_list_channels.",
            code="no_allowed_channels",
        )
    if allowed != gb.ALL_CHANNELS and index not in allowed:
        raise ToolSendRejected(
            f"Channel index {index} is not in {TOOL_SEND_CHANNELS_ENV}; "
            f"allowed indices: {sorted(allowed)}.",
            code="channel_not_allowed",
        )

    primary = _primary_index(table)
    if primary is not None and index == primary and not _primary_named(spec, table):
        # `all` deliberately stops short of Primary. A wildcard says "any channel I
        # set up"; the Primary channel's PSK is public on a default radio, so
        # transmitting there is a separate decision that has to be written down.
        raise ToolSendRejected(
            f"Channel index {index} is the PRIMARY channel, whose key is public on a "
            f"default radio. It is not covered by a wildcard: name it explicitly in "
            f'{TOOL_SEND_CHANNELS_ENV} (e.g. {TOOL_SEND_CHANNELS_ENV}="Primary") to '
            "transmit there.",
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
