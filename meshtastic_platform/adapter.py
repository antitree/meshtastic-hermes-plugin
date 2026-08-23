"""Meshtastic platform adapter for Hermes Agent (kind: platform).

Makes a Meshtastic LoRa mesh a bidirectional gateway channel: inbound text
messages drive the agent, and the agent's replies are sent back over the radio.
Mirrors the structure of the bundled IRC adapter.

The agent-facing reply policy is DMs-only by default (reply to direct messages
addressed to us), which avoids channel spam and bot-to-bot loops.

Encryption: replies to a DM go out end-to-end (PKI) to the sender's node; replies
on a channel use that channel's key. We never read or reply to traffic we cannot
decrypt.

`gateway.platforms.base` / `gateway.config` only exist inside the Hermes runtime,
so they are imported lazily; outside Hermes this module still imports (the adapter
class and registration simply become no-ops), which keeps it lint/test-friendly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# This adapter imports its sibling package `meshtastic_hermes` (for gateway_bridge,
# connection, tools). When the project is pip-installed both packages are on sys.path
# and this is a no-op. When it's loaded as a *directory-drop* plugin (e.g. the repo
# cloned into ~/.hermes/plugins/), the sibling package lives one level up (repo root)
# and is NOT importable by default — add the repo root so `import meshtastic_hermes`
# works in both layouts.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:  # Available only inside the Hermes gateway runtime.
    from gateway.config import Platform
    from gateway.platforms.base import (
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )

    _HAVE_GATEWAY = True
except Exception:  # pragma: no cover - exercised only outside Hermes
    _HAVE_GATEWAY = False


# Meshtastic text payloads are tiny (~237 bytes max). Stay under it and cap how many
# parts a single reply may flood onto the slow mesh.
_MAX_MESH_BYTES = 200
_MAX_PARTS = 5


def _split_text(text: str, max_bytes: int = _MAX_MESH_BYTES) -> list[str]:
    """Split text into chunks whose UTF-8 length is <= max_bytes, preferring word
    boundaries; hard-splits any single token that is itself too long."""
    text = (text or "").strip()
    if not text:
        return []
    out: list[str] = []
    cur = ""
    for word in text.split():
        cand = f"{cur} {word}".strip() if cur else word
        if len(cand.encode("utf-8")) <= max_bytes:
            cur = cand
            continue
        if cur:
            out.append(cur)
            cur = ""
        if len(word.encode("utf-8")) > max_bytes:
            b = word.encode("utf-8")
            while b:
                piece = b[:max_bytes]
                while piece:  # back off to a valid UTF-8 boundary
                    try:
                        out.append(piece.decode("utf-8"))
                        break
                    except UnicodeDecodeError:
                        piece = piece[:-1]
                b = b[len(piece):]
        else:
            cur = word
    if cur:
        out.append(cur)
    return out


def _channel_spec_from_env():
    """Parse the channel reply-allowlist from env into an UNRESOLVED spec.

    MESHTASTIC_REPLY_ALL=true            -> every channel.
    MESHTASTIC_REPLY_CHANNELS="in.secure" -> DMs + the channel with that NAME.
    MESHTASTIC_REPLY_CHANNELS="1,2"      -> DMs + those channel indices (legacy).
    Neither                              -> DMs only.

    Names cannot be turned into channel indices without the radio's channel table,
    so this stays pure and the adapter resolves it in ``connect()`` — every time,
    so a rename or reorder on the radio is picked up rather than cached forever.
    """
    from meshtastic_hermes import gateway_bridge as gb

    if os.getenv("MESHTASTIC_REPLY_ALL", "").lower() in {"1", "true", "yes"}:
        return gb.ALL_CHANNELS
    spec = gb.parse_channel_spec(os.getenv("MESHTASTIC_REPLY_CHANNELS"))
    if isinstance(spec, gb.ChannelSpec) and spec.indices:
        logger.warning(
            "MESHTASTIC_REPLY_CHANNELS uses numeric channel index/indices %s. Indices "
            "are radio SLOTS, not channel identities — editing or reordering channels "
            "silently repoints them, which can transmit replies on the wrong (possibly "
            "public) channel. Configure channel NAMES instead, e.g. "
            "MESHTASTIC_REPLY_CHANNELS=\"in.secure\" (see meshtastic_list_channels).",
            sorted(spec.indices),
        )
    return spec


def _allowed_channels_from_env():
    """Back-compat helper: the parsed spec, resolved against nothing.

    Retained because ``register()`` logs the configured policy before any radio
    connection exists. Names show up here unresolved (that is the point — there is
    no channel table yet); the adapter resolves them in ``connect()``.
    """
    return _channel_spec_from_env()


def _manager_status_connected(status) -> bool:
    """Return whether ConnectionManager.connect() produced a usable radio link.

    Deliberately still keyed off the boolean: `connecting` is NOT a usable link, so
    the adapter must not report healthy while the supervisor is still retrying.
    """
    return bool(isinstance(status, dict) and status.get("connected"))


def _manager_status_state(status) -> str:
    """The three-state connection state from a manager status dict."""
    if not isinstance(status, dict):
        return "disconnected"
    state = status.get("state")
    if state in {"connected", "connecting", "disconnected"}:
        return state
    return "connected" if status.get("connected") else "disconnected"


def _persist_local_node_identity(identity: dict) -> None:
    """Merge Meshtastic local-node identity into Hermes runtime status.

    Hermes' public write_runtime_status API does not currently accept plugin-owned
    platform fields. Preserve its schema and merge only our known identity keys into
    the profile-local gateway_state.json so status readers can distinguish the
    gateway's own node from remote nodes.
    """
    keys = ("node_id", "true_node_id", "node_num", "short_name", "long_name")
    data = {key: identity.get(key) for key in keys if identity.get(key) is not None}
    if not data:
        return

    try:
        import json
        from datetime import datetime, timezone

        home = os.getenv("HERMES_HOME")
        if not home:
            return
        path = Path(home) / "gateway_state.json"
        payload = {}
        if path.exists():
            try:
                payload = json.loads(path.read_text())
            except Exception:
                payload = {}
        platforms = payload.setdefault("platforms", {})
        platform = platforms.setdefault("meshtastic", {})
        platform.update(data)
        platform["identity_updated_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(payload, separators=(",", ":")))
    except Exception:
        logger.debug("Could not persist Meshtastic local node identity", exc_info=True)


if _HAVE_GATEWAY:

    class MeshtasticAdapter(BasePlatformAdapter):
        """Async adapter bridging the radio's threaded RX to the asyncio gateway."""

        def __init__(self, config, **kwargs):
            super().__init__(config=config, platform=Platform("meshtastic"))
            extra = getattr(config, "extra", {}) or {}
            self.host = os.getenv("MESHTASTIC_HOST") or extra.get("host", "")
            # The *configured* allowlist (names and/or legacy indices), parsed once.
            self.channel_spec = _channel_spec_from_env()
            # The allowlist actually enforced on inbound traffic: indices, re-resolved
            # from channel NAMES against the radio's channel table on every connect.
            # Before the first connect, name-based specs allow nothing (fail closed:
            # never guess an index for an unresolved name).
            self.allowed_channels = self._resolve_allowed_channels(None)
            self._loop: asyncio.AbstractEventLoop | None = None
            self._mgr = None

        def _resolve_allowed_channels(self, channel_table):
            """Turn ``self.channel_spec`` into enforceable channel indices.

            Called on every connect so a channel rename/reorder on the radio moves
            the allowlist with the NAME instead of leaving a stale index behind.
            """
            from meshtastic_hermes import gateway_bridge as gb

            allowed, resolved = gb.resolve_channel_spec(
                self.channel_spec, channel_table, log=logger
            )
            if resolved:
                logger.info(
                    "Meshtastic reply channels resolved: %s",
                    ", ".join(f"{name} -> {idx}" for name, idx in sorted(resolved.items())),
                )
            return allowed

        @property
        def name(self) -> str:
            return "Meshtastic"

        # ── lifecycle ────────────────────────────────────────────────────
        async def connect(self, *, is_reconnect: bool = False) -> bool:
            """Connect to the radio and start receiving.

            *is_reconnect* is part of the ``BasePlatformAdapter.connect``
            contract: the gateway always calls ``connect(is_reconnect=...)``
            (see Hermes ``gateway/run.py::_connect_adapter_with_timeout``), so
            this adapter MUST accept the keyword or the call raises TypeError.
            It is False on a cold first boot and True when the reconnect
            watcher re-establishes a platform that dropped after an outage.

            Meshtastic has no server-side offline queue to preserve, so the
            flag changes no behavior here and is only logged.
            """
            if not self.host:
                self._set_fatal_error(
                    "config_missing",
                    "MESHTASTIC_HOST is not set",
                    retryable=False,
                )
                return False
            self._loop = asyncio.get_running_loop()
            from meshtastic_hermes.connection import get_manager

            self._mgr = get_manager()
            # TCPInterface construction is blocking — keep it off the event loop.
            status = await self._loop.run_in_executor(None, self._mgr.connect, self.host)
            if not _manager_status_connected(status):
                state = _manager_status_state(status)
                # `connecting` is not usable yet, but it is not a dead configuration
                # either — say so, so the log doesn't send anyone debugging a node
                # that is merely still booting.
                self._set_fatal_error(
                    "connect_failed",
                    f"Meshtastic radio is {state} (not connected) to {self.host}",
                    retryable=True,
                )
                logger.warning(
                    "Meshtastic adapter could not reach %s yet (state=%s); the "
                    "connection supervisor and the gateway reconnect watcher will "
                    "retry (a booting node refusing TCP is expected; is another "
                    "client holding the node's single TCP slot?); "
                    "reply allowed_channels=%r",
                    self.host,
                    state,
                    self.allowed_channels,
                )
                return False

            from pubsub import pub

            pub.subscribe(self._on_rx, "meshtastic.receive")
            self._mark_connected()
            # Re-resolve channel NAMES -> indices against the table this link
            # reports. Doing it on every connect (cold boot AND reconnect) is what
            # makes a rename/reorder on the radio move the allowlist with the name.
            channel_table = []
            try:
                channel_table = await self._loop.run_in_executor(
                    None, self._mgr.channel_table
                )
            except Exception:
                logger.warning(
                    "Could not read the Meshtastic channel table; reply channel names "
                    "stay unresolved for now",
                    exc_info=True,
                )
            self.allowed_channels = self._resolve_allowed_channels(channel_table)
            identity = self._mgr.local_node_identity()
            _persist_local_node_identity(identity)
            if self._mgr.is_connected():
                logger.info(
                    "Meshtastic adapter connected%s to %s (node %s, short=%s, long=%s, reply allowed_channels=%r)",
                    " [reconnect]" if is_reconnect else "",
                    self.host,
                    identity.get("node_id"),
                    identity.get("short_name"),
                    identity.get("long_name"),
                    self.allowed_channels,
                )
            else:
                logger.warning(
                    "Meshtastic adapter could not reach %s yet — supervisor retrying "
                    "(is another client holding the node's single TCP slot?); "
                    "reply allowed_channels=%r",
                    self.host,
                    self.allowed_channels,
                )
            return True

        async def disconnect(self) -> None:
            try:
                from pubsub import pub

                pub.unsubscribe(self._on_rx, "meshtastic.receive")
            except Exception:
                pass
            if self._mgr and self._loop:
                await self._loop.run_in_executor(None, self._mgr.disconnect)
            self._mark_disconnected()

        # ── inbound (radio RX thread -> asyncio) ─────────────────────────
        def _on_rx(self, packet, interface=None):
            """pubsub callback on the radio RX thread. Hand off to the loop."""
            try:
                from meshtastic_hermes import gateway_bridge as gb

                inbound = gb.inbound_from_packet(packet, self._mgr.my_node_id())
                if inbound is None:
                    return
                decision = gb.should_reply(inbound, allowed_channels=self.allowed_channels)
                logger.debug(
                    "inbound %s ch=%s from=%s -> %s text=%r",
                    "DM" if inbound["is_dm"] else "channel",
                    inbound["channel"],
                    inbound["from_id"],
                    "REPLY" if decision else "skip (policy)",
                    inbound["text"],
                )
                if not decision:
                    return
                # Cross the thread boundary into the gateway's event loop.
                asyncio.run_coroutine_threadsafe(self._dispatch(inbound), self._loop)
            except Exception:
                logger.exception("Meshtastic adapter: inbound bridge failed")

        async def _dispatch(self, inbound: dict) -> None:
            if not self._message_handler:
                return
            from meshtastic_hermes import gateway_bridge as gb

            chat_id = gb.chat_id_for(inbound)
            source = self.build_source(
                chat_id=chat_id,
                chat_name=inbound["from_id"],
                chat_type="dm" if inbound["is_dm"] else "group",
                user_id=inbound["from_id"],
                user_name=inbound["from_id"],
            )
            event = MessageEvent(
                text=inbound["text"],
                message_type=MessageType.TEXT,
                source=source,
                message_id=inbound["message_id"] or str(int(time.time() * 1000)),
            )
            # Base class routes to the agent handler and calls self.send() with the reply.
            await self.handle_message(event)

        # ── outbound ─────────────────────────────────────────────────────
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            from meshtastic_hermes import gateway_bridge as gb
            from meshtastic_hermes import tools

            target = gb.outbound_target(str(chat_id))

            # Meshtastic packets are tiny — split long replies, and cap the number of
            # parts so a verbose reply can't flood the slow mesh.
            parts = _split_text(content, _MAX_MESH_BYTES)
            if not parts:
                return SendResult(success=True, message_id=str(int(time.time() * 1000)))
            if len(parts) > _MAX_PARTS:
                parts = parts[:_MAX_PARTS]
                last = parts[-1]
                while len((last + " …").encode("utf-8")) > _MAX_MESH_BYTES:
                    last = last[:-1]
                parts[-1] = last + " …"
                logger.warning("Meshtastic reply to %s truncated to %d parts", chat_id, _MAX_PARTS)

            def _do_send(text: str) -> str:
                return tools.send_text(
                    {
                        "text": text,
                        "dest_id": target["dest_id"],
                        "channel_index": target["channel_index"],
                        "pki": target["pki"],
                        "wait_ack": False,  # the gateway shouldn't block on radio acks
                    }
                )

            logger.debug("sending reply to chat_id=%s target=%s (%d part(s))", chat_id, target, len(parts))
            for idx, part in enumerate(parts):
                raw = await self._loop.run_in_executor(None, _do_send, part)
                data = json.loads(raw)
                if data.get("error"):
                    logger.warning("Meshtastic reply to %s failed: %s", chat_id, data["error"])
                    return SendResult(success=False, error=data["error"])
                if idx < len(parts) - 1:
                    await asyncio.sleep(1.0)  # pace multi-part sends on the slow mesh
            logger.info("Meshtastic reply sent to %s (%d part(s))", chat_id, len(parts))
            return SendResult(success=True, message_id=str(int(time.time() * 1000)))

        async def get_chat_info(self, chat_id):
            return {
                "name": chat_id,
                "type": "group" if str(chat_id).startswith("ch:") else "dm",
            }


