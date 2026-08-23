"""Mention gating at the adapter seam: env config, identity, and the loud warning.

Reuses the stub-gateway fixture and fake ConnectionManager from
``tests/test_adapter_runtime`` (see that module's docstring for what the stub is
and what it deliberately does not model), and drives the real
``MeshtasticAdapter`` so the env -> identity -> gate wiring is exercised where it
actually lives. The pure matcher is tested in tests/test_mention_gating.py.

Honest limit: like every test here, this runs against a fake radio. Nothing in
this file demonstrates on-air behavior.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

# `tests/` is not a package (no __init__.py), so import the sibling module by its
# bare name — pytest puts the test directory on sys.path under rootdir insertion.
from test_adapter_runtime import (  # noqa: F401 - adapter_mod is imported as a fixture
    _FakeManager,
    _make,
    _patch_manager,
    _text_packet,
    adapter_mod,
)

# _FakeManager reports short_name="MESH", long_name="Meshy Gateway", node_id="!aabbccdd".
SHORT = "MESH"


def _connect(a):
    assert asyncio.run(a.connect()) is True


def _run(a, *packets):
    """Connect, deliver packets on the RX thread, let the loop settle."""

    async def flow():
        await a.connect()
        for packet in packets:
            a._on_rx(packet)
        await asyncio.sleep(0.05)

    asyncio.run(flow())
    return [e.text for e in a.handled]


# ----------------------------------------------------------------------
# MESHTASTIC_REQUIRE_MENTION parsing
# ----------------------------------------------------------------------


def test_require_mention_defaults_to_on(adapter_mod, monkeypatch):  # noqa: F811
    monkeypatch.delenv("MESHTASTIC_REQUIRE_MENTION", raising=False)
    assert adapter_mod._require_mention_from_env() is True
    assert _make(adapter_mod, monkeypatch).require_mention is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "No", "  no  ", "nO"])
def test_falsey_values_disable_gating(adapter_mod, monkeypatch, value):  # noqa: F811
    """Matches the case-insensitive convention MESHTASTIC_REPLY_ALL uses."""
    monkeypatch.setenv("MESHTASTIC_REQUIRE_MENTION", value)
    assert adapter_mod._require_mention_from_env() is False
    assert _make(adapter_mod, monkeypatch).require_mention is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "", "off", "nope", "flase"])
def test_anything_else_leaves_gating_on(adapter_mod, monkeypatch, value):  # noqa: F811
    """Fail closed on typos: the cost of an accidental 'off' is a transmission loop.

    Note "off" and "flase" are deliberately in this list — they are NOT accepted
    as falsey, so a misconfigured operator keeps the safe behavior.
    """
    monkeypatch.setenv("MESHTASTIC_REQUIRE_MENTION", value)
    assert adapter_mod._require_mention_from_env() is True


# ----------------------------------------------------------------------
# inbound behavior
# ----------------------------------------------------------------------


def test_unaddressed_channel_message_is_ignored_when_gating_is_on(adapter_mod, monkeypatch):  # noqa: F811
    """The core safety property at the adapter seam."""
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "2")
    monkeypatch.delenv("MESHTASTIC_REQUIRE_MENTION", raising=False)
    _patch_manager(monkeypatch, _FakeManager(node_id="!aabbccdd"))
    a = _make(adapter_mod, monkeypatch)
    a._message_handler = object()

    handled = _run(
        a,
        _text_packet("what is the weather?", to_id="^all", channel=2),
        _text_packet("ask MESH about the weather", to_id="^all", channel=2),
        _text_packet("MESHY is a great bot", to_id="^all", channel=2),
    )
    assert handled == [], "no mention at the start => no transmission"


def test_addressed_channel_message_is_dispatched_with_the_mention_stripped(
    adapter_mod, monkeypatch  # noqa: F811
):
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "2")
    _patch_manager(monkeypatch, _FakeManager(node_id="!aabbccdd"))
    a = _make(adapter_mod, monkeypatch)
    a._message_handler = object()

    handled = _run(
        a,
        _text_packet("MESH weather now", to_id="^all", channel=2),
        _text_packet("@mesh: weather again", to_id="^all", channel=2),
        _text_packet("Meshy Gateway weather thrice", to_id="^all", channel=2),
        _text_packet("!aabbccdd weather by id", to_id="^all", channel=2),
    )
    assert handled == [
        "weather now",
        "weather again",
        "weather thrice",
        "weather by id",
    ]


def test_dms_are_answered_without_a_mention(adapter_mod, monkeypatch):  # noqa: F811
    """User decision 1: gating applies to CHANNELS only."""
    _patch_manager(monkeypatch, _FakeManager(node_id="!aabbccdd"))
    a = _make(adapter_mod, monkeypatch)
    a._message_handler = object()

    handled = _run(a, _text_packet("weather now", to_id="!aabbccdd"))
    assert handled == ["weather now"], "a DM is already addressed to this node"


def test_gating_off_restores_reply_to_every_channel_message(adapter_mod, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "2")
    monkeypatch.setenv("MESHTASTIC_REQUIRE_MENTION", "false")
    _patch_manager(monkeypatch, _FakeManager(node_id="!aabbccdd"))
    a = _make(adapter_mod, monkeypatch)
    a._message_handler = object()

    handled = _run(
        a,
        _text_packet("no mention here", to_id="^all", channel=2),
        _text_packet("MESH weather", to_id="^all", channel=2),
    )
    # Nothing is stripped when the gate is off.
    assert handled == ["no mention here", "MESH weather"]


def test_channel_allowlist_still_applies_before_the_mention_gate(adapter_mod, monkeypatch):  # noqa: F811
    """A mention on a channel that is not allowlisted is still silence."""
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "2")
    _patch_manager(monkeypatch, _FakeManager(node_id="!aabbccdd"))
    a = _make(adapter_mod, monkeypatch)
    a._message_handler = object()

    handled = _run(a, _text_packet("MESH weather", to_id="^all", channel=5))
    assert handled == []


# ----------------------------------------------------------------------
# identity plumbing
# ----------------------------------------------------------------------


def test_identity_is_refreshed_from_the_radio_on_connect(adapter_mod, monkeypatch):  # noqa: F811
    _patch_manager(monkeypatch, _FakeManager(node_id="!aabbccdd"))
    a = _make(adapter_mod, monkeypatch)

    assert not a.identity, "before connect there is no identity — gating fails closed"
    _connect(a)
    assert a.identity.node_id == "!aabbccdd"
    assert a.identity.short_name == SHORT
    assert a.identity.long_name == "Meshy Gateway"


def test_missing_identity_fails_closed_and_warns(adapter_mod, monkeypatch, caplog):  # noqa: F811
    """No identifier at all: drop channel traffic, never reply to everyone."""
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "2")
    mgr = _FakeManager(node_id="!aabbccdd")
    mgr.local_node_identity = lambda: {
        "node_id": None,
        "short_name": None,
        "long_name": None,
    }
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)
    a._message_handler = object()

    with caplog.at_level(logging.WARNING):
        handled = _run(a, _text_packet("MESH weather", to_id="^all", channel=2))

    assert handled == [], "unknown identity must not mean 'reply to everything'"
    assert "failing closed" in caplog.text.lower()
    assert a.state == "connected", "a missing name must not take the adapter down"


def test_degraded_identity_still_gates_on_the_node_id_and_warns(
    adapter_mod, monkeypatch, caplog  # noqa: F811
):
    """Names arrive late from the node DB; the node id works immediately."""
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "2")
    mgr = _FakeManager(node_id="!aabbccdd")
    mgr.local_node_identity = lambda: {
        "node_id": "!aabbccdd",
        "short_name": None,
        "long_name": None,
    }
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)
    a._message_handler = object()

    with caplog.at_level(logging.WARNING):
        handled = _run(
            a,
            _text_packet("!aabbccdd weather", to_id="^all", channel=2),
            _text_packet("MESH weather", to_id="^all", channel=2),
        )

    assert handled == ["weather"], "node id gates; the unknown short name does not"
    assert "degraded" in caplog.text.lower()


def test_no_identity_warning_when_gating_is_off(adapter_mod, monkeypatch, caplog):  # noqa: F811
    """Nothing is degraded if we aren't gating in the first place."""
    monkeypatch.setenv("MESHTASTIC_REQUIRE_MENTION", "false")
    mgr = _FakeManager(node_id="!aabbccdd")
    mgr.local_node_identity = lambda: {"node_id": None, "short_name": None, "long_name": None}
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)

    with caplog.at_level(logging.WARNING):
        _connect(a)
    assert "failing closed" not in caplog.text.lower()


