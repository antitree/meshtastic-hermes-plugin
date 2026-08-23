"""Message bodies must not reach the gateway log just because debug logging is on.

Mesh traffic is private: a channel message is encrypted with that channel's PSK
and a direct message is end-to-end (PKI) encrypted to this node's keypair. The
node decrypts both for us, so a debug log line carrying the plaintext copies
someone else's private message into the journal, where it is retained and rotated
long after the packet is gone. ``MESHTASTIC_DEBUG`` therefore buys verbosity, not
disclosure; bodies require the separate, explicit ``MESHTASTIC_DEBUG_LOG_TEXT``.

Reuses the stub-gateway fixture and fake ConnectionManager from
``tests/test_adapter_runtime`` (see that module's docstring for what the stub is
and what it deliberately does not model) so the real ``_on_rx`` emits the real
log record.

Honest limit: like every test here, this runs against a fake radio. Nothing in
this file demonstrates on-air behavior, and it says nothing about what the
``meshtastic`` library itself logs.
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

# A body that cannot plausibly occur anywhere else in a log line, so a bare
# substring check over the captured records is a meaningful leak test.
SECRET = "zqx-grid-square-EN61-rendezvous-0430Z"

# _FakeManager reports short_name="MESH"; a channel message needs the mention to
# clear gate 2, and the gate strips it before the body is logged.
MENTIONED = f"MESH {SECRET}"


@pytest.fixture(autouse=True)
def _debug_env(monkeypatch):
    """Verbose logging on, body logging left at its default (off)."""
    monkeypatch.setenv("MESHTASTIC_DEBUG", "1")
    monkeypatch.delenv("MESHTASTIC_DEBUG_LOG_TEXT", raising=False)


def _rx(mod, monkeypatch, packet, *, reply_channels=None):
    """Deliver one packet through the real _on_rx and return the adapter."""
    if reply_channels is not None:
        monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", reply_channels)
    _patch_manager(monkeypatch, _FakeManager(node_id="!aabbccdd"))
    a = _make(mod, monkeypatch)
    a._message_handler = object()  # _dispatch no-ops without one

    async def flow():
        await a.connect()
        a._on_rx(packet)
        await asyncio.sleep(0.05)

    asyncio.run(flow())
    return a


def _captured(caplog):
    """Every captured record's rendered message, joined."""
    return "\n".join(r.getMessage() for r in caplog.records)


def _inbound_line(caplog):
    lines = [m for m in _captured(caplog).splitlines() if m.startswith("inbound ")]
    assert lines, f"no inbound debug line was logged; got: {_captured(caplog)!r}"
    return lines[-1]


# ----------------------------------------------------------------------
# the helper
# ----------------------------------------------------------------------


def test_helper_redacts_by_default(adapter_mod, monkeypatch):  # noqa: F811
    monkeypatch.delenv("MESHTASTIC_DEBUG_LOG_TEXT", raising=False)
    out = adapter_mod.debug_text_for_log("hello")
    assert SECRET not in out
    assert "hello" not in out
    assert "text_len=5" in out
    assert "text_sha256=" in out


def test_helper_reports_the_real_length_and_a_stable_hash(adapter_mod, monkeypatch):  # noqa: F811
    monkeypatch.delenv("MESHTASTIC_DEBUG_LOG_TEXT", raising=False)
    out = adapter_mod.debug_text_for_log(SECRET)
    assert f"text_len={len(SECRET)}" in out
    # Same input -> same digest (so a duplicate/retransmit is still recognizable),
    # different input -> different digest (so two messages are distinguishable).
    assert out == adapter_mod.debug_text_for_log(SECRET)
    assert out != adapter_mod.debug_text_for_log(SECRET + "!")


def test_helper_returns_the_body_when_explicitly_enabled(adapter_mod, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MESHTASTIC_DEBUG_LOG_TEXT", "true")
    assert SECRET in adapter_mod.debug_text_for_log(SECRET)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " True "])
def test_helper_accepts_truthy_spellings(adapter_mod, monkeypatch, value):  # noqa: F811
    monkeypatch.setenv("MESHTASTIC_DEBUG_LOG_TEXT", value)
    assert SECRET in adapter_mod.debug_text_for_log(SECRET)


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "ture", "maybe"])
def test_helper_fails_closed_on_anything_else(adapter_mod, monkeypatch, value):  # noqa: F811
    """Only an explicit truthy value enables disclosure — a typo redacts.

    This is the mirror image of MESHTASTIC_REQUIRE_MENTION, where only an explicit
    falsey value disables the safety. Both fail towards the safe state.
    """
    monkeypatch.setenv("MESHTASTIC_DEBUG_LOG_TEXT", value)
    out = adapter_mod.debug_text_for_log(SECRET)
    assert SECRET not in out
    assert "text_len=" in out


def test_helper_handles_an_empty_and_a_missing_body(adapter_mod, monkeypatch):  # noqa: F811
    monkeypatch.delenv("MESHTASTIC_DEBUG_LOG_TEXT", raising=False)
    assert "text_len=0" in adapter_mod.debug_text_for_log("")
    assert adapter_mod.debug_text_for_log(None) == "text=<none>"


def test_helper_does_not_leak_a_non_string_body(adapter_mod, monkeypatch):  # noqa: F811
    """A malformed packet field must not slip past the redaction as a repr."""
    monkeypatch.delenv("MESHTASTIC_DEBUG_LOG_TEXT", raising=False)
    out = adapter_mod.debug_text_for_log({"secret": SECRET})
    assert SECRET not in out