# ── plugin registration ──────────────────────────────────────────────────
def check_requirements() -> bool:
    return bool(os.getenv("MESHTASTIC_HOST"))


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(os.getenv("MESHTASTIC_HOST") or extra.get("host"))


def _env_enablement():
    host = os.getenv("MESHTASTIC_HOST")
    if not host:
        return None
    return {"host": host}


# Environment variables this platform reads, in the order the wizard asks for them.
# (name, prompt, required)
_SETUP_ENV_VARS = [
    ("MESHTASTIC_HOST", "Meshtastic node host/IP (TCP, e.g. 192.168.1.50)", True),
    (
        "MESHTASTIC_REPLY_CHANNELS",
        "Reply channel names, comma-separated e.g. 'in.secure' (blank = DMs only)",
        False,
    ),
    (
        "MESHTASTIC_ALLOWED_USERS",
        "Allowed node ids, comma-separated (e.g. !a696579c)",
        False,
    ),
    ("MESHTASTIC_ALLOW_ALL_USERS", "Allow ANY node to talk to the agent? (true/false)", False),
]


def interactive_setup() -> None:
    """Registry ``setup_fn`` for ``hermes setup gateway`` -> Meshtastic.

    Without this, Hermes' ``_configure_platform`` falls through to its
    no-setup-helper branch, which prints ``Set these env vars in
    ~/.hermes/.env: MESHTASTIC_HOST`` *unconditionally* -- it never consults
    ``get_env_value``, so it says the var is unset even when it is set and the
    adapter is happily connected. Supplying a ``setup_fn`` takes over that
    branch entirely and lets us report the real state.

    Values are written with ``hermes_cli.config.save_env_value``, which targets
    the ACTIVE profile's ``.env`` (``$HERMES_HOME/.env``) -- for a profile named
    ``meshy`` that is ``~/.hermes/profiles/meshy/.env``, not ``~/.hermes/.env``.
    """
    try:
        from hermes_cli.config import get_env_value, save_env_value
    except ImportError:  # pragma: no cover - only outside the Hermes runtime
        print(
            "hermes_cli.config unavailable; set MESHTASTIC_HOST manually in "
            "$HERMES_HOME/.env"
        )
        return

    env_path = _active_env_path()

    print()
    print("Meshtastic setup")
    print("----------------")
    print(f"Values are saved to {env_path}")
    print()

    for name, label, required in _SETUP_ENV_VARS:
        current = get_env_value(name)
        if current:
            print(f"  {name} is already set ({current}).")
        elif required:
            print(f"  {name} is not set — the adapter stays dormant without it.")

        suffix = " [keep current]" if current else ("" if required else " [blank = skip]")
        try:
            value = input(f"{label}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if value:
            save_env_value(name, value)
            print(f"  Saved {name}.")

    host = get_env_value("MESHTASTIC_HOST")
    print()
    if host:
        print(f"Meshtastic is configured (MESHTASTIC_HOST={host}).")
        print("Restart the gateway to pick up changes.")
    else:
        print("Meshtastic is NOT configured — MESHTASTIC_HOST is still unset.")
        print(f"Set it in {env_path} and re-run this wizard.")


def _active_env_path() -> str:
    """Best-effort path of the .env the active Hermes profile actually uses."""
    try:
        from hermes_cli.config import get_env_path

        return str(get_env_path())
    except Exception:
        home = os.getenv("HERMES_HOME")
        if home:
            return str(Path(home) / ".env")
        return "~/.hermes/.env"


def register(ctx):
    """Plugin entry point: called once by the Hermes plugin system."""
    from meshtastic_hermes.connection import enable_debug_logging

    enable_debug_logging()  # honors MESHTASTIC_DEBUG

    # Bundle skills (loaded as `meshtastic-platform:<name>`), independent of whether the
    # gateway runtime is present.
    from pathlib import Path

    skills_dir = Path(__file__).parent / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                ctx.register_skill(child.name, skill_md)

    if not _HAVE_GATEWAY:
        logger.warning("gateway.platforms.base unavailable — Meshtastic platform not registered")
        return

    # Make the dormant-vs-active state visible: the gateway only creates+connects the
    # adapter when MESHTASTIC_HOST is set (check_fn/env_enablement gate on it). Use
    # WARNING for the unset case so it shows at the gateway's default log level (INFO
    # would be hidden unless MESHTASTIC_DEBUG raised it).
    host = os.getenv("MESHTASTIC_HOST")
    if host:
        logger.info(
            "meshtastic-platform registered (MESHTASTIC_HOST=%s, reply allowed_channels=%r)",
            host,
            _allowed_channels_from_env(),
        )
    else:
        logger.warning(
            "meshtastic-platform registered but MESHTASTIC_HOST is unset — the adapter will "
            "stay dormant (no radio connection). Set MESHTASTIC_HOST in ~/.hermes/.env."
        )

    ctx.register_platform(
        name="meshtastic",
        label="Meshtastic",
        adapter_factory=lambda cfg: MeshtasticAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["MESHTASTIC_HOST"],
        install_hint="Install meshtastic-hermes-plugin with pip (bundles the meshtastic radio stack)",
        # Without a setup_fn, `hermes setup gateway` falls back to a static
        # "Set these env vars" hint that never checks whether they ARE set.
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="MESHTASTIC_HOST",
        # User authorization (the gateway gates who may talk to the agent). Without
        # these, meshtastic defaults to deny-all. Allow specific node ids via
        # MESHTASTIC_ALLOWED_USERS="!a696579c,!..." or everyone via
        # MESHTASTIC_ALLOW_ALL_USERS=true.
        allowed_users_env="MESHTASTIC_ALLOWED_USERS",
        allow_all_env="MESHTASTIC_ALLOW_ALL_USERS",
        max_message_length=200,  # LoRa payloads are tiny
        emoji="📡",
        platform_hint=(
            "You are chatting over a Meshtastic LoRa mesh. Bandwidth is extremely "
            "limited (~200 bytes per message) and high-latency — keep replies very "
            "short and plain text (no markdown). Direct messages are end-to-end "
            "encrypted; channel messages are encrypted only with a shared channel key."
        ),
    )
