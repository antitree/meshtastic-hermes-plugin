"""Standalone harness coverage: REPL dispatch, watch, observe and bridge.

`observe` and `bridge` normally block on time.sleep and talk to a radio; both are
driven here with a stubbed sleep and a fake radio publishing on the real pubsub
topic, so the real command bodies run without hardware.
"""

from __future__ import annotations

import json
import types

import pytest

from meshtastic_hermes import __main__ as m
from meshtastic_hermes import connection


@pytest.fixture
def ctx():
    return m.build_registry()


def _err(raw):
    return json.loads(raw).get("error", "")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def test_pretty_passes_through_non_json():
    assert m._pretty("not json at all") == "not json at all"
    assert m._pretty('{"a": 1}') == '{\n  "a": 1\n}'


def test_simulate_reply_echoes_and_truncates():
    assert m.simulate_reply("hi", {}) == "ack: hi"
    assert len(m.simulate_reply("x" * 500, {})) == len("ack: ") + 120


# ----------------------------------------------------------------------
# repl_command dispatch
# ----------------------------------------------------------------------


def test_repl_send_requires_a_channel_and_text(ctx):
    assert "usage: send" in _err(m.repl_command(ctx, "send"))
    assert "usage: send" in _err(m.repl_command(ctx, "send 0"))


def test_repl_send_rejects_a_non_integer_channel(ctx):
    assert "must be an integer index" in _err(m.repl_command(ctx, "send primary hello"))


def test_repl_send_reaches_the_send_tool(ctx, monkeypatch):
    captured: list = []
    ctx.tools["meshtastic_send_text"]["handler"] = lambda p: captured.append(p) or "{}"
    m.repl_command(ctx, "send 2 hello there world")
    assert captured == [{"channel_index": 2, "text": "hello there world"}]


def test_repl_dm_requires_a_node_and_text(ctx):
    assert "usage: dm" in _err(m.repl_command(ctx, "dm"))
    assert "usage: dm" in _err(m.repl_command(ctx, "dm !abc"))


def test_repl_dm_uses_pki(ctx):
    captured: list = []
    ctx.tools["meshtastic_send_text"]["handler"] = lambda p: captured.append(p) or "{}"
    m.repl_command(ctx, "dm !11112222 secret words")
    assert captured == [{"dest_id": "!11112222", "pki": True, "text": "secret words"}]


def test_repl_recent_with_and_without_a_count(ctx):
    captured: list = []
    ctx.tools["meshtastic_recent_messages"]["handler"] = lambda p: captured.append(p) or "{}"
    m.repl_command(ctx, "recent")
    m.repl_command(ctx, "recent 5")
    assert captured == [{}, {"limit": 5}]


def test_repl_recent_rejects_a_non_integer_count(ctx):
    assert "usage: recent" in _err(m.repl_command(ctx, "recent lots"))


def test_repl_connect_with_and_without_a_host(ctx):
    captured: list = []
    ctx.tools["meshtastic_connect"]["handler"] = lambda p: captured.append(p) or "{}"
    m.repl_command(ctx, "connect")
    m.repl_command(ctx, "connect 1.2.3.4")
    assert captured == [{}, {"host": "1.2.3.4"}]


def test_repl_neighbors_requires_a_node_id(ctx):
    assert "usage: neighbors" in _err(m.repl_command(ctx, "neighbors"))


def test_repl_neighbors_queries_the_kb(ctx):
    out = json.loads(m.repl_command(ctx, "neighbors !11112222"))
    assert out["node_id"] == "!11112222"


def test_repl_interactions_filters(ctx):
    captured: list = []
    ctx.tools["meshtastic_kb_interactions"]["handler"] = lambda p: captured.append(p) or "{}"
    m.repl_command(ctx, "interactions")
    m.repl_command(ctx, "interactions !abc")
    m.repl_command(ctx, "interactions !abc 1700000000")
    assert captured == [{}, {"node_id": "!abc"}, {"node_id": "!abc", "since": 1700000000.0}]