# ----------------------------------------------------------------------
# _on_rx — DMs
# ----------------------------------------------------------------------


def test_dm_body_is_absent_from_debug_logs(adapter_mod, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    _rx(adapter_mod, monkeypatch, _text_packet(SECRET))
    assert SECRET not in _captured(caplog), "the DM body reached the gateway log"


def test_dm_debug_line_keeps_the_routing_context(adapter_mod, monkeypatch, caplog):  # noqa: F811
    """Redaction must not cost us the fields that make a decision diagnosable."""
    caplog.set_level(logging.DEBUG)
    _rx(adapter_mod, monkeypatch, _text_packet(SECRET))
    line = _inbound_line(caplog)
    assert "DM" in line  # message type
    assert "ch=0" in line  # channel
    assert "from=!11112222" in line  # sender node id
    assert "REPLY" in line  # routing decision
    assert f"text_len={len(SECRET)}" in line  # length, not body
    assert "text_sha256=" in line


def test_dm_body_is_logged_when_explicitly_enabled(adapter_mod, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("MESHTASTIC_DEBUG_LOG_TEXT", "true")
    _rx(adapter_mod, monkeypatch, _text_packet(SECRET))
    assert SECRET in _captured(caplog)


# ----------------------------------------------------------------------
# _on_rx — channel messages (same behavior as DMs)
# ----------------------------------------------------------------------


def test_channel_body_is_absent_from_debug_logs(adapter_mod, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    a = _rx(
        adapter_mod,
        monkeypatch,
        _text_packet(MENTIONED, to_id="^all", channel=2),
        reply_channels="2",
    )
    assert len(a.handled) == 1, "the channel message should have been dispatched"
    assert SECRET not in _captured(caplog), "the channel body reached the gateway log"


def test_channel_debug_line_keeps_the_routing_context(adapter_mod, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    _rx(
        adapter_mod,
        monkeypatch,
        _text_packet(MENTIONED, to_id="^all", channel=2),
        reply_channels="2",
    )
    line = _inbound_line(caplog)
    assert "channel" in line
    assert "ch=2" in line
    assert "from=!11112222" in line
    assert "REPLY" in line
    # The mention is stripped before the body is logged, so the length is the
    # gated text's, not the raw packet's.
    assert f"text_len={len(SECRET)}" in line


def test_channel_body_is_logged_when_explicitly_enabled(adapter_mod, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("MESHTASTIC_DEBUG_LOG_TEXT", "true")
    _rx(
        adapter_mod,
        monkeypatch,
        _text_packet(MENTIONED, to_id="^all", channel=2),
        reply_channels="2",
    )
    assert SECRET in _captured(caplog)


# ----------------------------------------------------------------------
# skipped messages leak too — a body we never reply to is still a body
# ----------------------------------------------------------------------


def test_skipped_channel_body_is_absent_and_the_skip_reason_survives(
    adapter_mod,  # noqa: F811
    monkeypatch,
    caplog,
):
    """A message rejected by the channel allowlist must not be logged either."""
    caplog.set_level(logging.DEBUG)
    a = _rx(adapter_mod, monkeypatch, _text_packet(SECRET, to_id="^all", channel=0))
    assert a.handled == []  # DMs-only default policy skipped it
    assert SECRET not in _captured(caplog)
    assert "skip (policy)" in _inbound_line(caplog)


def test_ungated_channel_body_is_absent_and_the_skip_reason_survives(
    adapter_mod,  # noqa: F811
    monkeypatch,
    caplog,
):
    """An allowlisted channel message that isn't addressed to us: still no body."""
    caplog.set_level(logging.DEBUG)
    a = _rx(
        adapter_mod,
        monkeypatch,
        _text_packet(SECRET, to_id="^all", channel=2),  # no "MESH " prefix
        reply_channels="2",
    )
    assert a.handled == []
    assert SECRET not in _captured(caplog)
    assert "skip (not addressed to us)" in _inbound_line(caplog)


# ----------------------------------------------------------------------
# MESHTASTIC_DEBUG must not imply payload logging
# ----------------------------------------------------------------------


def test_debug_alone_does_not_enable_body_logging(adapter_mod, monkeypatch):  # noqa: F811
    """The verbose switch and the disclosure switch are independent."""
    monkeypatch.setenv("MESHTASTIC_DEBUG", "1")
    monkeypatch.delenv("MESHTASTIC_DEBUG_LOG_TEXT", raising=False)
    assert adapter_mod._log_text_from_env() is False
    assert SECRET not in adapter_mod.debug_text_for_log(SECRET)


def test_body_logging_is_off_without_any_env(adapter_mod, monkeypatch):  # noqa: F811
    monkeypatch.delenv("MESHTASTIC_DEBUG", raising=False)
    monkeypatch.delenv("MESHTASTIC_DEBUG_LOG_TEXT", raising=False)
    assert adapter_mod._log_text_from_env() is False


# ----------------------------------------------------------------------
# documentation
# ----------------------------------------------------------------------


def test_the_switch_is_documented():
    """An undocumented privacy switch is one nobody will find when they need it."""
    import pathlib

    repo = pathlib.Path(__file__).resolve().parent.parent
    manifest = (repo / "meshtastic_platform" / "plugin.yaml").read_text()
    usage = (repo / "docs" / "usage.md").read_text()
    assert "MESHTASTIC_DEBUG_LOG_TEXT" in manifest
    assert "MESHTASTIC_DEBUG_LOG_TEXT" in usage
