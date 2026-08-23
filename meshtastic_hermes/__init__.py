"""Meshtastic Hermes plugin — registration.

Wires tool schemas to handlers, registers session lifecycle hooks, a `/meshtastic`
slash command, and a `hermes meshtastic` CLI command. Hermes calls ``register(ctx)``
exactly once at startup.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from . import schemas, tools
from .connection import get_manager
from .observer import get_observer

__version__ = "0.1.5"

logger = logging.getLogger(__name__)

# (schema, handler) pairs registered under the "meshtastic" toolset.
_TOOLS = [
    (schemas.CONNECT, tools.connect),
    (schemas.DISCONNECT, tools.disconnect),
    (schemas.SEND_TEXT, tools.send_text),
    (schemas.RECENT_MESSAGES, tools.recent_messages),
    (schemas.LIST_NODES, tools.list_nodes),
    (schemas.NODE_INFO, tools.node_info),
    (schemas.LIST_CHANNELS, tools.list_channels),
    (schemas.DEVICE_METRICS, tools.device_metrics),
    (schemas.KB_SUMMARY, tools.kb_summary),
    (schemas.KB_NODES, tools.kb_nodes),
    (schemas.KB_INTERACTIONS, tools.kb_interactions),
    (schemas.KB_NEIGHBORS, tools.kb_neighbors),
]


# ----------------------------------------------------------------------
# Hooks
# ----------------------------------------------------------------------


def _on_session_start(session_id=None, **kwargs):
    """Auto-connect when MESHTASTIC_HOST is set, so observation starts immediately."""
    host = os.environ.get("MESHTASTIC_HOST")
    if not host or get_manager().is_connected():
        return
    try:
        get_manager().connect(host)
        logger.info("Auto-connected to Meshtastic node %s", host)
    except Exception as exc:
        logger.warning("Meshtastic auto-connect failed: %s", exc)


def _on_session_end(session_id=None, **kwargs):
    # Intentionally does NOT disconnect: the radio link is a process-wide singleton
    # shared with the platform-adapter gateway, and the observer must keep filling the
    # knowledge base between sessions. Disconnecting here would tear down the adapter's
    # connection. The connection is released when the process exits.
    return


# ----------------------------------------------------------------------
# Slash command: /meshtastic
# ----------------------------------------------------------------------


def _render_connection(status: dict) -> str:
    """Human-readable one-liner for a status dict, honoring the three states.

    Falls back to the legacy boolean when ``state`` is absent (e.g. a status record
    written by an older gateway build).
    """
    host = status.get("host")
    state = status.get("state")
    if state is None:
        state = "connected" if status.get("connected") else "disconnected"
    if state == "connected":
        return f"connected to {host}"
    if state == "connecting":
        return f"connecting to {host}… (retrying; a booting node refusing TCP is normal)"
    return "disconnected"


def _handle_slash(raw_args: str) -> str:
    if raw_args.strip() == "help":
        return "Usage: /meshtastic — show connection status and knowledge-base summary."
    status = get_manager().status()
    kb = get_observer().kb.summary()
    lines = [
        f"Connection: {_render_connection(status)}",
        f"Local node: {status['node_id'] or 'unknown'}",
        f"KB: {kb['nodes']} nodes, {kb['packets']} packets "
        f"({kb['encrypted_packets']} encrypted), {kb['channels_seen']} channels",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Status helpers
# ----------------------------------------------------------------------


def _normalize_platform_state(state) -> str:
    """Map a Hermes platform state onto our three connection states."""
    if state == "connected":
        return "connected"
    if state in {"connecting", "reconnecting", "starting", "initializing"}:
        return "connecting"
    return "disconnected"


def _local_status() -> dict:
    status = get_manager().status()
    return {**status, "source": "process_local"}


def _gateway_runtime_status() -> dict | None:
    """Read the live Hermes gateway's Meshtastic platform state, when available.

    `hermes meshtastic status` runs in a short-lived CLI process. The radio link,
    however, is owned by the long-running gateway process, so the CLI process-local
    ConnectionManager normally reports disconnected even while the gateway is
    connected. Prefer Hermes' runtime status file when it belongs to a live process.
    """
    try:
        from gateway.status import read_runtime_status, runtime_status_pid_is_live
    except Exception:
        return None

    record = read_runtime_status()
    if not record or not runtime_status_pid_is_live(record):
        return None
    platforms = record.get("platforms") or {}
    platform = platforms.get("meshtastic")
    if not isinstance(platform, dict):
        return None
    state = platform.get("state")
    return {
        "connected": state == "connected",
        # Normalize Hermes' own platform state onto our three states. Hermes uses
        # richer platform states ("connecting", "reconnecting", "error", ...); anything
        # that is still actively coming up maps to `connecting`.
        "state": _normalize_platform_state(state),
        "host": os.environ.get("MESHTASTIC_HOST"),
        "node_id": platform.get("node_id"),
        "true_node_id": platform.get("true_node_id") or platform.get("node_id"),
        "node_num": platform.get("node_num"),
        "short_name": platform.get("short_name"),
        "long_name": platform.get("long_name"),
        "source": "gateway_runtime",
        "gateway_state": record.get("gateway_state"),
        "platform_state": state,
        "error_code": platform.get("error_code"),
        "error_message": platform.get("error_message"),
        "updated_at": platform.get("updated_at"),
        "identity_updated_at": platform.get("identity_updated_at"),
        "writer_pid": platform.get("writer_pid"),
    }


def _status_for_cli() -> dict:
    return _gateway_runtime_status() or _local_status()


# ----------------------------------------------------------------------
# CLI command: hermes meshtastic <status|kb-summary>
# ----------------------------------------------------------------------


def _cli_handler(args):
    sub = getattr(args, "meshtastic_command", None)
    if sub == "kb-summary":
        print(json.dumps(get_observer().kb.summary(), indent=2, default=str))
    elif sub == "status":
        print(json.dumps(_status_for_cli(), indent=2, default=str))
    else:
        print("Usage: hermes meshtastic <status|kb-summary>")


def _setup_argparse(subparser):
    subs = subparser.add_subparsers(dest="meshtastic_command")
    subs.add_parser("status", help="Show connection status")
    subs.add_parser("kb-summary", help="Show knowledge-base summary")
    subparser.set_defaults(func=_cli_handler)


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------


def register(ctx):
    """Entry point called once by Hermes at startup."""
    from .connection import enable_debug_logging

    enable_debug_logging()  # honors MESHTASTIC_DEBUG

    for schema, handler in _TOOLS:
        ctx.register_tool(
            name=schema["name"],
            toolset="meshtastic",
            schema=schema,
            handler=handler,
        )

    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)

    # Optional surfaces — guard so older Hermes builds without these APIs still load.
    if hasattr(ctx, "register_command"):
        ctx.register_command(
            "meshtastic",
            handler=_handle_slash,
            description="Show Meshtastic connection status and KB summary",
        )
    if hasattr(ctx, "register_cli_command"):
        ctx.register_cli_command(
            name="meshtastic",
            help="Manage the Meshtastic plugin",
            setup_fn=_setup_argparse,
            handler_fn=_cli_handler,
        )

    # Bundle skills shipped under skills/<name>/SKILL.md (loaded as `meshtastic:<name>`).
    skills_dir = Path(__file__).parent / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                ctx.register_skill(child.name, skill_md)

    logger.info("meshtastic plugin registered (%d tools)", len(_TOOLS))