# ----------------------------------------------------------------------
# the loud unsafe-combination warning
# ----------------------------------------------------------------------


def _warn(adapter_mod, allowed, require_mention):  # noqa: F811
    return adapter_mod.warn_if_reply_scope_is_unsafe(allowed, require_mention)


def test_warning_fires_for_reply_all_with_gating_off(adapter_mod, caplog):  # noqa: F811
    from meshtastic_hermes import gateway_bridge as gb

    with caplog.at_level(logging.WARNING):
        assert _warn(adapter_mod, gb.ALL_CHANNELS, False) is True

    text = caplog.text
    assert "UNSAFE MESHTASTIC REPLY CONFIGURATION" in text
    assert "TRANSMISSION LOOP RISK" in text
    # It must name the specific risk, not just say "be careful".
    assert "OTHER BOTS" in text
    assert "NO rate limit" in text
    assert "REGULATED" in text.upper()
    assert "MESHTASTIC_REQUIRE_MENTION" in text
    assert text.count("\n") > 5, "must be a multi-line block that is hard to miss"
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_warning_fires_when_the_allowlist_includes_public_primary(adapter_mod, caplog):  # noqa: F811
    with caplog.at_level(logging.WARNING):
        assert _warn(adapter_mod, {0}, False) is True
    assert "Primary" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert _warn(adapter_mod, {0, 2}, False) is True
    assert "UNSAFE" in caplog.text


