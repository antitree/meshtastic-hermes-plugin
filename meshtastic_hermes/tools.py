"""Tool handlers — run when the LLM calls a tool.

Contract (per the Hermes plugin guide): every handler takes ``(args, **kwargs)``,
ALWAYS returns a JSON string, and NEVER raises. Errors are returned as
``{"error": ...}`` JSON so the tool loop keeps running.

The ``meshtastic`` radio stack is a hard dependency (normally installed by pip) but
is still import-guarded inside the connection manager, so handlers that need it
surface a friendly install hint instead of crashing on a bare directory-drop install.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from typing import Any

from .connection import (
    ConnectTargetRejected,
    MeshtasticUnavailable,
    get_manager,
    validate_connect_target,
)
from . import ipc
from .observer import get_observer
from .policy import ToolSendRejected, validate_tool_send
from .privacy import (
    EXPOSE_TRAFFIC_METADATA_ENV,
    RECENT_TEXT_REDACTED_NOTE,
    TRAFFIC_METADATA_REDACTED_NOTE,
    expose_traffic_metadata,
    location_note,
    redact_location,
    redact_messages,
)
from .rate_limit import RateLimited, check_send

logger = logging.getLogger(__name__)

# Meshtastic portnum for plain text messages (portnums.proto TEXT_MESSAGE_APP).
_TEXT_MESSAGE_APP = 1


def _ok(data: Any) -> str:
    return json.dumps(data, default=str)


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra}, default=str)


def _guard(fn: Callable[[dict], Any]) -> Callable[..., str]:
    """Wrap a handler so it always returns JSON and never raises."""

    def wrapper(args: dict, **kwargs: Any) -> str:
        try:
            return fn(args or {})
        except MeshtasticUnavailable as exc:
            return _err(str(exc), code="radio_unavailable")
        except RuntimeError as exc:
            return _err(str(exc))
        except Exception as exc:  # last-resort safety net
            return _err(f"Unexpected error: {exc}", code="internal")

    return wrapper


def _kb():
    """The KB shared with the receive observer (single source of truth)."""
    return get_observer().kb


# ----------------------------------------------------------------------
# Core messaging
# ----------------------------------------------------------------------


@_guard
def connect(args: dict) -> str:
    # Authorize the target BEFORE touching the manager. validate_connect_target()
    # is pure, so a rejected connect cannot overwrite the manager's _host/_port or
    # disturb an existing healthy link — nothing downstream has run yet.
    try:
        host, port = validate_connect_target(args.get("host"), args.get("port"))
    except ConnectTargetRejected as exc:
        return _err(str(exc), code=exc.code)
    status = get_manager().connect(host, port)
    # "status" mirrors the three-state `state` field (it used to be hardcoded
    # "connected", which lied while the node was still coming up).
    return _ok({"status": status.get("state", "connected"), **status})


@_guard
def disconnect(args: dict) -> str:
    return _ok(get_manager().disconnect())


@_guard
def send_text(args: dict) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return _err("No text provided.")

    mgr = get_manager()

    # Authorize the destination BEFORE anything touches the radio. validate_tool_send()
    # is pure, so a rejected send cannot transmit: nothing downstream has run yet.
    # channel_index is NOT defaulted to 0 here — a broadcast must name its channel,
    # because the channel a default would pick is the public Primary.
    try:
        target = validate_tool_send(args, mgr.channel_table())
    except ToolSendRejected as exc:
        return _err(str(exc), code=exc.code)

    channel_index = target.channel_index
    dest_id = target.dest_id
    pki = target.pki
    # Reliable delivery by default: the firmware retries and reports ack/nak. Helps
    # messages survive lossy multi-hop links.
    want_ack = bool(args.get("want_ack", True))
    # Block for the ack/nak by default for DIRECTED messages (a single recipient, so
    # the ack is meaningful); broadcasts have no single recipient, so don't block.
    wait_ack = bool(args.get("wait_ack", bool(dest_id) and want_ack))
    ack_timeout = float(args.get("ack_timeout", 15.0))

    # (pki without dest_id is rejected by validate_tool_send above.)
    iface = mgr.iface

    # Airtime budget. Checked AFTER policy authorizes the destination and after the
    # interface is resolved (so an unreachable radio does not burn budget), but
    # BEFORE any sendData. This is the single choke point every outbound path shares:
    # direct tool calls, adapter replies (which call this function once per chunked
    # part — so a multi-part reply spends one token per TRANSMITTED PACKET, not one
    # per logical reply), and the bridge harness `--send`. See rate_limit.py.
    # `_continuation` marks a later chunk of a reply already in flight. It still costs
    # a token — chunks are real airtime — but is not held behind the per-destination
    # cooldown, which spaces conversational TURNS, not the parts of one answer.
    try:
        check_send(
            dest_id=dest_id,
            channel_index=None if dest_id else channel_index,
            continuation=bool(args.get("_continuation", False)),
        )
    except RateLimited as exc:
        logger.warning(
            "Transmit refused by rate limiter (%s scope): %s retry_after_s=%s",
            exc.scope,
            exc,
            exc.retry_after_s,
        )
        return _err(
            exc.code,
            retry_after_s=exc.retry_after_s,
            scope=exc.scope,
            detail=str(exc),
        )

    # We send everything via sendData(portNum=TEXT_MESSAGE_APP) — identical on-air to
    # sendText — because only sendData exposes onResponseAckPermitted, needed to have
    # the routing ACK invoke our callback. pkiEncrypted toggles end-to-end encryption.
    send_kwargs: dict[str, Any] = {
        "portNum": _TEXT_MESSAGE_APP,
        "channelIndex": channel_index,
        "wantAck": want_ack,
        "pkiEncrypted": pki,
    }
    if dest_id:
        send_kwargs["destinationId"] = dest_id

    ack: dict[str, Any] | None = None
    if want_ack and wait_ack:
        event = threading.Event()
        captured: dict[str, Any] = {}

        def _on_response(resp: dict) -> None:
            routing = (resp.get("decoded") or {}).get("routing") or {}
            captured["reason"] = routing.get("errorReason", "NONE")
            captured["from"] = resp.get("fromId")
            event.set()

        iface.sendData(
            text.encode("utf-8"),
            onResponse=_on_response,
            onResponseAckPermitted=True,
            **send_kwargs,
        )
        if event.wait(ack_timeout):
            reason = captured.get("reason", "NONE")
            ack = {
                "status": "delivered" if reason == "NONE" else "failed",
                "reason": reason,
                "from": captured.get("from"),
            }
        else:
            ack = {"status": "no_ack", "reason": "TIMEOUT", "timeout_s": ack_timeout}
    else:
        iface.sendData(text.encode("utf-8"), **send_kwargs)

    return _ok(
        {
            "sent": True,
            "encryption": "pki" if pki else "channel",
            "want_ack": want_ack,
            "ack": ack,
            "text": text,
            "channel_index": channel_index,
            "dest_id": dest_id,
        }
    )


@_guard
def meshagatchi_submit_event(args: dict) -> str:
    """Submit a structured event through the live Meshagatchi sidecar."""
    socket_path = (os.getenv("MESHTASTIC_MESHAGATCHI_SOCKET")
                   or os.getenv("MESHTASTIC_IPC_SOCKET") or "").strip()
    if not socket_path:
        return _err("Meshagatchi event interface is not configured", code="unavailable")
    response = ipc.round_trip(socket_path, {"op": "event.submit", "event": args})
    return _ok(response)


@_guard
def recent_messages(args: dict) -> str:
    """Recent decoded text — REDACTED to metadata unless plaintext exposure is on.

    The observer's buffer keeps the full bodies (the gateway needs them to answer a
    message); the gate is here, on the way out to the model. Redacted rows still
    carry sender/recipient/channel/timestamp plus a length and short hash, so "three
    messages arrived on channel 1 just now" remains answerable without disclosing
    what anyone said.
    """
    limit = int(args.get("limit", 20))
    rows, redacted = redact_messages(get_observer().recent_messages(limit))
    payload: dict[str, Any] = {"count": len(rows), "messages": rows}
    if redacted:
        payload["text_redacted"] = True
        payload["note"] = RECENT_TEXT_REDACTED_NOTE
    return _ok(payload)


# ----------------------------------------------------------------------
# Network inspection
# ----------------------------------------------------------------------


def _node_summary(node_id: str, node: dict[str, Any]) -> dict[str, Any]:
    user = node.get("user") or {}
    pos = node.get("position") or {}
    metrics = node.get("deviceMetrics") or {}
    summary = {
        "id": node_id,
        "short_name": user.get("shortName"),
        "long_name": user.get("longName"),
        "hw_model": user.get("hwModel"),
        "role": user.get("role"),
        "snr": node.get("snr"),
        "last_heard": node.get("lastHeard"),
        "hops_away": node.get("hopsAway"),
        "battery": metrics.get("batteryLevel"),
        "lat": pos.get("latitude"),
        "lon": pos.get("longitude"),
    }
    # Coordinates are dropped on the way OUT, not read selectively above, so this
    # cannot be defeated by a future field being added to the dict without the
    # author remembering the gate. redact_location() is the same function the KB
    # path uses — one gate, both sources.
    return redact_location(summary)


@_guard
def list_nodes(args: dict) -> str:
    limit = int(args.get("limit", 50))
    nodes = get_manager().iface.nodes or {}
    out = [_node_summary(nid, n) for nid, n in list(nodes.items())[:limit]]
    return _ok(location_note({"count": len(out), "nodes": out}))


@_guard
def node_info(args: dict) -> str:
    mgr = get_manager()
    nodes = mgr.iface.nodes or {}
    node_id = args.get("node_id") or mgr.my_node_id()
    if not node_id:
        return _err("Could not determine node id.")
    node = nodes.get(node_id)
    if node is None:
        return _err(f"Node {node_id} not found in the radio's node DB.", node_id=node_id)
    return _ok(location_note(_node_summary(node_id, node)))


@_guard
def list_channels(args: dict) -> str:
    local = get_manager().iface.localNode
    channels = getattr(local, "channels", None) or []
    out = []
    for idx, ch in enumerate(channels):
        settings = getattr(ch, "settings", None)
        role = getattr(ch, "role", None)
        # role: 0=DISABLED, 1=PRIMARY, 2=SECONDARY
        if role == 0:
            continue
        out.append(
            {
                "index": idx,
                "name": getattr(settings, "name", "") or ("Primary" if role == 1 else f"ch{idx}"),
                "role": {0: "DISABLED", 1: "PRIMARY", 2: "SECONDARY"}.get(role, str(role)),
                "has_psk": bool(getattr(settings, "psk", b"")),
            }
        )
    return _ok({"count": len(out), "channels": out})


@_guard
def device_metrics(args: dict) -> str:
    mgr = get_manager()
    node_id = mgr.my_node_id()
    nodes = mgr.iface.nodes or {}
    node = nodes.get(node_id, {}) if node_id else {}
    metrics = node.get("deviceMetrics") or {}
    pos = node.get("position") or {}
    # This node's OWN position. Still gated: "where is the gateway" locates the
    # operator just as precisely as any peer, and the answer travels wherever the
    # model's output travels.
    return _ok(
        location_note(
            redact_location(
                {
                    "node_id": node_id,
                    "battery_level": metrics.get("batteryLevel"),
                    "voltage": metrics.get("voltage"),
                    "channel_utilization": metrics.get("channelUtilization"),
                    "air_util_tx": metrics.get("airUtilTx"),
                    "uptime_seconds": metrics.get("uptimeSeconds"),
                    "lat": pos.get("latitude"),
                    "lon": pos.get("longitude"),
                    "altitude": pos.get("altitude"),
                }
            )
        )
    )


# ----------------------------------------------------------------------
# Knowledge base
# ----------------------------------------------------------------------


def _metadata_gated(payload: dict) -> str:
    """Refuse a detailed reconnaissance view, returning the counts instead.

    Not a bare error: the summary still answers "is the mesh busy, how many nodes
    have I heard", which is operationally useful and names nobody. What is withheld
    is the per-node, per-packet, per-relationship detail that turns that into a
    social graph of the people on the mesh.
    """
    return _ok(
        {
            **payload,
            "traffic_metadata_redacted": True,
            "note": TRAFFIC_METADATA_REDACTED_NOTE,
            "required_env": EXPOSE_TRAFFIC_METADATA_ENV,
        }
    )


@_guard
def kb_summary(args: dict) -> str:
    """Aggregate counts. Available by default — it names no node and locates nobody.

    Except ``top_talkers``, which DOES name specific nodes and ranks them by how
    much they transmit. That is the first step of the same reconnaissance the
    detailed tools do, so it rides the traffic-metadata gate; the counts around it
    do not.
    """
    summary = _kb().summary()
    if not expose_traffic_metadata():
        summary = {k: v for k, v in summary.items() if k != "top_talkers"}
        summary["top_talkers_redacted"] = True
    return _ok(summary)


@_guard
def kb_nodes(args: dict) -> str:
    """Per-node KB rows — gated, and location-redacted even when ungated.

    Two gates apply independently, because the KB is the SECOND path to the same
    sensitive fields: the ``nodes`` table carries its own ``lat``/``lon`` columns,
    populated separately from the radio's live node DB. Enabling traffic metadata
    unlocks the rows; it does NOT unlock coordinates — only
    MESHTASTIC_EXPOSE_LOCATION does that, on this path exactly as on the live one.
    """
    if not expose_traffic_metadata():
        return _metadata_gated({"node_count": _kb().summary().get("nodes", 0)})
    limit = int(args.get("limit", 50))
    sort = args.get("sort", "last_seen")
    rows = redact_location(_kb().nodes(limit=limit, sort=sort))
    return _ok(location_note({"count": len(rows), "nodes": rows}))


@_guard
def kb_interactions(args: dict) -> str:
    """Per-packet interaction records — who addressed whom, when, on which channel.

    Individually dull, collectively a timeline of everyone in radio range. Gated.
    """
    limit = int(args.get("limit", 100))
    node_id = args.get("node_id")
    since = args.get("since")
    if not expose_traffic_metadata():
        rows = _kb().interactions(node_id=node_id, since=since, limit=limit)
        # The COUNT is not the disclosure; the records are. Returning it keeps
        # "has this node been active since X" answerable.
        return _metadata_gated({"count": len(rows), "interactions": []})
    rows = redact_location(_kb().interactions(node_id=node_id, since=since, limit=limit))
    return _ok({"count": len(rows), "interactions": rows})


@_guard
def kb_neighbors(args: dict) -> str:
    """Inferred direct contacts of a node — the sharpest reconnaissance view. Gated.

    This is an explicit social graph: it answers "who does this person talk to",
    ranked, from traffic nobody consented to have analyzed.
    """
    node_id = args.get("node_id")
    if not node_id:
        return _err("node_id is required.")
    limit = int(args.get("limit", 50))
    if not expose_traffic_metadata():
        peers = _kb().neighbors(node_id, limit=limit)
        return _metadata_gated(
            {"node_id": node_id, "neighbor_count": len(peers), "neighbors": []}
        )
    rows = redact_location(_kb().neighbors(node_id, limit=limit))
    return _ok({"node_id": node_id, "count": len(rows), "neighbors": rows})
