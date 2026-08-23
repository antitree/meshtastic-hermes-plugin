"""Adapter tests that exercise the MeshtasticAdapter class itself.

`meshtastic_platform.adapter` only defines MeshtasticAdapter when the Hermes
gateway runtime (`gateway.platforms.base`, `gateway.config`) is importable, which
it is not outside Hermes. So these tests install a *stub* gateway package into
sys.modules and re-import the adapter module against it.

That stub is the honest limit of these tests: they verify this adapter's own
logic against the base-class surface it uses (`_set_fatal_error`,
`_mark_connected`, `build_source`, `handle_message`, `SendResult`). They do NOT
verify that the stub matches real Hermes — the Hermes source is not available
here. If the real base class differs, these tests still pass. See ROADMAP.md.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from dataclasses import dataclass, field
from enum import Enum

import pytest

# ----------------------------------------------------------------------
# Stub gateway runtime
# ----------------------------------------------------------------------


class _MessageType(Enum):
    TEXT = "text"


@dataclass
class _SendResult:
    success: bool
    message_id: str | None = None
    error: str | None = None


@dataclass
class _MessageEvent:
    text: str
    message_type: object
    source: object
    message_id: str


class _Platform:
    def __init__(self, name):
        self.name = name


class _BasePlatformAdapter:
    """Minimal stand-in for Hermes' BasePlatformAdapter."""

    def __init__(self, config=None, platform=None):
        self.config = config
        self.platform = platform
        self._message_handler = None
        self.state = "init"
        self.fatal = None
        self.handled: list = []

    def _set_fatal_error(self, code, message, retryable=False):
        self.fatal = {"code": code, "message": message, "retryable": retryable}

    def _mark_connected(self):
        self.state = "connected"

    def _mark_disconnected(self):
        self.state = "disconnected"

    def build_source(self, **kw):
        return dict(kw)

    async def handle_message(self, event):
        self.handled.append(event)


@dataclass
class _Config:
    extra: dict = field(default_factory=dict)


@pytest.fixture
def adapter_mod(monkeypatch):
    """Import meshtastic_platform.adapter with a stub gateway runtime present."""
    gateway = types.ModuleType("gateway")
    gw_config = types.ModuleType("gateway.config")
    gw_config.Platform = _Platform
    gw_platforms = types.ModuleType("gateway.platforms")
    gw_base = types.ModuleType("gateway.platforms.base")
    gw_base.BasePlatformAdapter = _BasePlatformAdapter
    gw_base.MessageEvent = _MessageEvent
    gw_base.MessageType = _MessageType
    gw_base.SendResult = _SendResult

    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.config", gw_config)
    monkeypatch.setitem(sys.modules, "gateway.platforms", gw_platforms)
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", gw_base)

    import meshtastic_platform.adapter as mod

    reloaded = importlib.reload(mod)
    assert reloaded._HAVE_GATEWAY, "stub gateway runtime did not take effect"
    yield reloaded

    # Restore the real (gateway-less) module so later tests see the normal state.
    for name in ("gateway", "gateway.config", "gateway.platforms", "gateway.platforms.base"):
        sys.modules.pop(name, None)
    importlib.reload(mod)


def _make(adapter_mod, monkeypatch, *, host="10.0.0.5", extra=None):
    monkeypatch.setenv("MESHTASTIC_HOST", host) if host else monkeypatch.delenv(
        "MESHTASTIC_HOST", raising=False
    )
    return adapter_mod.MeshtasticAdapter(_Config(extra=extra or {}))


# ----------------------------------------------------------------------
# construction
# ----------------------------------------------------------------------


def test_adapter_name_and_host_from_env(adapter_mod, monkeypatch):
    a = _make(adapter_mod, monkeypatch, host="192.168.1.9")
    assert a.name == "Meshtastic"
    assert a.host == "192.168.1.9"


def test_host_falls_back_to_config_extra(adapter_mod, monkeypatch):
    a = _make(adapter_mod, monkeypatch, host=None, extra={"host": "cfg.example"})
    assert a.host == "cfg.example"


def test_config_without_extra_attribute(adapter_mod, monkeypatch):
    monkeypatch.delenv("MESHTASTIC_HOST", raising=False)

    class Bare:
        pass

    a = adapter_mod.MeshtasticAdapter(Bare())
    assert a.host == ""