@pytest.mark.parametrize(
    "allowed,require_mention",
    [
        ("__all__", True),   # broad scope, but gated — safe
        ({0}, True),         # public Primary, but gated — safe
        ({2}, False),        # ungated, but a narrow private channel
        ({2, 3}, False),
        (None, False),       # DMs only
        (None, True),
    ],
)
def test_warning_stays_quiet_for_safe_combinations(adapter_mod, caplog, allowed, require_mention):  # noqa: F811
    with caplog.at_level(logging.WARNING):
        assert _warn(adapter_mod, allowed, require_mention) is False
    assert "UNSAFE" not in caplog.text


def test_warning_is_emitted_on_connect_and_re_emitted_on_reconnect(
    adapter_mod, monkeypatch, caplog  # noqa: F811
):
    """It must be re-emitted every connect, not buried once in the boot log."""
    monkeypatch.setenv("MESHTASTIC_REPLY_ALL", "true")
    monkeypatch.setenv("MESHTASTIC_REQUIRE_MENTION", "false")
    _patch_manager(monkeypatch, _FakeManager(node_id="!aabbccdd"))
    a = _make(adapter_mod, monkeypatch)

    with caplog.at_level(logging.WARNING):
        _connect(a)
    assert caplog.text.count("UNSAFE MESHTASTIC REPLY CONFIGURATION") == 1

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(a.connect(is_reconnect=True)) is True
    assert caplog.text.count("UNSAFE MESHTASTIC REPLY CONFIGURATION") == 1


def test_unsafe_config_still_connects(adapter_mod, monkeypatch, caplog):  # noqa: F811
    """User decision 3: warn loudly, but honor the operator's config."""
    monkeypatch.setenv("MESHTASTIC_REPLY_ALL", "true")
    monkeypatch.setenv("MESHTASTIC_REQUIRE_MENTION", "0")
    _patch_manager(monkeypatch, _FakeManager(node_id="!aabbccdd"))
    a = _make(adapter_mod, monkeypatch)
    a._message_handler = object()

    with caplog.at_level(logging.WARNING):
        handled = _run(a, _text_packet("no mention", to_id="^all", channel=0))

    assert a.state == "connected"
    assert a.fatal is None
    assert handled == ["no mention"], "the config is honored, not refused"
    assert "UNSAFE" in caplog.text


def test_default_config_produces_no_unsafe_warning(adapter_mod, monkeypatch, caplog):  # noqa: F811
    monkeypatch.delenv("MESHTASTIC_REPLY_ALL", raising=False)
    monkeypatch.delenv("MESHTASTIC_REPLY_CHANNELS", raising=False)
    monkeypatch.delenv("MESHTASTIC_REQUIRE_MENTION", raising=False)
    _patch_manager(monkeypatch, _FakeManager(node_id="!aabbccdd"))
    a = _make(adapter_mod, monkeypatch)

    with caplog.at_level(logging.WARNING):
        _connect(a)
    assert "UNSAFE" not in caplog.text
