"""Mention gating: the pure matcher and the inbound gate.

Why this exists: a channel allowlist on its own makes the bot reply to EVERY
message on that channel. Two bots configured that way answer each other without
end, and on LoRa — a shared, legally regulated medium — that is an unbounded
transmission loop, not merely noise. The only pre-existing loop guard is
``from_id == my_node_id``, which by construction cannot catch a *different* node.

So on a channel the bot now only answers when the message is ADDRESSED to it. The
NEGATIVE cases below are the point of the feature: a mention mid-sentence, and a
longer word that merely starts with the short name, must not make the radio
transmit.

Everything here is pure — no radio, no env, no Hermes. The adapter wiring is
tested in tests/test_mention_gating_adapter.py.
"""

from __future__ import annotations

import pytest

from meshtastic_hermes import gateway_bridge as gb

# The worked example from the feature spec.
SHORT = "MESH"
LONG = "MESHTASTIC Bot"
NODE = "!deadbeef"
ID = gb.Identity(node_id=NODE, short_name=SHORT, long_name=LONG)


def _m(text, **kw):
    kw.setdefault("short_name", SHORT)
    kw.setdefault("long_name", LONG)
    kw.setdefault("node_id", NODE)
    return gb.match_mention(text, **kw)


# ----------------------------------------------------------------------
# positive matches — every spelling the spec requires
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,remainder",
    [
        ("MESH can you give me the weather?", "can you give me the weather?"),
        ("mesh weather", "weather"),
        ("MeSh: Weather now", "Weather now"),
        ("MESHTASTIC BOT can you give me the weather", "can you give me the weather"),
        ("@mesh weather", "weather"),
        ("@MESHTASTIC Bot weather now", "weather now"),
        ("!deadbeef weather", "weather"),
        ("@!deadbeef weather", "weather"),
    ],
)
def test_spec_examples_all_match_and_strip(text, remainder):
    assert _m(text) == remainder


@pytest.mark.parametrize(
    "text,remainder",
    [
        ("MESH, weather", "weather"),            # comma separator
        ("MESH:weather", "weather"),             # colon, no space
        ("MESH   weather", "weather"),           # runs of whitespace
        ("  MESH weather", "weather"),           # leading whitespace on the packet
        ("@  MESH weather", "weather"),          # space after the @
        ("deadbeef weather", "weather"),         # node id without its '!'
        ("!DEADBEEF weather", "weather"),        # node id is case-insensitive too
        ("meshtastic bot: weather", "weather"),         # long name, lowered, with a colon
        ("MESH what is 2 + 2?", "what is 2 + 2?"),
    ],
)
def test_accepted_variations(text, remainder):
    assert _m(text) == remainder


# ----------------------------------------------------------------------
# NEGATIVE cases — the actual safety property
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "ask MESH about the weather",     # mention mid-sentence
        "I think MESHTASTIC Bot is offline",     # long name mid-sentence
        "tell !deadbeef to reboot",       # node id mid-sentence
        "hello everyone",                 # no mention at all
        "weather MESH",                   # mention at the END, not the start
        "",                               # empty text
        "   ",                            # whitespace only
        "@",                              # a bare @ addresses nobody
    ],
)
def test_no_match_means_no_transmission(text):
    assert _m(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "MESHNET weather",     # longer word starting with the short name
        "MESHs are cool",
        "MESHTASTIC Botting tonight",  # longer word starting with the long name
    ],
)
def test_a_longer_word_starting_with_an_identifier_is_not_a_mention(text):
    """The word-boundary rule. A naive ``startswith`` would match all of these."""
    assert _m(text) is None


def test_short_name_is_not_a_prefix_wildcard():
    """The classic case: short name 'MES' must not answer MESHNET or MESHY."""
    kw = {"short_name": "MES", "long_name": None, "node_id": None}
    assert gb.match_mention("MESHNET weather", **kw) is None
    assert gb.match_mention("MESHY is down", **kw) is None
    assert gb.match_mention("MES weather", **kw) == "weather"
    assert gb.match_mention("MES: weather", **kw) == "weather"