def test_repl_interactions_rejects_a_non_numeric_timestamp(ctx):
    assert "usage: interactions" in _err(m.repl_command(ctx, "interactions !abc yesterday"))


def test_repl_kbnodes_sort(ctx):
    captured: list = []
    ctx.tools["meshtastic_kb_nodes"]["handler"] = lambda p: captured.append(p) or "{}"
    m.repl_command(ctx, "kbnodes")
    m.repl_command(ctx, "kbnodes packets")
    assert captured == [{}, {"sort": "packets"}]


def test_repl_simple_verbs_map_to_tools(ctx):
    for verb, tool in m._REPL_SIMPLE.items():
        captured: list = []
        ctx.tools[tool]["handler"] = lambda p, c=captured: c.append(p) or "{}"
        m.repl_command(ctx, verb)
        assert captured == [{}], verb


def test_repl_raw_tool_call_with_json_args(ctx):
    captured: list = []
    ctx.tools["meshtastic_kb_nodes"]["handler"] = lambda p: captured.append(p) or "{}"
    m.repl_command(ctx, 'meshtastic_kb_nodes {"limit": 5}')
    assert captured == [{"limit": 5}]


def test_repl_raw_tool_call_without_args(ctx):
    out = json.loads(m.repl_command(ctx, "meshtastic_kb_summary"))
    assert "packets" in out


def test_repl_rejects_invalid_json_args(ctx):
    assert "invalid JSON args" in _err(m.repl_command(ctx, "meshtastic_kb_nodes {oops}"))


def test_repl_unknown_command(ctx):
    out = json.loads(m.repl_command(ctx, "frobnicate"))
    assert "unknown command" in out["error"]
    assert out["hint"] == "type 'help'"


# ----------------------------------------------------------------------
# readline
# ----------------------------------------------------------------------


def test_enable_readline_skipped_for_non_interactive_stdin(monkeypatch):
    monkeypatch.setattr(m.sys.stdin, "isatty", lambda: False)
    assert m._enable_readline() == (None, None)


def test_enable_readline_on_a_tty(monkeypatch, tmp_path):
    monkeypatch.setattr(m.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(m.os.path, "expanduser", lambda p: str(tmp_path / "hist"))
    readline, histfile = m._enable_readline()
    # readline is optional on some platforms; either outcome is valid.
    assert (readline is None) == (histfile is None)


# ----------------------------------------------------------------------
# watch
# ----------------------------------------------------------------------


def test_watch_prints_only_new_messages(ctx, monkeypatch, capsys):
    from meshtastic_hermes.observer import get_observer

    # This test is about NEW-message detection (is "fresh" printed once and
    # "already seen" not re-printed?). The watch loop dispatches the registered
    # recent_messages handler, which redacts bodies by default since item 4, so opt
    # in to keep asserting on the body. The default-redacted behavior of the same
    # loop is asserted by test_watch_redacts_bodies_by_default below.
    monkeypatch.setenv("MESHTASTIC_EXPOSE_RECENT_TEXT", "true")
    obs = get_observer()
    obs.on_receive(
        {
            "fromId": "!old",
            "toId": "^all",
            "rxTime": 1.0,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "already seen"},
        }
    )

    ticks = {"n": 0}

    def fake_sleep(_secs):
        # Inject one new message after the first poll, then let the loop expire.
        if ticks["n"] == 0:
            obs.on_receive(
                {
                    "fromId": "!new",
                    "toId": "!aabbccdd",
                    "rxTime": 2.0,
                    "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "fresh"},
                }
            )
        ticks["n"] += 1

    times = iter([0.0, 0.0, 0.5, 99.0])
    monkeypatch.setattr(m.time, "sleep", fake_sleep)
    monkeypatch.setattr(m.time, "time", lambda: next(times))

    m._watch_messages(ctx, 1.0)
    out = capsys.readouterr().out
    assert "fresh" in out
    assert "already seen" not in out  # pre-existing messages are not re-printed
    assert "[DM]" in out  # addressed to a specific node