# ----------------------------------------------------------------------
# connect / disconnect
# ----------------------------------------------------------------------


def test_connect_without_host_sets_fatal_error(adapter_mod, monkeypatch):
    a = _make(adapter_mod, monkeypatch, host=None)
    assert asyncio.run(a.connect()) is False
    assert a.fatal["code"] == "config_missing"
    assert a.fatal["retryable"] is False
    assert a.state == "init"  # never marked connected


class _FakeManager:
    """Stands in for ConnectionManager without opening a socket."""

    def __init__(self, *, connected=True, node_id="!aabbccdd"):
        self._connected = connected
        self._node_id = node_id
        self.connect_calls: list = []
        self.disconnect_calls = 0

    def connect(self, host, port=4403):
        self.connect_calls.append((host, port))
        return {"connected": self._connected, "host": host, "node_id": self._node_id}

    def disconnect(self):
        self.disconnect_calls += 1
        return {"connected": False}

    def is_connected(self):
        return self._connected

    def my_node_id(self):
        return self._node_id


def _patch_manager(monkeypatch, mgr):
    from meshtastic_hermes import connection

    monkeypatch.setattr(connection, "get_manager", lambda: mgr)


def test_connect_success_subscribes_and_marks_connected(adapter_mod, monkeypatch):
    from pubsub import pub

    mgr = _FakeManager(connected=True)
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch, host="10.1.2.3")

    assert asyncio.run(a.connect()) is True
    assert mgr.connect_calls == [("10.1.2.3",)] or mgr.connect_calls == [("10.1.2.3", 4403)]
    assert a.state == "connected"
    # the RX callback is really subscribed to the real pubsub topic
    assert pub.getDefaultTopicMgr().getTopic("meshtastic.receive").hasListener(a._on_rx)


def test_connect_returns_true_even_when_radio_unreachable(adapter_mod, monkeypatch):
    """The supervisor retries in the background, so connect() must not report failure."""
    mgr = _FakeManager(connected=False)
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch, host="10.1.2.3")

    assert asyncio.run(a.connect()) is True
    assert a.state == "connected"
    assert a.fatal is None


def test_disconnect_unsubscribes_and_stops_manager(adapter_mod, monkeypatch):
    from pubsub import pub

    mgr = _FakeManager()
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)

    async def flow():
        await a.connect()
        await a.disconnect()

    asyncio.run(flow())
    assert mgr.disconnect_calls == 1
    assert a.state == "disconnected"
    assert not pub.getDefaultTopicMgr().getTopic("meshtastic.receive").hasListener(a._on_rx)


def test_disconnect_before_connect_is_safe(adapter_mod, monkeypatch):
    a = _make(adapter_mod, monkeypatch)
    asyncio.run(a.disconnect())  # no manager, no loop — must not raise
    assert a.state == "disconnected"


def test_disconnect_survives_a_failing_unsubscribe(adapter_mod, monkeypatch):
    """Teardown must complete even if pubsub refuses the unsubscribe."""
    from pubsub import pub

    a = _make(adapter_mod, monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("no such listener")

    monkeypatch.setattr(pub, "unsubscribe", boom)
    asyncio.run(a.disconnect())
    assert a.state == "disconnected"  # still marked down


# ----------------------------------------------------------------------
# inbound
# ----------------------------------------------------------------------


def _text_packet(text, *, from_id="!11112222", to_id="!aabbccdd", channel=0, pid=7):
    return {
        "fromId": from_id,
        "toId": to_id,
        "channel": channel,
        "id": pid,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text},
    }


def test_on_rx_dispatches_a_dm(adapter_mod, monkeypatch):
    mgr = _FakeManager(node_id="!aabbccdd")
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)
    a._message_handler = object()  # the gateway installs one; _dispatch no-ops without it

    async def flow():
        await a.connect()
        a._on_rx(_text_packet("ping"))
        await asyncio.sleep(0.05)  # let run_coroutine_threadsafe land

    asyncio.run(flow())
    assert len(a.handled) == 1
    event = a.handled[0]
    assert event.text == "ping"
    assert event.message_id == "7"
    assert event.source["chat_id"] == "!11112222"
    assert event.source["chat_type"] == "dm"


def test_on_rx_skips_channel_traffic_by_default(adapter_mod, monkeypatch):
    mgr = _FakeManager(node_id="!aabbccdd")
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)

    async def flow():
        await a.connect()
        a._on_rx(_text_packet("hi all", to_id="^all", channel=0))
        await asyncio.sleep(0.05)

    asyncio.run(flow())
    assert a.handled == []  # DMs-only default policy