# ----------------------------------------------------------------------
# longest match, literal matching, metacharacters
# ----------------------------------------------------------------------


def test_longest_match_wins_when_the_long_name_starts_with_the_short_name():
    """'MESHTASTIC Bot weather' must strip the whole long name, not leave 'Box weather'."""
    kw = {"short_name": "MES", "long_name": "MESHTASTIC Bot", "node_id": "!deadbeef"}
    assert gb.match_mention("MESHTASTIC Bot weather", **kw) == "weather"
    assert gb.match_mention("MES weather", **kw) == "weather"


def test_long_name_is_matched_literally_not_token_by_token():
    kw = {"short_name": None, "long_name": "MESHTASTIC Bot", "node_id": None}
    assert gb.match_mention("MESHTASTIC Bot weather", **kw) == "weather"
    # Only the first token of the long name is not a mention.
    assert gb.match_mention("MES weather", **kw) is None
    # Neither is the long name with its internal spacing changed.
    assert gb.match_mention("MESHTASTICBot weather", **kw) is None


def test_names_with_regex_metacharacters_match_only_themselves():
    """A name is never interpolated into a pattern, so '.' is a literal dot."""
    kw = {"short_name": "R.B", "long_name": "R.B (bot)", "node_id": None}
    assert gb.match_mention("R.B hi", **kw) == "hi"
    assert gb.match_mention("R.B (bot) hi", **kw) == "hi"
    assert gb.match_mention("RxB hi", **kw) is None, "'.' must not act as a wildcard"

    plus = {"short_name": "a+b", "long_name": None, "node_id": None}
    assert gb.match_mention("a+b hi", **plus) == "hi"
    assert gb.match_mention("aaab hi", **plus) is None


def test_names_are_stripped_of_surrounding_whitespace():
    kw = {"short_name": "  MESH  ", "long_name": None, "node_id": None}
    assert gb.match_mention("MESH weather", **kw) == "weather"


# ----------------------------------------------------------------------
# degraded / absent identity
# ----------------------------------------------------------------------


def test_gating_still_works_on_node_id_alone():
    """Names arrive late from the node DB; the node id is available immediately."""
    kw = {"short_name": None, "long_name": None, "node_id": "!deadbeef"}
    assert gb.match_mention("!deadbeef weather", **kw) == "weather"
    assert gb.match_mention("deadbeef weather", **kw) == "weather"
    assert gb.match_mention("MESH weather", **kw) is None  # name is not known yet


def test_no_identifiers_at_all_matches_nothing():
    """Fail closed: with nothing to match, nothing is a mention."""
    assert gb.match_mention("MESH weather", short_name=None, long_name=None, node_id=None) is None
    assert gb.match_mention("anything", short_name="", long_name="", node_id="") is None


# ----------------------------------------------------------------------
# bare mention
# ----------------------------------------------------------------------


def test_a_bare_mention_returns_empty_string_not_none():
    """Documented behavior: "" means 'addressed us, said nothing'; None means
    'did not address us'. They are deliberately distinguishable."""
    assert _m("MESH") == ""
    assert _m("MESH:") == ""
    assert _m("@MESHTASTIC Bot  ") == ""
    assert _m("MESH") is not None


# ----------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------


def test_identity_from_status_reads_the_connection_manager_shape():
    ident = gb.Identity.from_status(
        {
            "node_id": "!deadbeef",
            "true_node_id": "!deadbeef",
            "node_num": 3735928559,
            "short_name": "MESH",
            "long_name": "MESHTASTIC Bot",
        }
    )
    assert (ident.node_id, ident.short_name, ident.long_name) == ("!deadbeef", "MESH", "MESHTASTIC Bot")
    assert ident
    assert not ident.is_degraded