def test_watch_labels_a_channel_broadcast(ctx, monkeypatch, capsys):
    from meshtastic_hermes.observer import get_observer

    obs = get_observer()

    def fake_sleep(_secs):
        obs.on_receive(
            {
                "fromId": "!x",
                "toId": "^all",
                "channel": 2,
                "rxTime": 5.0,
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "bcast"},
            }
        )

    times = iter([0.0, 0.0, 0.5, 99.0])
    monkeypatch.setattr(m.time, "sleep", fake_sleep)
    monkeypatch.setattr(m.time, "time", lambda: next(times))

    m._watch_messages(ctx, 1.0)
    assert "[ch2]" in capsys.readouterr().out


def test_watch_redacts_bodies_by_default(ctx, monkeypatch, capsys):
    """The harness dispatches the REGISTERED handler, so it inherits the gate."""
    from meshtastic_hermes.observer import get_observer

    monkeypatch.delenv("MESHTASTIC_EXPOSE_RECENT_TEXT", raising=False)
    obs = get_observer()

    def fake_sleep(_secs):
        obs.on_receive(
            {
                "fromId": "!x",
                "toId": "^all",
                "channel": 2,
                "rxTime": 5.0,
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "SENTINEL-WATCH-BODY"},
            }
        )

    times = iter([0.0, 0.0, 0.5, 99.0])
    monkeypatch.setattr(m.time, "sleep", fake_sleep)
    monkeypatch.setattr(m.time, "time", lambda: next(times))

    m._watch_messages(ctx, 1.0)
    out = capsys.readouterr().out
    assert "SENTINEL-WATCH-BODY" not in out
    # ...and it says so, rather than printing a bare None that reads as an empty
    # message. len(SENTINEL-WATCH-BODY) == 19.
    assert "<redacted len=19" in out


def test_watch_stops_on_keyboard_interrupt(ctx, monkeypatch):
    def boom(_secs):
        raise KeyboardInterrupt

    times = iter([0.0, 0.0, 0.5])
    monkeypatch.setattr(m.time, "sleep", boom)
    monkeypatch.setattr(m.time, "time", lambda: next(times))
    m._watch_messages(ctx, 1.0)  # must return cleanly


# ----------------------------------------------------------------------
# the repl command loop
# ----------------------------------------------------------------------


def _drive_repl(monkeypatch, lines):
    it = iter(lines)
    monkeypatch.setattr("builtins.input", lambda _p: next(it))


def test_repl_loop_help_and_blank_lines(ctx, monkeypatch, capsys):
    _drive_repl(monkeypatch, ["", "help", "?", "quit"])
    assert m._cmd_repl(ctx, types.SimpleNamespace(host=None)) == 0
    out = capsys.readouterr().out
    assert out.count("Commands (channel is an INDEX") == 2


def test_repl_loop_watch_usage_error(ctx, monkeypatch, capsys):
    _drive_repl(monkeypatch, ["watch forever", "quit"])
    m._cmd_repl(ctx, types.SimpleNamespace(host=None))
    assert "usage: watch [seconds]" in capsys.readouterr().out


def test_repl_loop_watch_invokes_the_watcher(ctx, monkeypatch):
    called: list = []
    monkeypatch.setattr(m, "_watch_messages", lambda c, s: called.append(s))
    _drive_repl(monkeypatch, ["watch", "watch 5", "quit"])
    m._cmd_repl(ctx, types.SimpleNamespace(host=None))
    assert called == [120.0, 5.0]


def test_repl_loop_exit_on_eof(ctx, monkeypatch):
    def eof(_p):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert m._cmd_repl(ctx, types.SimpleNamespace(host=None)) == 0


def test_repl_loop_autoconnects_to_the_given_host(ctx, monkeypatch, capsys):
    captured: list = []
    ctx.tools["meshtastic_connect"]["handler"] = lambda p: captured.append(p) or '{"ok": true}'
    _drive_repl(monkeypatch, ["quit"])
    m._cmd_repl(ctx, types.SimpleNamespace(host="1.2.3.4"))
    assert captured == [{"host": "1.2.3.4"}]


