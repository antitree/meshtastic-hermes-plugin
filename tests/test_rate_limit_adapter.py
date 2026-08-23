"""Transmit rate limiting through the PLATFORM ADAPTER — remediation item 3.

Two properties are pinned here that the limiter-level tests cannot reach:

1. **One token per transmitted PACKET.** A long reply is chunked by the adapter into
   several mesh packets. Each is separate airtime, so each must cost a token. A
   limiter applied per logical *reply* would let one verbose answer emit five packets
   against a single token — exactly the flood this item exists to stop.
2. **Adapter sends and tool sends share ONE limiter.** Hermes loads the tools plugin
   under a mangled package name while the adapter imports ``meshtastic_hermes``
   top-level, so two module copies exist. If the limiter were a module global, the
   adapter and the tool would each get a bucket and the loop breaker would be
   trivially bypassed.

The adapter class only exists when the Hermes gateway runtime is importable, which it
is not outside Hermes — so, as in ``test_adapter_runtime.py``, a stub gateway package
is installed into ``sys.modules`` and the adapter re-imported against it. No radio: a
fake interface is injected into the process-wide ConnectionManager.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

import pytest

from meshtastic_hermes import connection, rate_limit, tools

# ----------------------------------------------------------------------
# Stub gateway runtime (kept local: importing fixtures across test modules
# resolves in-tree but breaks when the package is installed in CI)
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


class _BasePlatformAdapter(ABC):
    def __init__(self, config=None, platform=None):
        self.config = config
        self.platform = platform
        self._message_handler = None
        self.state = "init"
        self.fatal = None
        self.handled: list = []

    def _set_fatal_error(self, code, message, *, retryable):
        self.fatal = {"code": code, "message": message, "retryable": retryable}

    def _mark_connected(self):
        self.state = "connected"

    def _mark_disconnected(self):
        self.state = "disconnected"

    def build_source(self, **kw):
        return dict(kw)

    async def handle_message(self, event):
        self.handled.append(event)

    @abstractmethod
    async def connect(self, *, is_reconnect: bool = False) -> bool: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def send(self, chat_id, content, reply_to=None, metadata=None): ...

    @abstractmethod
    async def get_chat_info(self, chat_id): ...


@dataclass
class _Config:
    extra: dict = field(default_factory=dict)


@pytest.fixture
def adapter_mod(monkeypatch):
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

    for name in ("gateway", "gateway.config", "gateway.platforms", "gateway.platforms.base"):
        sys.modules.pop(name, None)
    importlib.reload(mod)


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeIface:
    """Records every sendData call — the ground truth for 'what went on the air'."""

    def __init__(self) -> None:
        self.sent: list = []

    def sendData(self, payload, **kwargs):
        self.sent.append({"payload": payload, **kwargs})


TABLE = [
    {"index": 0, "name": "", "role": 1},
    {"index": 1, "name": "in.secure", "role": 2},
]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for var in (
        rate_limit.MAX_SENDS_PER_MINUTE_ENV,
        rate_limit.MAX_CHANNEL_SENDS_PER_MINUTE_ENV,
        rate_limit.MAX_DM_SENDS_PER_MINUTE_ENV,
        rate_limit.REPLY_COOLDOWN_SECONDS_ENV,
    ):
        monkeypatch.delenv(var, raising=False)
    # Item 1's policy is opened deliberately so these tests can only fail for
    # rate-limit reasons, never because a broadcast was refused by policy.
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST", "true")
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_ALLOW_PRIMARY", "true")
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "in.secure,Primary")


@pytest.fixture
def iface(monkeypatch):
    fake = FakeIface()
    mgr = connection.get_manager()
    monkeypatch.setattr(type(mgr), "iface", property(lambda self: fake))
    monkeypatch.setattr(type(mgr), "channel_table", lambda self: TABLE)
    return fake


def install(clock, **limits):
    limiter = rate_limit.TransmitLimiter(config=rate_limit.LimitConfig(**limits), time_fn=clock)
    rate_limit.set_limiter(limiter)
    return limiter


def make(adapter_mod, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_HOST", "192.0.2.10")
    return adapter_mod.MeshtasticAdapter(_Config())


def run_send(adapter, chat_id, content, monkeypatch):
    """Drive adapter.send() with the inter-part pacing sleep stubbed out."""

    async def _nosleep(*_a, **_k):
        return None

    monkeypatch.setattr(asyncio, "sleep", _nosleep)

    async def flow():
        adapter._loop = asyncio.get_running_loop()
        return await adapter.send(chat_id, content)

    return asyncio.run(flow())


# ----------------------------------------------------------------------
# THE subtle requirement: one token per transmitted PACKET
# ----------------------------------------------------------------------


def test_multipart_reply_consumes_one_token_per_transmitted_packet(
    adapter_mod, monkeypatch, iface
):
    """A reply that chunks into 5 packets must spend 5 tokens, not 1.

    The budget here is 3. If the limiter charged per logical reply, all 5 packets
    would go out on one token; charging per packet stops it after 3.
    """
    clock = FakeClock()
    install(
        clock,
        max_sends_per_minute=3,
        max_channel_sends_per_minute=99,
        max_dm_sends_per_minute=99,
        reply_cooldown_seconds=0.0001,
    )
    a = make(adapter_mod, monkeypatch)

    long_text = " ".join(["word"] * 1000)  # far more than _MAX_PARTS worth
    parts = adapter_mod._split_text(long_text, adapter_mod._MAX_MESH_BYTES)
    assert len(parts) > adapter_mod._MAX_PARTS  # the adapter will cap it at _MAX_PARTS

    res = run_send(a, "!deadbeef", long_text, monkeypatch)

    # Exactly the budget went on the air, and the reply reports failure rather than
    # pretending a truncated transmission succeeded.
    assert len(iface.sent) == 3
    assert res.success is False
    assert res.error == rate_limit.RATE_LIMITED
    assert rate_limit.get_limiter().snapshot()["global"] == 3


def test_multipart_reply_within_budget_sends_every_part(adapter_mod, monkeypatch, iface):
    """The converse: with enough budget all parts go out, and all are charged."""
    clock = FakeClock()
    install(
        clock,
        max_sends_per_minute=50,
        max_channel_sends_per_minute=50,
        max_dm_sends_per_minute=50,
        reply_cooldown_seconds=0.0001,
    )
    a = make(adapter_mod, monkeypatch)

    text = " ".join(["word"] * 120)  # a few hundred bytes -> several parts
    expected = len(adapter_mod._split_text(text, adapter_mod._MAX_MESH_BYTES))
    assert 1 < expected <= adapter_mod._MAX_PARTS

    res = run_send(a, "!deadbeef", text, monkeypatch)

    assert res.success is True
    assert len(iface.sent) == expected
    # One token per PACKET — not one per reply.
    assert rate_limit.get_limiter().snapshot()["global"] == expected


def test_single_part_reply_costs_exactly_one_token(adapter_mod, monkeypatch, iface):
    clock = FakeClock()
    install(
        clock,
        max_sends_per_minute=50,
        max_channel_sends_per_minute=50,
        max_dm_sends_per_minute=50,
        reply_cooldown_seconds=0.0001,
    )
    a = make(adapter_mod, monkeypatch)

    res = run_send(a, "!deadbeef", "short answer", monkeypatch)
    assert res.success is True
    assert len(iface.sent) == 1
    assert rate_limit.get_limiter().snapshot()["global"] == 1


# ----------------------------------------------------------------------
# Adapter replies and tool sends consume the SAME limiter state
# ----------------------------------------------------------------------


def test_adapter_and_tool_sends_share_one_limiter(adapter_mod, monkeypatch, iface):
    """The cross-module-copy requirement, exercised end to end.

    A tool send spends the budget; the adapter reply that follows is refused by the
    SAME bucket. If each path had its own limiter this would happily transmit.
    """
    clock = FakeClock()
    install(
        clock,
        max_sends_per_minute=2,
        max_channel_sends_per_minute=99,
        max_dm_sends_per_minute=99,
        reply_cooldown_seconds=0.0001,
    )
    a = make(adapter_mod, monkeypatch)

    # Two tool sends to DIFFERENT peers exhaust the global budget.
    for peer in ("!aaaa1111", "!bbbb2222"):
        clock.advance(1.0)
        out = json.loads(
            tools.send_text({"text": "hi", "dest_id": peer, "pki": True, "wait_ack": False})
        )
        assert out["sent"] is True
    assert len(iface.sent) == 2

    clock.advance(1.0)
    res = run_send(a, "!cccc3333", "adapter reply", monkeypatch)
    assert res.success is False
    assert res.error == rate_limit.RATE_LIMITED
    assert len(iface.sent) == 2  # the adapter transmitted NOTHING


def test_adapter_send_then_tool_send_share_one_limiter(adapter_mod, monkeypatch, iface):
    """And the other direction: an adapter reply spends budget the tool then lacks."""
    clock = FakeClock()
    install(
        clock,
        max_sends_per_minute=1,
        max_channel_sends_per_minute=99,
        max_dm_sends_per_minute=99,
        reply_cooldown_seconds=0.0001,
    )
    a = make(adapter_mod, monkeypatch)

    res = run_send(a, "!deadbeef", "short", monkeypatch)
    assert res.success is True
    assert len(iface.sent) == 1

    clock.advance(1.0)
    out = json.loads(
        tools.send_text(
            {"text": "hi", "dest_id": "!0aca4a9c", "pki": True, "wait_ack": False}
        )
    )
    assert out["error"] == rate_limit.RATE_LIMITED
    assert len(iface.sent) == 1


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------


def test_rate_limited_reply_returns_send_result_failure_and_logs(
    adapter_mod, monkeypatch, iface, caplog
):
    clock = FakeClock()
    install(
        clock,
        max_sends_per_minute=99,
        max_channel_sends_per_minute=99,
        max_dm_sends_per_minute=99,
        reply_cooldown_seconds=30.0,
    )
    a = make(adapter_mod, monkeypatch)

    assert run_send(a, "!deadbeef", "first", monkeypatch).success is True

    caplog.clear()
    with caplog.at_level("WARNING"):
        clock.advance(1.0)
        res = run_send(a, "!deadbeef", "second", monkeypatch)

    assert res.success is False
    assert res.error == "rate_limited"
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "rate limited" in joined.lower()
    assert "retry_after_s" in joined
    assert len(iface.sent) == 1  # the cooled-down reply never transmitted


def test_connect_logs_the_effective_transmit_budget(adapter_mod, monkeypatch, caplog):
    """An operator's only lever over airtime is these four vars, so the effective
    values are re-stated on every connect — and an invalid one is reported as the
    conservative default that will actually be enforced, not as what was typed."""
    monkeypatch.setenv(rate_limit.MAX_SENDS_PER_MINUTE_ENV, "7")
    monkeypatch.setenv(rate_limit.REPLY_COOLDOWN_SECONDS_ENV, "not-a-number")

    with caplog.at_level("INFO"):
        budget = adapter_mod.log_transmit_budget()

    assert budget["MESHTASTIC_MAX_SENDS_PER_MINUTE"] == 7
    assert (
        budget["MESHTASTIC_REPLY_COOLDOWN_SECONDS"]
        == rate_limit.DEFAULT_REPLY_COOLDOWN_SECONDS
    )
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "transmit budget" in joined.lower()
    assert "7 sends/min" in joined
