"""Registration surface of the tools plugin: hooks, slash command, CLI command.

`build_registry()` already covers the happy path in test_standalone.py; these
cover the branches it does not reach — the optional-API guards, the session
hooks, and the CLI/slash handlers.
"""

from __future__ import annotations

import argparse
import json

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


def test_cli_status(capsys):
    pkg._cli_handler(argparse.Namespace(meshtastic_command="status"))
    assert json.loads(capsys.readouterr().out)["connected"] is False


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
