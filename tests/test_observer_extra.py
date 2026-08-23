"""Observer coverage: the privacy boundary and the never-raise guarantee.

The load-bearing property here is that encrypted traffic contributes *metadata
only* — it must never reach the recent-messages buffer, and its portnum must be
recorded as ENCRYPTED rather than whatever the frame claims.
"""

from __future__ import annotations

import time

from meshtastic_hermes import knowledge, observer


def _obs():
    return observer.Observer(knowledge.NodeGraph(":memory:"))


# ----------------------------------------------------------------------
# node id normalization
# ----------------------------------------------------------------------


def test_node_id_normalization():
    assert observer._node_id(0xAABBCCDD) == "!aabbccdd"
    assert observer._node_id(None) == ""
    assert observer._node_id("!already") == "!already"
    assert observer._node_id(object()) not in (None, "")  # falls back to str()


# ----------------------------------------------------------------------
# the never-raise guarantee
# ----------------------------------------------------------------------


def test_on_receive_swallows_malformed_packets():
    """This runs on the radio's receive thread; raising would kill the listener."""
    o = _obs()
    o.on_receive(None)  # not a dict at all
    o.on_receive({"decoded": "not-a-dict"})
    o.on_receive({})
    assert o.recent_messages() == []


def test_on_receive_accepts_the_interface_kwarg():
    o = _obs()
    o.on_receive({"fromId": "!a", "decoded": {"portnum": "POSITION_APP"}}, interface=object())
    assert o.kb.summary()["packets"] == 1


# ----------------------------------------------------------------------
# metadata recording
# ----------------------------------------------------------------------


def test_encrypted_packet_records_metadata_only():
    o = _obs()
    o.on_receive(
        {
            "from": 0x11112222,
            "to": 0x33334444,
            "channel": 2,
            "encrypted": b"\xde\xad\xbe\xef",
            "hopLimit": 3,
            "rxSnr": 5.5,
            "rxRssi": -90,
            "rxTime": 1700000000,
        }
    )
    rows = o.kb.interactions()
    assert len(rows) == 1
    assert rows[0]["encrypted"] == 1
    assert rows[0]["portnum"] == "ENCRYPTED"  # never the claimed portnum
    assert rows[0]["payload_size"] == 4
    assert rows[0]["from_node"] == "!11112222"
    assert o.recent_messages() == []  # content never surfaces


def test_decoded_frame_marked_encrypted_when_the_flag_is_set():
    """A packet carrying both `decoded` and `encrypted` is still opaque to us."""
    o = _obs()
    o.on_receive(
        {
            "fromId": "!a",
            "encrypted": b"xx",
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "should not surface"},
        }
    )
    assert o.kb.interactions()[0]["portnum"] == "ENCRYPTED"
    assert o.recent_messages() == []


def test_missing_destination_defaults_to_broadcast():
    o = _obs()
    o.on_receive({"fromId": "!a", "decoded": {"portnum": "POSITION_APP"}})
    assert o.kb.interactions()[0]["to_node"] == knowledge.BROADCAST_ID


def test_missing_rxtime_falls_back_to_now():
    o = _obs()
    before = time.time()
    o.on_receive({"fromId": "!a", "decoded": {"portnum": "POSITION_APP"}})
    assert o.kb.interactions()[0]["ts"] >= before


# ----------------------------------------------------------------------
# NODEINFO enrichment
# ----------------------------------------------------------------------


def test_nodeinfo_enriches_the_node_row():
    o = _obs()
    o.on_receive(
        {
            "fromId": "!11112222",
            "from": 0x11112222,
            "rxTime": 1700000000,
            "decoded": {
                "portnum": "NODEINFO_APP",
                "user": {
                    "shortName": "AB",
                    "longName": "Alpha Bravo",
                    "hwModel": "TBEAM",
                    "role": "CLIENT",
                },
            },
        }
    )
    node = o.kb.nodes()[0]
    assert node["short_name"] == "AB"
    assert node["long_name"] == "Alpha Bravo"
    assert node["hw_model"] == "TBEAM"
    assert node["num"] == 0x11112222


def test_nodeinfo_without_a_user_block():
    o = _obs()
    o.on_receive({"fromId": "!a", "decoded": {"portnum": "NODEINFO_APP"}})
    node = o.kb.nodes()[0]
    assert node["node_id"] == "!a"
    assert node["short_name"] is None


# ----------------------------------------------------------------------
# text surfacing
# ----------------------------------------------------------------------


def test_decoded_text_reaches_the_recent_buffer():
    o = _obs()
    o.on_receive(
        {
            "fromId": "!11112222",
            "toId": "^all",
            "channel": 1,
            "rxTime": 1700000000,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
        }
    )
    msg = o.recent_messages()[0]
    assert msg == {
        "ts": 1700000000.0,
        "from": "!11112222",
        "to": "^all",
        "channel": 1,
        "text": "hello",
    }


def test_text_decoded_from_a_raw_payload():
    """Some frames carry bytes rather than a pre-decoded `text` field."""
    o = _obs()
    o.on_receive(
        {"fromId": "!a", "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"raw bytes"}}
    )
    assert o.recent_messages()[0]["text"] == "raw bytes"


def test_invalid_utf8_payload_is_replaced_not_dropped():
    o = _obs()
    o.on_receive(
        {"fromId": "!a", "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"\xff\xfe"}}
    )
    assert o.recent_messages()[0]["text"] == "��"


def test_recent_messages_are_newest_first_and_limited():
    o = _obs()
    for i in range(5):
        o.on_receive(
            {
                "fromId": "!a",
                "rxTime": float(i),
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": f"m{i}"},
            }
        )
    assert [m["text"] for m in o.recent_messages(3)] == ["m4", "m3", "m2"]


def test_recent_buffer_is_bounded():
    o = _obs()
    for i in range(observer._RECENT_MAXLEN + 20):
        o.on_receive(
            {
                "fromId": "!a",
                "rxTime": float(i),
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": f"m{i}"},
            }
        )
    assert len(o.recent_messages(10_000)) == observer._RECENT_MAXLEN


# ----------------------------------------------------------------------
# the shared singleton
# ----------------------------------------------------------------------


def test_get_observer_is_a_singleton_shared_with_the_connection_manager():
    from meshtastic_hermes.connection import _shared_state

    o = observer.get_observer()
    assert observer.get_observer() is o
    assert _shared_state().observer is o


def test_observer_defaults_to_the_env_configured_kb():
    o = observer.Observer()
    assert o.kb.db_path == ":memory:"  # set by the session fixture
