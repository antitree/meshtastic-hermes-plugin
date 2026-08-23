"""Channel-NAME allowlist resolution — the safety-critical configuration surface.

Channel indices are radio SLOTS, not identities. Reordering or editing channels on
the radio repoints an index at a *different* channel, which would make the bot
transmit replies onto the wrong — possibly public — channel. These tests pin the
name-based behavior that prevents that.

No radio is involved: resolve_channel_spec() takes the channel table as plain data,
so the whole policy is testable without hardware.
"""

from __future__ import annotations

from meshtastic_hermes import gateway_bridge as gb


def _table(*rows):
    """Build a channel table: (index, name) or (index, name, role) tuples."""
    return [{"index": r[0], "name": r[1], "role": r[2] if len(r) > 2 else 2} for r in rows]


PRIMARY_ONLY = (0, "", 1)


# ── parsing (pure, no radio) ──────────────────────────────────────────────


def test_name_with_dot_is_not_split():
    """'in.secure' is ONE name — splitting is on commas only."""
    spec = gb.parse_channel_spec("in.secure")
    assert spec.names == ("in.secure",)
    assert spec.indices == frozenset()


def test_name_with_internal_spaces_preserved():
    spec = gb.parse_channel_spec("  my channel , other  ")
    assert spec.names == ("my channel", "other")


def test_name_case_is_preserved_by_the_parser():
    """The old parser lowercased the whole spec, which corrupted names."""
    assert gb.parse_channel_spec("In.Secure").names == ("In.Secure",)


def test_all_keyword_stays_case_insensitive():
    assert gb.parse_channel_spec("all") == gb.ALL_CHANNELS
    assert gb.parse_channel_spec("ALL") == gb.ALL_CHANNELS
    assert gb.parse_channel_spec(" AlL ") == gb.ALL_CHANNELS


# ── resolution ────────────────────────────────────────────────────────────


def test_name_resolves_to_its_index():
    spec = gb.parse_channel_spec("in.secure")
    allowed, resolved = gb.resolve_channel_spec(spec, _table(PRIMARY_ONLY, (2, "in.secure")))
    assert allowed == {2}
    assert resolved == {"in.secure": 2}


def test_names_are_case_sensitive():
    """Meshtastic channel names are case-sensitive; a wrong-case name must NOT match."""
    table = _table(PRIMARY_ONLY, (2, "in.secure"))
    allowed, resolved = gb.resolve_channel_spec(gb.parse_channel_spec("IN.SECURE"), table)
    assert allowed is None
    assert resolved == {}


def test_unknown_name_warns_and_keeps_the_rest(caplog):
    """Decision 1: warn and continue with what resolved — never fail, never silently drop."""
    spec = gb.parse_channel_spec("in.secure,nope,alsonope")
    table = _table(PRIMARY_ONLY, (2, "in.secure"))
    with caplog.at_level("WARNING"):
        allowed, resolved = gb.resolve_channel_spec(spec, table)
    assert allowed == {2}
    assert resolved == {"in.secure": 2}
    warned = " ".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "nope" in warned and "alsonope" in warned


def test_mixed_names_and_indices():
    spec = gb.parse_channel_spec("in.secure, 3")
    allowed, resolved = gb.resolve_channel_spec(spec, _table(PRIMARY_ONLY, (2, "in.secure")))
    assert allowed == {2, 3}
    assert resolved == {"in.secure": 2}


def test_reorder_moves_allowlist_with_the_name():
    """THE safety property: the operator reorders channels on the radio and the
    allowlist follows the NAME to its new index instead of pointing at whatever
    now occupies the old slot.

    This is the test that fails on the pre-feature code, where an index-based
    allowlist would keep replying on slot 2 after in.secure moved to slot 1.
    """
    spec = gb.parse_channel_spec("in.secure")

    before = _table(PRIMARY_ONLY, (1, "public.chat"), (2, "in.secure"))
    allowed_before, _ = gb.resolve_channel_spec(spec, before)
    assert allowed_before == {2}

    # Operator swaps the two secondary channels on the radio.
    after = _table(PRIMARY_ONLY, (1, "in.secure"), (2, "public.chat"))
    allowed_after, resolved_after = gb.resolve_channel_spec(spec, after)

    assert allowed_after == {1}, "allowlist must follow the channel NAME, not the old index"
    assert resolved_after == {"in.secure": 1}
    assert 2 not in allowed_after, "must not keep replying on the slot in.secure vacated"


def test_empty_primary_name_never_matches_a_typo():
    """An unnamed Primary must not swallow a mistyped name — that would broadcast
    a private reply on the PUBLIC channel."""
    table = _table(PRIMARY_ONLY, (2, "in.secure"))
    allowed, _ = gb.resolve_channel_spec(gb.parse_channel_spec("in.secur"), table)
    assert allowed is None
    # An explicitly empty entry is dropped by the parser, not matched to Primary.
    assert gb.parse_channel_spec(" , ") is None


def test_primary_targetable_by_alias():
    table = _table(PRIMARY_ONLY, (2, "in.secure"))
    for alias in ("Primary", "primary", "LongFast"):
        allowed, resolved = gb.resolve_channel_spec(gb.parse_channel_spec(alias), table)
        assert allowed == {0}, alias
        assert resolved == {alias: 0}


def test_named_primary_matches_by_its_real_name():
    table = _table((0, "base.camp", 1), (2, "in.secure"))
    allowed, _ = gb.resolve_channel_spec(gb.parse_channel_spec("base.camp"), table)
    assert allowed == {0}
    # ...and the alias no longer applies, because the primary HAS a name.
    assert gb.resolve_channel_spec(gb.parse_channel_spec("Primary"), table)[0] is None


def test_resolve_passes_through_sentinels():
    assert gb.resolve_channel_spec(None, _table(PRIMARY_ONLY)) == (None, {})
    assert gb.resolve_channel_spec(gb.ALL_CHANNELS, _table(PRIMARY_ONLY)) == (
        gb.ALL_CHANNELS,
        {},
    )


def test_indices_resolve_without_a_channel_table():
    """Decision 2: legacy index specs keep working, table or no table."""
    allowed, resolved = gb.resolve_channel_spec(gb.parse_channel_spec("2"), None)
    assert allowed == {2}
    assert resolved == {}


def test_names_fail_closed_without_a_channel_table(caplog):
    """No table = no way to know which index a name is. Never guess an index."""
    with caplog.at_level("WARNING"):
        allowed, resolved = gb.resolve_channel_spec(gb.parse_channel_spec("in.secure"), None)
    assert allowed is None
    assert resolved == {}


def test_resolved_indices_drive_should_reply():
    spec = gb.parse_channel_spec("in.secure")
    allowed, _ = gb.resolve_channel_spec(spec, _table(PRIMARY_ONLY, (2, "in.secure")))
    assert gb.should_reply({"is_dm": False, "channel": 2}, allowed_channels=allowed) is True
    assert gb.should_reply({"is_dm": False, "channel": 0}, allowed_channels=allowed) is False