def test_identity_truthiness_and_degradation():
    assert not gb.Identity()
    assert not gb.Identity.from_status(None)
    assert not gb.Identity.from_status({})

    only_id = gb.Identity(node_id="!deadbeef")
    assert only_id
    assert only_id.is_degraded, "node id alone still gates, but misses name mentions"

    full = gb.Identity(node_id="!deadbeef", short_name="MESH", long_name="MESHTASTIC Bot")
    assert not full.is_degraded


# ----------------------------------------------------------------------
# apply_mention_gate
# ----------------------------------------------------------------------


def _inbound(text, *, is_dm=False, channel=1):
    return {
        "text": text,
        "from_id": "!11112222",
        "to_id": "!deadbeef" if is_dm else "^all",
        "channel": channel,
        "is_dm": is_dm,
        "message_id": "7",
    }


def test_dms_are_always_answered_and_never_stripped():
    """User decision 1: a DM is already addressed to this node."""
    dm = _inbound("weather now", is_dm=True)
    assert gb.apply_mention_gate(dm, ID) is dm
    assert gb.apply_mention_gate(dm, ID)["text"] == "weather now"
    # ...even with no identity at all, and even if it happens to name us.
    assert gb.apply_mention_gate(dm, gb.Identity()) is dm
    named = _inbound("MESH weather", is_dm=True)
    assert gb.apply_mention_gate(named, ID)["text"] == "MESH weather"


def test_channel_message_with_a_mention_is_stripped_for_the_agent():
    """User decision 2: the agent sees 'weather now', not 'MESH weather now'."""
    gated = gb.apply_mention_gate(_inbound("MESH weather now"), ID)
    assert gated is not None
    assert gated["text"] == "weather now"
    assert gated["raw_text"] == "MESH weather now", "the original is preserved"
    assert gated["mentioned"] is True


def test_channel_message_without_a_mention_is_dropped():
    assert gb.apply_mention_gate(_inbound("what is the weather?"), ID) is None
    assert gb.apply_mention_gate(_inbound("ask MESH about the weather"), ID) is None


def test_the_gate_does_not_mutate_the_input():
    inbound = _inbound("MESH weather")
    gb.apply_mention_gate(inbound, ID)
    assert inbound["text"] == "MESH weather"
    assert "raw_text" not in inbound


def test_gating_off_passes_channel_traffic_through_untouched():
    inbound = _inbound("no mention here")
    assert gb.apply_mention_gate(inbound, ID, require_mention=False) is inbound


def test_fail_closed_when_identity_is_unknown():
    """Never fall through to 'reply to everything' because we don't know our name."""
    for identity in (None, gb.Identity(), gb.Identity.from_status({})):
        assert gb.apply_mention_gate(_inbound("MESH weather"), identity) is None
        assert gb.apply_mention_gate(_inbound("anything"), identity) is None
    # DMs are unaffected by the degraded state.
    assert gb.apply_mention_gate(_inbound("hi", is_dm=True), None) is not None


def test_gating_on_node_id_alone_still_answers_an_id_mention():
    only_id = gb.Identity(node_id="!deadbeef")
    assert gb.apply_mention_gate(_inbound("!deadbeef weather"), only_id)["text"] == "weather"
    assert gb.apply_mention_gate(_inbound("MESH weather"), only_id) is None


def test_a_bare_mention_is_forwarded_with_empty_text():
    """Documented: being called by name with no question still reaches the agent."""
    gated = gb.apply_mention_gate(_inbound("MESH"), ID)
    assert gated is not None
    assert gated["text"] == ""
    assert gated["raw_text"] == "MESH"


# ----------------------------------------------------------------------
# should_reply / process_inbound integration
# ----------------------------------------------------------------------


