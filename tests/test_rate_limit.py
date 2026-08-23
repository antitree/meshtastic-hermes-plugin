"""Transmit rate limiting — security remediation item 3.

This is a loop breaker on a shared, legally regulated medium. Two bots on one
channel could otherwise answer each other forever, and a model in a tool loop could
call ``meshtastic_send_text`` without bound. The regressions worth holding here are
the negative ones: a refused send never reaches ``iface.sendData``, and a multi-part
reply spends one token per TRANSMITTED PACKET rather than one per logical reply.

No radio and no live Hermes: a fake interface is injected into the process-wide
ConnectionManager, and the clock is injected so nothing sleeps.
"""

from __future__ import annotations

import json

import pytest

from meshtastic_hermes import connection, rate_limit, tools

RL_ENV = (
    rate_limit.MAX_SENDS_PER_MINUTE_ENV,
    rate_limit.MAX_CHANNEL_SENDS_PER_MINUTE_ENV,
    rate_limit.MAX_DM_SENDS_PER_MINUTE_ENV,
    rate_limit.REPLY_COOLDOWN_SECONDS_ENV,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test states its own limits; nothing leaks in from the environment."""
    for var in (
        *RL_ENV,
        "MESHTASTIC_TOOL_SEND_CHANNELS",
        "MESHTASTIC_TOOL_SEND_ALLOW_PRIMARY",
        "MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST",
        "MESHTASTIC_REPLY_CHANNELS",
        "MESHTASTIC_REPLY_ALL",
    ):
        monkeypatch.delenv(var, raising=False)


class FakeClock:
    """An injected monotonic clock. Nothing in these tests ever sleeps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeIface:
    """Records every sendData call so a refused send is provably silent."""

    def __init__(self) -> None:
        self.sent: list = []

    def sendData(self, payload, **kwargs):
        self.sent.append({"payload": payload, **kwargs})


# index 0 = unnamed PRIMARY (public), 1 = "in.secure"
TABLE = [
    {"index": 0, "name": "", "role": 1},
    {"index": 1, "name": "in.secure", "role": 2},
]


@pytest.fixture
def iface(monkeypatch):
    """A fake radio wired into the process-wide manager, plus an open broadcast policy.

    Item 1's send policy is deliberately configured OPEN here so these tests fail for
    rate-limit reasons only — a policy rejection would otherwise mask the limiter and
    make every assertion pass for the wrong reason.
    """
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST", "true")
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_ALLOW_PRIMARY", "true")
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "in.secure,Primary")

    fake = FakeIface()
    mgr = connection.get_manager()
    monkeypatch.setattr(type(mgr), "iface", property(lambda self: fake))
    monkeypatch.setattr(type(mgr), "channel_table", lambda self: TABLE)
    return fake


def install(clock, **limits) -> rate_limit.TransmitLimiter:
    """Put a fake-time limiter into the shared slot every outbound path reads."""
    cfg = rate_limit.LimitConfig(**limits) if limits else rate_limit.LimitConfig()
    limiter = rate_limit.TransmitLimiter(config=cfg, time_fn=clock)
    rate_limit.set_limiter(limiter)
    return limiter


def dm(text: str = "hi", dest: str = "!deadbeef") -> dict:
    return json.loads(
        tools.send_text({"text": text, "dest_id": dest, "pki": True, "wait_ack": False})
    )


def broadcast(text: str = "hi", channel: int = 1) -> dict:
    return json.loads(
        tools.send_text({"text": text, "channel_index": channel, "wait_ack": False})
    )


# ----------------------------------------------------------------------
# The limiter in isolation (pure, fake time)
# ----------------------------------------------------------------------


def test_burst_above_global_limit_is_rejected():
    clock = FakeClock()
    limiter = rate_limit.TransmitLimiter(
        config=rate_limit.LimitConfig(
            max_sends_per_minute=3,
            max_channel_sends_per_minute=99,
            max_dm_sends_per_minute=99,
            reply_cooldown_seconds=0.001,
        ),
        time_fn=clock,
    )
    # Spread across DIFFERENT destinations so only the GLOBAL bucket can be the
    # thing that stops us — otherwise this would pass for the wrong reason.
    for i in range(3):
        clock.advance(1.0)
        limiter.check(dest_id=f"!node{i}")

    clock.advance(1.0)
    with pytest.raises(rate_limit.RateLimited) as exc:
        limiter.check(dest_id="!node9")
    assert exc.value.scope == "global"
    assert exc.value.retry_after_s > 0


def test_channel_and_dm_buckets_are_independent():
    clock = FakeClock()
    limiter = rate_limit.TransmitLimiter(
        config=rate_limit.LimitConfig(
            max_sends_per_minute=99,
            max_channel_sends_per_minute=2,
            max_dm_sends_per_minute=2,
            reply_cooldown_seconds=0.001,
        ),
        time_fn=clock,
    )
    for _ in range(2):
        clock.advance(1.0)
        limiter.check(channel_index=1)
    clock.advance(1.0)
    with pytest.raises(rate_limit.RateLimited) as exc:
        limiter.check(channel_index=1)
    assert exc.value.scope == "channel"

    # A different channel, and a DM, are untouched by the exhausted channel bucket.
    clock.advance(1.0)
    limiter.check(channel_index=2)
    clock.advance(1.0)
    limiter.check(dest_id="!deadbeef")


