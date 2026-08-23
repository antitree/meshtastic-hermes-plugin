"""Adapter-level channel-NAME allowlist behavior.

Reuses the stub-gateway fixture and fake ConnectionManager from
``tests/test_adapter_runtime`` (see that module's docstring for what the stub is
and what it deliberately does not model), and drives the real
``MeshtasticAdapter.connect()`` so the name -> index resolution seam is exercised
where it actually lives.

The safety claim under test: the allowlist is re-resolved from channel NAMES on
every connect, so a rename or reorder on the radio moves the allowlist with the
name instead of leaving the bot transmitting on a stale slot.
"""

from __future__ import annotations

import asyncio
import logging

# `tests/` is not a package (no __init__.py), so import the sibling module by its
# bare name — pytest puts the test directory on sys.path under rootdir insertion.
from test_adapter_runtime import (  # noqa: F401 - adapter_mod is imported as a fixture
    _FakeManager,
    _make,
    _patch_manager,
    _text_packet,
    adapter_mod,
)

PRIMARY = {"index": 0, "name": "", "role": 1}


def _ch(index, name, role=2):
    return {"index": index, "name": name, "role": role}


def _connect(a):
    assert asyncio.run(a.connect()) is True


def test_name_spec_resolves_at_connect(adapter_mod, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "in.secure")
    mgr = _FakeManager(channels=[PRIMARY, _ch(2, "in.secure")])
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)

    # Before connecting there is no channel table, so a name-only spec allows
    # nothing — fail closed rather than guessing an index.
    assert a.allowed_channels is None
    assert a.channel_spec.names == ("in.secure",)

    _connect(a)
    assert a.allowed_channels == {2}
    assert mgr.channel_table_calls == 1


def test_reconnect_reresolves_after_a_reorder(adapter_mod, monkeypatch):  # noqa: F811
    """Decision 3 + the core safety property, at the adapter seam.

    The operator swaps two channels on the radio; on the next connect the
    allowlist must follow the NAME to its new index, not keep the old one.
    """
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "in.secure")
    mgr = _FakeManager(channels=[PRIMARY, _ch(1, "public.chat"), _ch(2, "in.secure")])
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)

    _connect(a)
    assert a.allowed_channels == {2}

    # Operator reorders channels on the radio, then the link drops and comes back.
    mgr.channels = [PRIMARY, _ch(1, "in.secure"), _ch(2, "public.chat")]
    assert asyncio.run(a.connect(is_reconnect=True)) is True

    assert a.allowed_channels == {1}, "reconnect must re-resolve the name, not cache the index"
    assert 2 not in a.allowed_channels, "must not keep replying on the slot in.secure vacated"


def test_reconnect_picks_up_a_renamed_channel(adapter_mod, monkeypatch, caplog):  # noqa: F811
    """If the configured name disappears entirely, the bot goes quiet on channels
    (and warns) rather than transmitting onto whatever took the slot."""
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "in.secure")
    mgr = _FakeManager(channels=[PRIMARY, _ch(2, "in.secure")])
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)

    _connect(a)
    assert a.allowed_channels == {2}

    mgr.channels = [PRIMARY, _ch(2, "public.chat")]
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(a.connect(is_reconnect=True)) is True

    assert a.allowed_channels is None
    assert "in.secure" in caplog.text


def test_unknown_name_warns_but_adapter_still_connects(adapter_mod, monkeypatch, caplog):  # noqa: F811
    """Decision 1: warn and continue with what resolved; never fail the adapter."""
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "in.secure,typo.here")
    mgr = _FakeManager(channels=[PRIMARY, _ch(2, "in.secure")])
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)

    with caplog.at_level(logging.WARNING):
        _connect(a)

    assert a.state == "connected"
    assert a.fatal is None
    assert a.allowed_channels == {2}
    assert "typo.here" in caplog.text


def test_legacy_index_spec_still_works_and_warns(adapter_mod, monkeypatch, caplog):  # noqa: F811
    """Decision 2: numeric indices keep working so existing setups don't break,
    but the operator is told they are unsafe."""
    with caplog.at_level(logging.WARNING):
        monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "2")
        mgr = _FakeManager(channels=[PRIMARY, _ch(2, "in.secure")])
        _patch_manager(monkeypatch, mgr)
        a = _make(adapter_mod, monkeypatch)

    assert a.allowed_channels == {2}
    assert "slot" in caplog.text.lower()
    assert "MESHTASTIC_REPLY_CHANNELS" in caplog.text

    _connect(a)
    assert a.allowed_channels == {2}  # index specs need no channel table


def test_mixed_names_and_indices_at_the_adapter(adapter_mod, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "in.secure, 3")
    mgr = _FakeManager(channels=[PRIMARY, _ch(2, "in.secure"), _ch(3, "ops")])
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)

    _connect(a)
    assert a.allowed_channels == {2, 3}


def test_named_channel_drives_inbound_dispatch(adapter_mod, monkeypatch):  # noqa: F811
    """The resolved index really gates inbound traffic, and traffic on the slot the
    name does NOT occupy is ignored."""
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "in.secure")
    # This test is about NAME->index resolution gating inbound traffic, so take
    # mention gating out of the picture; it has its own tests in
    # tests/test_mention_gating_adapter.py.
    monkeypatch.setenv("MESHTASTIC_REQUIRE_MENTION", "false")
    mgr = _FakeManager(channels=[PRIMARY, _ch(1, "public.chat"), _ch(2, "in.secure")])
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)
    a._message_handler = object()

    async def flow():
        await a.connect()
        a._on_rx(_text_packet("private", to_id="^all", channel=2))
        a._on_rx(_text_packet("public", to_id="^all", channel=1))
        await asyncio.sleep(0.05)

    asyncio.run(flow())
    assert [e.text for e in a.handled] == ["private"]
    assert a.handled[0].source["chat_id"] == "ch:2"


def test_reply_all_is_unaffected_by_names(adapter_mod, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MESHTASTIC_REPLY_ALL", "true")
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "in.secure")
    mgr = _FakeManager(channels=[PRIMARY])
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)

    from meshtastic_hermes import gateway_bridge as gb

    assert a.allowed_channels == gb.ALL_CHANNELS
    _connect(a)
    assert a.allowed_channels == gb.ALL_CHANNELS


def test_connect_survives_a_channel_table_read_failure(adapter_mod, monkeypatch, caplog):  # noqa: F811
    """A radio that can't report its channels must not take the adapter down."""
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "in.secure")
    mgr = _FakeManager()

    def boom():
        raise RuntimeError("radio busy")

    mgr.channel_table = boom
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)

    with caplog.at_level(logging.WARNING):
        _connect(a)

    assert a.state == "connected"
    assert a.allowed_channels is None  # fail closed: no table, no named channels


def test_dms_only_when_nothing_is_configured(adapter_mod, monkeypatch):  # noqa: F811
    monkeypatch.delenv("MESHTASTIC_REPLY_ALL", raising=False)
    monkeypatch.delenv("MESHTASTIC_REPLY_CHANNELS", raising=False)
    mgr = _FakeManager(channels=[PRIMARY, _ch(2, "in.secure")])
    _patch_manager(monkeypatch, mgr)
    a = _make(adapter_mod, monkeypatch)

    _connect(a)
    assert a.channel_spec is None
    assert a.allowed_channels is None