def test_repl_loop_exit_keyword(ctx, monkeypatch):
    _drive_repl(monkeypatch, ["exit"])
    assert m._cmd_repl(ctx, types.SimpleNamespace(host=None)) == 0


def test_repl_loop_writes_history(ctx, monkeypatch, tmp_path):
    written: list = []
    fake_readline = types.SimpleNamespace(write_history_file=lambda p: written.append(p))
    monkeypatch.setattr(m, "_enable_readline", lambda: (fake_readline, str(tmp_path / "h")))
    _drive_repl(monkeypatch, ["quit"])
    m._cmd_repl(ctx, types.SimpleNamespace(host=None))
    assert written == [str(tmp_path / "h")]


def test_repl_loop_tolerates_an_unwritable_history_file(ctx, monkeypatch):
    def boom(_p):
        raise OSError("read-only")

    monkeypatch.setattr(
        m, "_enable_readline", lambda: (types.SimpleNamespace(write_history_file=boom), "/x/h")
    )
    _drive_repl(monkeypatch, ["quit"])
    assert m._cmd_repl(ctx, types.SimpleNamespace(host=None)) == 0


def test_repl_loop_tools_listing(ctx, monkeypatch, capsys):
    _drive_repl(monkeypatch, ["tools", "quit"])
    m._cmd_repl(ctx, types.SimpleNamespace(host=None))
    assert "13 tools" in capsys.readouterr().out


# ----------------------------------------------------------------------
# call / observe / bridge
# ----------------------------------------------------------------------


def test_call_rejects_invalid_json_args(capsys):
    assert m.main(["call", "meshtastic_kb_nodes", "{oops}"]) == 1
    assert "invalid JSON args" in capsys.readouterr().out


def test_call_passes_json_args(capsys, monkeypatch):
    # About ARG PASSING through `call`, not the privacy gate: kb_nodes returns counts
    # only unless traffic metadata is exposed (item 4), so opt in to see "nodes".
    monkeypatch.setenv("MESHTASTIC_EXPOSE_TRAFFIC_METADATA", "true")
    assert m.main(["call", "meshtastic_kb_nodes", '{"limit": 1}']) == 0
    assert "nodes" in capsys.readouterr().out


@pytest.fixture
def fake_radio(monkeypatch):
    """Point the ConnectionManager at a fake TCPInterface."""
    built: list = []

    class _Radio:
        def __init__(self, host, portNumber=4403):
            self.host = host
            self.nodes = {}
            self.localNode = types.SimpleNamespace(channels=[])
            self.myInfo = types.SimpleNamespace(my_node_num=0xAABBCCDD)
            self.sent: list = []
            built.append(self)

        def sendData(self, payload, **kw):
            self.sent.append({"payload": payload, **kw})

        def close(self):
            pass

    monkeypatch.setattr(
        connection,
        "_import_meshtastic",
        lambda: types.SimpleNamespace(tcp_interface=types.SimpleNamespace(TCPInterface=_Radio)),
    )
    return built


def test_observe_connects_dumps_and_disconnects(ctx, fake_radio, monkeypatch, capsys):
    monkeypatch.setattr(m.time, "sleep", lambda _s: None)
    rc = m._cmd_observe(ctx, types.SimpleNamespace(host="1.2.3.4", seconds=1))
    assert rc == 0
    out = capsys.readouterr().out
    assert "# meshtastic_list_nodes" in out
    assert "# meshtastic_kb_summary" in out
    assert connection.get_manager().is_connected() is False  # cleaned up


def test_observe_stops_early_on_keyboard_interrupt(ctx, fake_radio, monkeypatch, capsys):
    def boom(_s):
        raise KeyboardInterrupt

    monkeypatch.setattr(m.time, "sleep", boom)
    assert m._cmd_observe(ctx, types.SimpleNamespace(host="1.2.3.4", seconds=1)) == 0