def test_limiter_resets_after_the_window():
    clock = FakeClock()
    limiter = rate_limit.TransmitLimiter(
        config=rate_limit.LimitConfig(
            max_sends_per_minute=2,
            max_channel_sends_per_minute=2,
            max_dm_sends_per_minute=2,
            reply_cooldown_seconds=0.001,
        ),
        time_fn=clock,
    )
    limiter.check(channel_index=1)
    clock.advance(1.0)
    limiter.check(channel_index=1)
    clock.advance(1.0)
    with pytest.raises(rate_limit.RateLimited):
        limiter.check(channel_index=1)

    # Past the window, the oldest stamps age out and the bucket admits again.
    clock.advance(rate_limit.WINDOW_SECONDS)
    limiter.check(channel_index=1)


def test_cooldown_spaces_consecutive_sends_to_one_destination():
    clock = FakeClock()
    limiter = rate_limit.TransmitLimiter(
        config=rate_limit.LimitConfig(reply_cooldown_seconds=10.0), time_fn=clock
    )
    limiter.check(dest_id="!deadbeef")
    clock.advance(4.0)
    with pytest.raises(rate_limit.RateLimited) as exc:
        limiter.check(dest_id="!deadbeef")
    assert exc.value.scope == "cooldown"
    assert exc.value.retry_after_s == pytest.approx(6.0)

    # A DIFFERENT peer is not held behind this peer's cooldown.
    limiter.check(dest_id="!0aca4a9c")

    clock.advance(6.0)
    limiter.check(dest_id="!deadbeef")


def test_refused_send_charges_nothing():
    """A rejection must not spend the global budget, or refusals would self-starve."""
    clock = FakeClock()
    limiter = rate_limit.TransmitLimiter(
        config=rate_limit.LimitConfig(
            max_sends_per_minute=99,
            max_channel_sends_per_minute=1,
            max_dm_sends_per_minute=99,
            reply_cooldown_seconds=0.001,
        ),
        time_fn=clock,
    )
    limiter.check(channel_index=1)
    for _ in range(5):
        clock.advance(1.0)
        with pytest.raises(rate_limit.RateLimited):
            limiter.check(channel_index=1)
    assert limiter.snapshot()["global"] == 1  # only the accepted send counted


# ----------------------------------------------------------------------
# Config parsing fails closed
# ----------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["nope", "0", "-5", "  ", "1e", "inf!"])
@pytest.mark.parametrize(
    "var,attr,default",
    [
        (
            rate_limit.MAX_SENDS_PER_MINUTE_ENV,
            "max_sends_per_minute",
            rate_limit.DEFAULT_MAX_SENDS_PER_MINUTE,
        ),
        (
            rate_limit.MAX_CHANNEL_SENDS_PER_MINUTE_ENV,
            "max_channel_sends_per_minute",
            rate_limit.DEFAULT_MAX_CHANNEL_SENDS_PER_MINUTE,
        ),
        (
            rate_limit.MAX_DM_SENDS_PER_MINUTE_ENV,
            "max_dm_sends_per_minute",
            rate_limit.DEFAULT_MAX_DM_SENDS_PER_MINUTE,
        ),
        (
            rate_limit.REPLY_COOLDOWN_SECONDS_ENV,
            "reply_cooldown_seconds",
            rate_limit.DEFAULT_REPLY_COOLDOWN_SECONDS,
        ),
    ],
)
def test_invalid_config_falls_back_to_the_conservative_default(
    monkeypatch, var, attr, default, bad
):
    """There is deliberately no spelling of 'unlimited'. A typo must not disable the
    limiter — that would turn a config mistake into an unbounded transmitter."""
    monkeypatch.setenv(var, bad)
    assert getattr(rate_limit.LimitConfig.from_env(), attr) == default


def test_valid_config_is_honored(monkeypatch):
    monkeypatch.setenv(rate_limit.MAX_SENDS_PER_MINUTE_ENV, "42")
    monkeypatch.setenv(rate_limit.REPLY_COOLDOWN_SECONDS_ENV, "2.5")
    cfg = rate_limit.LimitConfig.from_env()
    assert cfg.max_sends_per_minute == 42
    assert cfg.reply_cooldown_seconds == 2.5


# ----------------------------------------------------------------------
# Through the tool handler — a refused send must not reach the radio
# ----------------------------------------------------------------------