def test_on_rx_dispatches_allowed_channel(adapter_mod, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "2")
    mgr = _FakeManager(node_id="!aabbccdd")
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)
    a._message_handler = object()
    assert a.allowed_channels == {2}

    async def flow():
        await a.connect()
        a._on_rx(_text_packet("hi", to_id="^all", channel=2))
        await asyncio.sleep(0.05)

    asyncio.run(flow())
    assert len(a.handled) == 1
    assert a.handled[0].source["chat_id"] == "ch:2"
    assert a.handled[0].source["chat_type"] == "group"


def test_on_rx_ignores_non_text(adapter_mod, monkeypatch):
    mgr = _FakeManager()
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)

    async def flow():
        await a.connect()
        a._on_rx({"fromId": "!1", "decoded": {"portnum": "POSITION_APP"}})
        a._on_rx({"fromId": "!1", "encrypted": b"xx"})  # no 'decoded' at all
        await asyncio.sleep(0.05)

    asyncio.run(flow())
    assert a.handled == []


def test_on_rx_swallows_exceptions(adapter_mod, monkeypatch, caplog):
    """The RX callback runs on the radio's reader thread; raising would kill it."""
    a = _make(adapter_mod, monkeypatch)
    a._mgr = None  # my_node_id() on None -> AttributeError inside the callback
    a._on_rx(_text_packet("boom"))  # must not raise
    assert "inbound bridge failed" in caplog.text


def test_dispatch_without_message_handler_is_a_noop(adapter_mod, monkeypatch):
    a = _make(adapter_mod, monkeypatch)
    a._message_handler = None
    inbound = {
        "text": "x",
        "from_id": "!1",
        "to_id": "!2",
        "channel": 0,
        "is_dm": True,
        "message_id": "",
    }
    asyncio.run(a._dispatch(inbound))
    assert a.handled == []


def test_dispatch_generates_message_id_when_absent(adapter_mod, monkeypatch):
    a = _make(adapter_mod, monkeypatch)
    a._message_handler = object()
    inbound = {
        "text": "x",
        "from_id": "!1",
        "to_id": "!2",
        "channel": 0,
        "is_dm": True,
        "message_id": "",
    }
    asyncio.run(a._dispatch(inbound))
    assert a.handled[0].message_id.isdigit()


# ----------------------------------------------------------------------
# outbound
# ----------------------------------------------------------------------


def _patch_send(monkeypatch, sink, result=None):
    from meshtastic_hermes import tools

    def fake_send_text(args, **kw):
        sink.append(args)
        return result if result is not None else json.dumps({"sent": True})

    monkeypatch.setattr(tools, "send_text", fake_send_text)


def test_send_dm_uses_pki(adapter_mod, monkeypatch):
    sink: list = []
    _patch_send(monkeypatch, sink)
    a = _make(adapter_mod, monkeypatch)

    async def flow():
        a._loop = asyncio.get_running_loop()
        return await a.send("!11112222", "hello there")

    res = asyncio.run(flow())
    assert res.success is True
    assert len(sink) == 1
    assert sink[0]["dest_id"] == "!11112222"
    assert sink[0]["pki"] is True
    assert sink[0]["wait_ack"] is False  # the gateway must never block on a radio ack


def test_send_channel_uses_channel_index(adapter_mod, monkeypatch):
    sink: list = []
    _patch_send(monkeypatch, sink)
    a = _make(adapter_mod, monkeypatch)

    async def flow():
        a._loop = asyncio.get_running_loop()
        return await a.send("ch:3", "broadcast")

    asyncio.run(flow())
    assert sink[0]["dest_id"] is None
    assert sink[0]["channel_index"] == 3
    assert sink[0]["pki"] is False


def test_send_empty_content_short_circuits(adapter_mod, monkeypatch):
    sink: list = []
    _patch_send(monkeypatch, sink)
    a = _make(adapter_mod, monkeypatch)

    async def flow():
        a._loop = asyncio.get_running_loop()
        return await a.send("!1", "   ")

    res = asyncio.run(flow())
    assert res.success is True
    assert sink == []  # nothing transmitted


