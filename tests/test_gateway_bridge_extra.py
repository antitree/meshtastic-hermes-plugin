"""gateway_bridge edge cases not covered by test_gateway_bridge.py."""

from __future__ import annotations

from meshtastic_hermes import gateway_bridge as gb


def test_normalize_node_forms():
    assert gb.normalize_node(None) == ""
    assert gb.normalize_node("!already") == "!already"
    assert gb.normalize_node(0xAABBCCDD) == "!aabbccdd"
    # An un-intifiable value falls back to str() rather than raising.
    assert gb.normalize_node([1, 2]) == "[1, 2]"


def test_inbound_decodes_a_raw_byte_payload():
    packet = {
        "fromId": "!11112222",
        "toId": "!aabbccdd",
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"hi there"},
    }
    assert gb.inbound_from_packet(packet, "!aabbccdd")["text"] == "hi there"


def test_inbound_ignores_an_empty_text_frame():
    packet = {
        "fromId": "!1",
        "toId": "!aabbccdd",
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": ""},
    }
    assert gb.inbound_from_packet(packet, "!aabbccdd") is None


def test_inbound_ignores_a_frame_with_no_usable_payload():
    packet = {
        "fromId": "!1",
        "toId": "!aabbccdd",
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": 12345},
    }
    assert gb.inbound_from_packet(packet, "!aabbccdd") is None


def test_inbound_without_a_known_local_node_id():
    """With no local id, nothing can be identified as addressed to us."""
    packet = {
        "fromId": "!1",
        "toId": "!aabbccdd",
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"},
    }
    inbound = gb.inbound_from_packet(packet, None)
    assert inbound["is_dm"] is False


def test_inbound_normalizes_numeric_addresses():
    packet = {
        "from": 0x11112222,
        "to": 0xAABBCCDD,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"},
    }
    inbound = gb.inbound_from_packet(packet, "!aabbccdd")
    assert inbound["from_id"] == "!11112222"
    assert inbound["is_dm"] is True


def test_outbound_target_for_a_malformed_channel_id_falls_back_to_primary():
    assert gb.outbound_target("ch:notanumber") == {
        "dest_id": None,
        "channel_index": 0,
        "pki": False,
    }


def test_parse_channel_spec_forms():
    assert gb.parse_channel_spec(None) is None
    assert gb.parse_channel_spec("") is None
    assert gb.parse_channel_spec("   ") is None
    assert gb.parse_channel_spec("all") == gb.ALL_CHANNELS
    assert gb.parse_channel_spec("ALL") == gb.ALL_CHANNELS
    assert gb.parse_channel_spec("1, 2") == {1, 2}
    assert gb.parse_channel_spec(3) == {3}
    # Unparseable entries are skipped, not fatal; an all-junk spec means "no channels".
    assert gb.parse_channel_spec("1,x,2") == {1, 2}
    assert gb.parse_channel_spec("x,y") is None
    assert gb.parse_channel_spec(",,") is None


def test_should_reply_policy_matrix():
    dm = {"is_dm": True, "channel": 0}
    ch0 = {"is_dm": False, "channel": 0}
    ch1 = {"is_dm": False, "channel": 1}

    assert gb.should_reply(dm) is True                                   # DMs always
    assert gb.should_reply(ch0) is False                                 # default: silent
    assert gb.should_reply(ch1, allowed_channels={1}) is True
    assert gb.should_reply(ch0, allowed_channels={1}) is False           # Primary stays quiet
    assert gb.should_reply(ch0, allowed_channels=gb.ALL_CHANNELS) is True


def test_process_inbound_returns_none_for_unreadable_traffic():
    assert gb.process_inbound({"encrypted": b"x"}, "!aabbccdd", lambda t, i: "r") is None


def test_process_inbound_skip_carries_the_inbound_message():
    packet = {
        "fromId": "!1",
        "toId": "^all",
        "channel": 0,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"},
    }
    result = gb.process_inbound(packet, "!aabbccdd", lambda t, i: "r")
    assert result["action"] == "skip"
    assert result["inbound"]["text"] == "hi"
    assert "reply" not in result  # the responder is never invoked on a skip


def test_process_inbound_passes_context_to_the_responder():
    packet = {
        "fromId": "!11112222",
        "toId": "!aabbccdd",
        "id": 42,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "ping"},
    }
    seen: list = []

    def responder(text, inbound):
        seen.append((text, inbound["from_id"], inbound["is_dm"]))
        return "pong"

    result = gb.process_inbound(packet, "!aabbccdd", responder)
    assert seen == [("ping", "!11112222", True)]
    assert result["reply"] == "pong"
    assert result["chat_id"] == "!11112222"
    assert result["target"] == {"dest_id": "!11112222", "channel_index": 0, "pki": True}
    assert result["inbound"]["message_id"] == "42"


def test_chat_id_for_channel_and_dm():
    assert gb.chat_id_for({"is_dm": True, "from_id": "!1", "channel": 0}) == "!1"
    assert gb.chat_id_for({"is_dm": False, "from_id": "!1", "channel": 4}) == "ch:4"
