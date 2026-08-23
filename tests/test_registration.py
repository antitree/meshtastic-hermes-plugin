"""Registration surface of the tools plugin: hooks, slash command, CLI command.

`build_registry()` already covers the happy path in test_standalone.py; these
cover the branches it does not reach — the optional-API guards, the session
hooks, and the CLI/slash handlers.
"""

from __future__ import annotations

import argparse
import json
import sys
import types

import meshtastic_hermes as pkg
from meshtastic_hermes import connection


class _MinimalCtx:
    """A Hermes context WITHOUT register_command / register_cli_command.

    Older Hermes builds lack those APIs; registration must still succeed.
    """

    def __init__(self):
        self.tools: dict = {}
        self.hooks: dict = {}
        self.skills: dict = {}

    def register_tool(self, name, toolset, schema, handler, **_kw):
        self.tools[name] = handler

    def register_hook(self, event, fn):
        self.hooks.setdefault(event, []).append(fn)

    def register_skill(self, name, path):
        self.skills[name] = path


def test_register_survives_a_context_without_the_optional_apis():
    ctx = _MinimalCtx()
    pkg.register(ctx)  # must not raise
    assert len(ctx.tools) == 12
    assert set(ctx.hooks) == {"on_session_start", "on_session_end"}
    assert set(ctx.skills) == {"mesh-recon", "messaging-safety"}


def test_registered_tool_names_match_the_manifest():
    """plugin.yaml's provides_tools is what Hermes advertises — it must be true."""
    import pathlib

    import yaml

    manifest = yaml.safe_load(
        (pathlib.Path(pkg.__file__).parent / "plugin.yaml").read_text()
    )
    ctx = _MinimalCtx()
    pkg.register(ctx)
    assert sorted(manifest["provides_tools"]) == sorted(ctx.tools)
    assert sorted(manifest["provides_hooks"]) == sorted(ctx.hooks)


# ----------------------------------------------------------------------
# session hooks
# ----------------------------------------------------------------------


def test_session_start_is_a_noop_without_a_host(monkeypatch):
    monkeypatch.delenv("MESHTASTIC_HOST", raising=False)
    called = []
    monkeypatch.setattr(
        connection.get_manager(), "connect", lambda *a, **k: called.append(a)
    )
    pkg._on_session_start()
    assert called == []


def test_session_start_auto_connects_when_a_host_is_set(monkeypatch):
    monkeypatch.setenv("MESHTASTIC_HOST", "auto.example")
    called = []
    monkeypatch.setattr(
        connection.get_manager(), "connect", lambda *a, **k: called.append(a)
    )
    pkg._on_session_start(session_id="s1")
    assert called == [("auto.example",)]


def test_session_start_skips_when_already_connected(monkeypatch):
    monkeypatch.setenv("MESHTASTIC_HOST", "auto.example")
    mgr = connection.get_manager()
    mgr._iface = object()
    called = []
    monkeypatch.setattr(mgr, "connect", lambda *a, **k: called.append(a))
    pkg._on_session_start()
    assert called == []


def test_session_start_survives_a_failing_connect(monkeypatch, caplog):
    """An unreachable node must not break session startup."""
    import logging

    monkeypatch.setenv("MESHTASTIC_HOST", "auto.example")
    caplog.set_level(logging.WARNING)

    def boom(*_a, **_k):
        raise OSError("refused")

    monkeypatch.setattr(connection.get_manager(), "connect", boom)
    pkg._on_session_start()  # must not raise
    assert "auto-connect failed" in caplog.text


def test_session_end_does_not_disconnect():
    """The link is shared with the platform adapter — ending a session must not tear it down."""
    mgr = connection.get_manager()
    sentinel = object()
    mgr._iface = sentinel
    pkg._on_session_end(session_id="s1")
    assert mgr._iface is sentinel


# ----------------------------------------------------------------------
# /meshtastic slash command
# ----------------------------------------------------------------------


def test_slash_help():
    assert "Usage: /meshtastic" in pkg._handle_slash("help")


def test_slash_status_when_disconnected():
    out = pkg._handle_slash("")
    assert "Connection: disconnected" in out
    assert "Local node: unknown" in out
    assert "KB: 0 nodes, 0 packets" in out


def test_slash_status_when_connected(monkeypatch):
    mgr = connection.get_manager()
    monkeypatch.setattr(
        mgr, "status", lambda: {"connected": True, "host": "1.2.3.4", "node_id": "!aabbccdd"}
    )
    from meshtastic_hermes.observer import get_observer

    get_observer().kb.record_packet({"ts": 1.0, "from_node": "!x", "to_node": "^all", "channel": 0})

    out = pkg._handle_slash("")
    assert "Connection: connected to 1.2.3.4" in out
    assert "Local node: !aabbccdd" in out
    assert "1 nodes, 1 packets" in out


# ----------------------------------------------------------------------
# hermes meshtastic <status|kb-summary>
# ----------------------------------------------------------------------


def test_cli_kb_summary(capsys):
    pkg._cli_handler(argparse.Namespace(meshtastic_command="kb-summary"))
    assert "packets" in json.loads(capsys.readouterr().out)