def test_send_splits_long_content_into_parts(adapter_mod, monkeypatch):
    sink: list = []
    _patch_send(monkeypatch, sink)
    a = _make(adapter_mod, monkeypatch)

    async def _nosleep(*_a, **_k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _nosleep)

    text = " ".join(["word"] * 120)  # ~600 bytes -> multiple parts

    async def flow():
        a._loop = asyncio.get_running_loop()
        return await a.send("!1", text)

    res = asyncio.run(flow())
    assert res.success is True
    assert len(sink) > 1
    assert all(len(s["text"].encode("utf-8")) <= adapter_mod._MAX_MESH_BYTES for s in sink)


def test_send_caps_parts_and_marks_truncation(adapter_mod, monkeypatch):
    sink: list = []
    _patch_send(monkeypatch, sink)
    a = _make(adapter_mod, monkeypatch)

    async def _nosleep(*_a, **_k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _nosleep)

    text = " ".join(["word"] * 1000)  # far more than _MAX_PARTS worth

    async def flow():
        a._loop = asyncio.get_running_loop()
        return await a.send("!1", text)

    asyncio.run(flow())
    assert len(sink) == adapter_mod._MAX_PARTS
    assert sink[-1]["text"].endswith("…")
    assert len(sink[-1]["text"].encode("utf-8")) <= adapter_mod._MAX_MESH_BYTES


def test_send_reports_failure_from_tool_error(adapter_mod, monkeypatch):
    sink: list = []
    _patch_send(monkeypatch, sink, result=json.dumps({"error": "Not connected."}))
    a = _make(adapter_mod, monkeypatch)

    async def flow():
        a._loop = asyncio.get_running_loop()
        return await a.send("!1", "hello")

    res = asyncio.run(flow())
    assert res.success is False
    assert res.error == "Not connected."


def test_send_stops_at_the_first_failing_part(adapter_mod, monkeypatch):
    sink: list = []
    from meshtastic_hermes import tools

    def fake_send_text(args, **kw):
        sink.append(args)
        return json.dumps({"error": "radio gone"}) if len(sink) == 2 else json.dumps({"sent": True})

    monkeypatch.setattr(tools, "send_text", fake_send_text)

    async def _nosleep(*_a, **_k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    a = _make(adapter_mod, monkeypatch)

    async def flow():
        a._loop = asyncio.get_running_loop()
        return await a.send("!1", " ".join(["word"] * 200))

    res = asyncio.run(flow())
    assert res.success is False
    assert len(sink) == 2  # aborted, did not keep flooding the mesh


# ----------------------------------------------------------------------
# get_chat_info
# ----------------------------------------------------------------------


def test_get_chat_info_distinguishes_dm_from_channel(adapter_mod, monkeypatch):
    a = _make(adapter_mod, monkeypatch)
    assert asyncio.run(a.get_chat_info("ch:1")) == {"name": "ch:1", "type": "group"}
    assert asyncio.run(a.get_chat_info("!abc")) == {"name": "!abc", "type": "dm"}


# ----------------------------------------------------------------------
# registration with the gateway runtime present
# ----------------------------------------------------------------------


def test_register_registers_the_platform(adapter_mod, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_HOST", "10.0.0.1")
    captured = {}

    class Ctx:
        def register_skill(self, name, path):
            captured.setdefault("skills", []).append(name)

        def register_platform(self, **kw):
            captured["platform"] = kw

    adapter_mod.register(Ctx())
    plat = captured["platform"]
    # Must match the `name:` in meshtastic_platform/plugin.yaml.
    assert plat["name"] == "meshtastic"
    assert plat["required_env"] == ["MESHTASTIC_HOST"]
    assert plat["max_message_length"] == 200
    assert plat["allowed_users_env"] == "MESHTASTIC_ALLOWED_USERS"
    assert plat["allow_all_env"] == "MESHTASTIC_ALLOW_ALL_USERS"
    # the factory really builds an adapter
    assert isinstance(plat["adapter_factory"](_Config()), adapter_mod.MeshtasticAdapter)


def test_register_warns_when_host_unset(adapter_mod, monkeypatch, caplog):
    import logging

    monkeypatch.delenv("MESHTASTIC_HOST", raising=False)
    caplog.set_level(logging.WARNING)

    class Ctx:
        def register_skill(self, name, path):
            pass

        def register_platform(self, **kw):
            pass

    adapter_mod.register(Ctx())
    assert "stay dormant" in caplog.text