def _bridge_args(**kw):
    # Stands in for the argparse namespace `bridge` builds. `no_mention=True`
    # keeps these pre-existing cases exercising the reply-to-everything behavior
    # they were written for; mention gating gets its own tests below.
    base = {
        "host": "1.2.3.4",
        "seconds": 1,
        "send": False,
        "all": False,
        "channels": None,
        "no_mention": True,
    }
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_bridge_requires_a_host(ctx, monkeypatch, capsys):
    monkeypatch.delenv("MESHTASTIC_HOST", raising=False)
    assert m._cmd_bridge(ctx, _bridge_args(host=None)) == 1
    assert "no host given" in capsys.readouterr().out


def test_bridge_dry_run_reports_the_reply_it_would_send(ctx, fake_radio, monkeypatch, capsys):
    from pubsub import pub

    def deliver(_secs):
        pub.sendMessage(
            "meshtastic.receive",
            packet={
                "fromId": "!11112222",
                "toId": "!aabbccdd",
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "ping"},
            },
            interface=None,
        )

    monkeypatch.setattr(m.time, "sleep", deliver)
    assert m._cmd_bridge(ctx, _bridge_args()) == 0
    out = capsys.readouterr().out
    assert "[inbound DM] !11112222: 'ping'" in out
    assert "-> reply to !11112222: 'ack: ping'" in out
    assert "dry-run" in out
    assert fake_radio[-1].sent == []  # nothing transmitted


def test_bridge_send_mode_transmits(ctx, fake_radio, monkeypatch, capsys):
    from pubsub import pub

    def deliver(_secs):
        pub.sendMessage(
            "meshtastic.receive",
            packet={
                "fromId": "!11112222",
                "toId": "!aabbccdd",
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "ping"},
            },
            interface=None,
        )

    monkeypatch.setattr(m.time, "sleep", deliver)
    m._cmd_bridge(ctx, _bridge_args(send=True))
    assert fake_radio[-1].sent[0]["payload"] == b"ack: ping"
    assert "sent:" in capsys.readouterr().out


def test_bridge_reports_a_skip_on_a_disallowed_channel(ctx, fake_radio, monkeypatch, capsys):
    from pubsub import pub

    def deliver(_secs):
        pub.sendMessage(
            "meshtastic.receive",
            packet={
                "fromId": "!11112222",
                "toId": "^all",
                "channel": 0,
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi all"},
            },
            interface=None,
        )

    monkeypatch.setattr(m.time, "sleep", deliver)
    m._cmd_bridge(ctx, _bridge_args())
    captured = capsys.readouterr()
    assert "[skip ch0] !11112222: 'hi all'" in captured.out
    assert "DMs only" in captured.err  # the scope banner goes to stderr


def test_bridge_scope_labels(ctx, fake_radio, monkeypatch, capsys):
    monkeypatch.setattr(m.time, "sleep", lambda _s: None)

    m._cmd_bridge(ctx, _bridge_args(all=True))
    assert "DMs + all channels" in capsys.readouterr().err

    m._cmd_bridge(ctx, _bridge_args(channels="1,2"))
    assert "DMs + channels [1, 2]" in capsys.readouterr().err


def test_bridge_stops_early_on_keyboard_interrupt(ctx, fake_radio, monkeypatch):
    def boom(_s):
        raise KeyboardInterrupt

    monkeypatch.setattr(m.time, "sleep", boom)
    assert m._cmd_bridge(ctx, _bridge_args()) == 0


def test_bridge_rx_callback_swallows_exceptions(ctx, fake_radio, monkeypatch, capsys):
    """A malformed packet must not kill the radio's reader thread."""
    from pubsub import pub

    def deliver(_secs):
        pub.sendMessage("meshtastic.receive", packet=None, interface=None)

    monkeypatch.setattr(m.time, "sleep", deliver)
    assert m._cmd_bridge(ctx, _bridge_args()) == 0


def test_bridge_uses_the_env_host(ctx, fake_radio, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_HOST", "env.example")
    monkeypatch.setattr(m.time, "sleep", lambda _s: None)
    assert m._cmd_bridge(ctx, _bridge_args(host=None)) == 0
    assert fake_radio[-1].host == "env.example"