def test_tool_send_returns_the_documented_json_shape(iface):
    clock = FakeClock()
    install(clock, max_sends_per_minute=1, reply_cooldown_seconds=0.001)

    assert dm("first")["sent"] is True
    clock.advance(1.0)

    out = dm("second")
    assert out["error"] == rate_limit.RATE_LIMITED
    assert isinstance(out["retry_after_s"], (int, float))
    assert out["retry_after_s"] > 0


def test_rejected_tool_send_never_calls_send_data(iface):
    """The core negative regression: refusal is SILENT on the air."""
    clock = FakeClock()
    install(clock, max_sends_per_minute=1, reply_cooldown_seconds=0.001)

    dm("first")
    assert len(iface.sent) == 1

    for _ in range(5):
        clock.advance(1.0)
        assert dm("blocked")["error"] == rate_limit.RATE_LIMITED
    assert len(iface.sent) == 1  # nothing else ever went out


def test_tool_dm_and_broadcast_buckets_are_independent_end_to_end(iface):
    clock = FakeClock()
    install(
        clock,
        max_sends_per_minute=99,
        max_channel_sends_per_minute=1,
        max_dm_sends_per_minute=1,
        reply_cooldown_seconds=0.001,
    )

    assert broadcast("a", channel=1)["sent"] is True
    clock.advance(1.0)
    assert broadcast("b", channel=1)["error"] == rate_limit.RATE_LIMITED

    # The DM bucket is untouched by the exhausted channel bucket.
    clock.advance(1.0)
    assert dm("c")["sent"] is True
    assert len(iface.sent) == 2


def test_tool_send_recovers_after_the_window(iface):
    clock = FakeClock()
    install(clock, max_sends_per_minute=1, reply_cooldown_seconds=0.001)

    dm("first")
    clock.advance(1.0)
    assert dm("blocked")["error"] == rate_limit.RATE_LIMITED

    clock.advance(rate_limit.WINDOW_SECONDS)
    assert dm("later")["sent"] is True
    assert len(iface.sent) == 2


# ----------------------------------------------------------------------
# Shared across BOTH copies of the package
# ----------------------------------------------------------------------


def test_limiter_state_lives_in_a_fixed_sys_modules_slot():
    """Hermes loads the tools plugin under a mangled package name while the adapter
    imports ``meshtastic_hermes`` top-level, so TWO module objects with two sets of
    globals exist. A module-global limiter would give each its own bucket — double
    the configured limit and no shared loop breaker. Simulate the second copy by
    importing the module afresh and check both see one limiter."""
    import importlib
    import sys

    first = rate_limit.get_limiter()

    # Force a genuinely separate module object, as Hermes' mangled import does.
    saved = sys.modules.pop("meshtastic_hermes.rate_limit")
    try:
        second_copy = importlib.import_module("meshtastic_hermes.rate_limit")
        assert second_copy is not saved, "expected a distinct module object"
        assert second_copy.get_limiter() is first
        assert second_copy._SHARED_KEY == saved._SHARED_KEY
        assert second_copy._SHARED_KEY in sys.modules
    finally:
        sys.modules["meshtastic_hermes.rate_limit"] = saved


# ----------------------------------------------------------------------
# The continuation exemption is narrow: cooldown only, never the buckets
# ----------------------------------------------------------------------


def test_continuation_skips_the_cooldown_but_still_costs_a_token():
    """Parts 2..n of one answer must not be held behind the reply cooldown — a reply
    longer than one packet would otherwise be undeliverable at any sane cooldown.
    They must still be CHARGED, or the exemption would become the flood hole."""
    clock = FakeClock()
    limiter = rate_limit.TransmitLimiter(
        config=rate_limit.LimitConfig(
            max_sends_per_minute=99,
            max_channel_sends_per_minute=99,
            max_dm_sends_per_minute=99,
            reply_cooldown_seconds=30.0,
        ),
        time_fn=clock,
    )
    limiter.check(dest_id="!deadbeef")
    for _ in range(3):
        limiter.check(dest_id="!deadbeef", continuation=True)  # no time advanced
    assert limiter.snapshot()["global"] == 4
    assert limiter.snapshot()["dm:!deadbeef"] == 4

    # A NEW turn is still held by the cooldown.
    with pytest.raises(rate_limit.RateLimited) as exc:
        limiter.check(dest_id="!deadbeef")
    assert exc.value.scope == "cooldown"


def test_continuation_is_still_bounded_by_the_buckets():
    """The exemption is cooldown-only. A continuation cannot exceed the bucket."""
    clock = FakeClock()
    limiter = rate_limit.TransmitLimiter(
        config=rate_limit.LimitConfig(
            max_sends_per_minute=2,
            max_channel_sends_per_minute=99,
            max_dm_sends_per_minute=99,
            reply_cooldown_seconds=30.0,
        ),
        time_fn=clock,
    )
    limiter.check(dest_id="!deadbeef")
    limiter.check(dest_id="!deadbeef", continuation=True)
    with pytest.raises(rate_limit.RateLimited) as exc:
        limiter.check(dest_id="!deadbeef", continuation=True)
    assert exc.value.scope == "global"
