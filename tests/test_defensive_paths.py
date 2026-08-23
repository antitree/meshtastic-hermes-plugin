"""Defensive error branches that are reachable, and worth pinning.

These are the "never crash" guarantees: a callback on the radio's reader thread,
a teardown path, and a multibyte hard-split. Each is cheap to reach and each
protects a real failure mode, so they get tests rather than a coverage
exception. The exceptions that remain are listed in ROADMAP.md.
"""

from __future__ import annotations

import types

from meshtastic_hermes import __main__ as m
from meshtastic_platform import adapter

# ----------------------------------------------------------------------
# adapter._split_text: UTF-8 boundary back-off inside a hard split
# ----------------------------------------------------------------------


def test_hard_split_backs_off_to_a_utf8_boundary():
    """A single oversized multibyte token must never be cut mid-character."""
    # 3-byte chars against a limit that is not a multiple of 3 forces the back-off.
    token = "あ" * 40  # 120 bytes, no spaces -> hard split
    parts = adapter._split_text(token, 10)

    assert all(len(p.encode("utf-8")) <= 10 for p in parts)
    assert "".join(parts) == token  # nothing lost, nothing mangled
    assert "�" not in "".join(parts)  # no replacement chars from a bad cut


def test_hard_split_of_a_mixed_width_token():
    token = "aあbい" * 20
    parts = adapter._split_text(token, 7)
    assert all(len(p.encode("utf-8")) <= 7 for p in parts)
    assert "".join(parts) == token


# ----------------------------------------------------------------------
# __main__: teardown paths
# ----------------------------------------------------------------------


def test_repl_exit_tolerates_a_failing_disconnect(monkeypatch):
    """A disconnect that throws on the way out must not change the exit code."""
    ctx = m.build_registry()

    def boom(_payload):
        raise RuntimeError("radio already gone")

    ctx.tools["meshtastic_disconnect"]["handler"] = boom
    monkeypatch.setattr("builtins.input", lambda _p: "quit")
    assert m._cmd_repl(ctx, types.SimpleNamespace(host=None)) == 0


def test_bridge_reports_a_missing_radio_stack(monkeypatch, capsys):
    """Without pubsub installed the bridge must explain itself, not traceback."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "pubsub":
            raise ImportError("No module named 'pubsub'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    ctx = m.build_registry()
    args = types.SimpleNamespace(
        host="1.2.3.4", seconds=1, send=False, all=False, channels=None, no_mention=True
    )
    assert m._cmd_bridge(ctx, args) == 1
    assert "radio stack not installed" in capsys.readouterr().out


def test_bridge_ignores_a_packet_that_is_not_a_text_frame(monkeypatch, capsys):
    """process_inbound returning None must simply produce no output."""
    from meshtastic_hermes import connection

    class _Radio:
        def __init__(self, host, portNumber=4403):
            self.host = host
            self.nodes = {}
            self.localNode = types.SimpleNamespace(channels=[])
            self.myInfo = types.SimpleNamespace(my_node_num=0xAABBCCDD)

        def sendData(self, payload, **kw):
            pass

        def close(self):
            pass

    monkeypatch.setattr(
        connection,
        "_import_meshtastic",
        lambda: types.SimpleNamespace(tcp_interface=types.SimpleNamespace(TCPInterface=_Radio)),
    )

    def deliver(_secs):
        from pubsub import pub

        pub.sendMessage(
            "meshtastic.receive",
            packet={"fromId": "!1", "decoded": {"portnum": "POSITION_APP"}},
            interface=None,
        )

    monkeypatch.setattr(m.time, "sleep", deliver)
    ctx = m.build_registry()
    args = types.SimpleNamespace(
        host="1.2.3.4", seconds=1, send=False, all=False, channels=None, no_mention=True
    )
    assert m._cmd_bridge(ctx, args) == 0
    out = capsys.readouterr().out
    assert "[inbound" not in out
    assert "[skip" not in out


# ----------------------------------------------------------------------
# module entry point
# ----------------------------------------------------------------------


def test_module_is_runnable_as_python_dash_m():
    """`python -m meshtastic_hermes list` is documented; make sure it works."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "meshtastic_hermes", "list"],
        capture_output=True,
        text=True,
        check=False,
        env={"MESHTASTIC_HERMES_DB": ":memory:", "PATH": "/usr/bin:/bin"},
        cwd=str(__import__("pathlib").Path(__file__).parent.parent),
    )
    assert result.returncode == 0, result.stderr
    assert "12 tools" in result.stdout