def test_should_reply_applies_mention_gating_on_channels_only():
    kw = {"allowed_channels": {1}, "identity": ID, "require_mention": True}
    assert gb.should_reply(_inbound("MESH weather"), **kw) is True
    assert gb.should_reply(_inbound("weather"), **kw) is False
    assert gb.should_reply(_inbound("ask MESH later"), **kw) is False
    # DMs bypass the gate entirely.
    assert gb.should_reply(_inbound("weather", is_dm=True), **kw) is True


def test_should_reply_channel_allowlist_still_applies_first():
    """A mention on a channel that is NOT allowlisted is still silence."""
    assert (
        gb.should_reply(
            _inbound("MESH weather", channel=9),
            allowed_channels={1},
            identity=ID,
            require_mention=True,
        )
        is False
    )


def test_should_reply_gating_off_restores_reply_to_all():
    kw = {"allowed_channels": {1}, "identity": ID, "require_mention": False}
    assert gb.should_reply(_inbound("no mention"), **kw) is True
    assert gb.should_reply(_inbound("hello"), **kw) is True


def test_should_reply_default_is_unchanged_for_existing_callers():
    """Backwards compatibility: the pure predicate's own default is gating OFF.
    The operator-facing default (ON) lives in the adapter's env handling."""
    assert gb.should_reply(_inbound("no mention"), allowed_channels={1}) is True


def _packet(text, *, channel=1, to_id="^all"):
    return {
        "fromId": "!11112222",
        "toId": to_id,
        "channel": channel,
        "id": 7,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text},
    }


def test_process_inbound_strips_the_mention_before_the_responder_sees_it():
    seen = []

    def responder(text, inbound):
        seen.append(text)
        return "ok"

    result = gb.process_inbound(
        _packet("MESH weather now"),
        "!deadbeef",
        responder,
        allowed_channels={1},
        identity=ID,
        require_mention=True,
    )
    assert result["action"] == "reply"
    assert seen == ["weather now"], "the agent must never see the mention"
    assert result["inbound"]["raw_text"] == "MESH weather now"
    assert result["chat_id"] == "ch:1"


def test_process_inbound_skips_an_unaddressed_channel_message():
    called = []
    result = gb.process_inbound(
        _packet("what is the weather?"),
        "!deadbeef",
        lambda t, i: called.append(t) or "ok",
        allowed_channels={1},
        identity=ID,
        require_mention=True,
    )
    assert result["action"] == "skip"
    assert called == [], "the responder (the agent/LLM) must not even be invoked"
    assert result["inbound"]["text"] == "what is the weather?"


def test_process_inbound_dm_needs_no_mention():
    result = gb.process_inbound(
        _packet("weather", to_id="!deadbeef"),
        "!deadbeef",
        lambda t, i: f"ack: {t}",
        allowed_channels={1},
        identity=ID,
        require_mention=True,
    )
    assert result["action"] == "reply"
    assert result["reply"] == "ack: weather"
    assert result["chat_id"] == "!11112222"


def test_process_inbound_two_bots_do_not_answer_each_other():
    """The loop this feature exists to prevent, end to end.

    Bot B (a DIFFERENT node, so the `from_id == my_node_id` guard does not fire)
    broadcasts a plain reply on an allowlisted channel. With gating on we stay
    silent; with gating off we transmit, and so would it.
    """
    other_bots_reply = _packet("ack: the weather is fine")

    gated = gb.process_inbound(
        other_bots_reply,
        "!deadbeef",
        lambda t, i: "ack: ...",
        allowed_channels=gb.ALL_CHANNELS,
        identity=ID,
        require_mention=True,
    )
    assert gated["action"] == "skip"

    ungated = gb.process_inbound(
        other_bots_reply,
        "!deadbeef",
        lambda t, i: "ack: ...",
        allowed_channels=gb.ALL_CHANNELS,
        identity=ID,
        require_mention=False,
    )
    assert ungated["action"] == "reply", "without gating, the loop is live"