def test_cli_status(monkeypatch, capsys):
    monkeypatch.setattr(pkg, "_gateway_runtime_status", lambda: None)
    pkg._cli_handler(argparse.Namespace(meshtastic_command="status"))
    data = json.loads(capsys.readouterr().out)
    assert data["connected"] is False
    assert data["source"] == "process_local"


def test_cli_status_prefers_live_gateway_runtime(monkeypatch, capsys):
    monkeypatch.setenv("MESHTASTIC_HOST", "10.2.2.60")

    gateway_pkg = types.ModuleType("gateway")
    status_mod = types.ModuleType("gateway.status")

    def read_runtime_status():
        return {
            "gateway_state": "running",
            "platforms": {
                "meshtastic": {
                    "state": "connected",
                    "node_id": "!aabbccdd",
                    "true_node_id": "!aabbccdd",
                    "node_num": 0xAABBCCDD,
                    "short_name": "MESH",
                    "long_name": "Meshy Gateway",
                    "error_code": None,
                    "error_message": None,
                    "updated_at": "2026-08-23T15:04:58Z",
                    "identity_updated_at": "2026-08-23T15:04:59Z",
                    "writer_pid": 9820,
                }
            },
        }

    status_mod.read_runtime_status = read_runtime_status
    status_mod.runtime_status_pid_is_live = lambda record: True
    monkeypatch.setitem(sys.modules, "gateway", gateway_pkg)
    monkeypatch.setitem(sys.modules, "gateway.status", status_mod)

    pkg._cli_handler(argparse.Namespace(meshtastic_command="status"))
    data = json.loads(capsys.readouterr().out)
    assert data["connected"] is True
    assert data["host"] == "10.2.2.60"
    assert data["source"] == "gateway_runtime"
    assert data["node_id"] == "!aabbccdd"
    assert data["true_node_id"] == "!aabbccdd"
    assert data["node_num"] == 0xAABBCCDD
    assert data["short_name"] == "MESH"
    assert data["long_name"] == "Meshy Gateway"
    assert data["identity_updated_at"] == "2026-08-23T15:04:59Z"
    assert data["gateway_state"] == "running"
    assert data["platform_state"] == "connected"


def test_cli_status_ignores_stale_gateway_runtime(monkeypatch):
    gateway_pkg = types.ModuleType("gateway")
    status_mod = types.ModuleType("gateway.status")
    status_mod.read_runtime_status = lambda: {"platforms": {"meshtastic": {"state": "connected"}}}
    status_mod.runtime_status_pid_is_live = lambda record: False
    monkeypatch.setitem(sys.modules, "gateway", gateway_pkg)
    monkeypatch.setitem(sys.modules, "gateway.status", status_mod)

    assert pkg._gateway_runtime_status() is None


def test_cli_usage_for_an_unknown_subcommand(capsys):
    pkg._cli_handler(argparse.Namespace(meshtastic_command="nope"))
    assert "Usage: hermes meshtastic" in capsys.readouterr().out


def test_cli_usage_when_no_subcommand_given(capsys):
    pkg._cli_handler(argparse.Namespace())
    assert "Usage: hermes meshtastic" in capsys.readouterr().out


def test_setup_argparse_wires_the_subcommands():
    parser = argparse.ArgumentParser()
    pkg._setup_argparse(parser)
    assert parser.parse_args(["status"]).meshtastic_command == "status"
    assert parser.parse_args(["kb-summary"]).meshtastic_command == "kb-summary"
    assert parser.parse_args([]).func is pkg._cli_handler


# ----------------------------------------------------------------------
# Three-state rendering
# ----------------------------------------------------------------------


def test_render_connection_three_states():
    assert pkg._render_connection({"state": "connected", "host": "1.2.3.4"}) == (
        "connected to 1.2.3.4"
    )
    connecting = pkg._render_connection({"state": "connecting", "host": "1.2.3.4"})
    assert connecting.startswith("connecting to 1.2.3.4")
    assert "normal" in connecting  # tells the user a refused TCP is expected
    assert pkg._render_connection({"state": "disconnected", "host": "1.2.3.4"}) == "disconnected"


def test_render_connection_falls_back_to_the_legacy_boolean():
    """A status record from an older build has no `state` key."""
    assert pkg._render_connection({"connected": True, "host": "1.2.3.4"}) == "connected to 1.2.3.4"
    assert pkg._render_connection({"connected": False, "host": "1.2.3.4"}) == "disconnected"


def test_slash_status_while_connecting(monkeypatch):
    mgr = connection.get_manager()
    monkeypatch.setattr(
        mgr,
        "status",
        lambda: {
            "connected": False,
            "state": "connecting",
            "host": "1.2.3.4",
            "node_id": None,
        },
    )
    out = pkg._handle_slash("")
    assert "Connection: connecting to 1.2.3.4" in out
    assert "disconnected" not in out


def test_normalize_platform_state():
    assert pkg._normalize_platform_state("connected") == "connected"
    for coming_up in ("connecting", "reconnecting", "starting", "initializing"):
        assert pkg._normalize_platform_state(coming_up) == "connecting"
    for dead in ("error", "stopped", None, "whatever"):
        assert pkg._normalize_platform_state(dead) == "disconnected"
