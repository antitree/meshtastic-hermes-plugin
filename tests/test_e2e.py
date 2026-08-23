"""End-to-end tests: a fake radio driving the real code over real pubsub topics.

No hardware and no Hermes checkout. A `_FakeRadio` stands in for TCPInterface and
publishes packets on the genuine `meshtastic.receive` topic, so every layer in
between is the production code: the ConnectionManager's subscriptions, the
Observer, the knowledge base, the gateway_bridge routing policy, and the tool
handlers. Only the two ends are fake — the radio, and (in the adapter test) the
Hermes gateway base class.

What these do NOT cover: the real Hermes gateway's behavior, and real radio
timing/packet framing. See ROADMAP.md.
"""

from __future__ import annotations

import json
import types

import pytest

from meshtastic_hermes import connection, tools
from meshtastic_hermes import gateway_bridge as gb

MY_NODE = 0xAABBCCDD
MY_ID = "!aabbccdd"
PEER_ID = "!11112222"


class _FakeRadio:
    """A TCPInterface stand-in that publishes on the real pubsub topics."""

    def __init__(self, host, portNumber=4403):
        self.host = host
        self.port = portNumber
        self.closed = False
        self.myInfo = types.SimpleNamespace(my_node_num=MY_NODE)
        self.nodes: dict = {}
        self.localNode = types.SimpleNamespace(channels=[])
        self.sent: list = []

    # -- outbound -----------------------------------------------------
    def sendData(self, payload, **kw):
        self.sent.append({"payload": payload, **kw})

    def close(self):
        self.closed = True

    # -- inbound ------------------------------------------------------
    def deliver_text(self, text, *, from_id=PEER_ID, to_id=MY_ID, channel=0, pid=1):
        from pubsub import pub

        pub.sendMessage(
            "meshtastic.receive",
            packet={
                "fromId": from_id,
                "from": int(from_id[1:], 16),
                "toId": to_id,
                "to": MY_NODE if to_id == MY_ID else 0xFFFFFFFF,
                "channel": channel,
                "id": pid,
                "rxSnr": 6.0,
                "rxRssi": -80,
                "hopLimit": 3,
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text},
            },
            interface=self,
        )

    def deliver_encrypted(self, *, from_id=PEER_ID, channel=3, pid=2):
        from pubsub import pub

        pub.sendMessage(
            "meshtastic.receive",
            packet={
                "fromId": from_id,
                "from": int(from_id[1:], 16),
                "toId": "^all",
                "channel": channel,
                "id": pid,
                "encrypted": b"\xde\xad\xbe\xef\x00\x11",
            },
            interface=self,
        )

    def drop(self):
        from pubsub import pub

        pub.sendMessage("meshtastic.connection.lost", interface=self)


@pytest.fixture
def radio(monkeypatch):
    """Connect the real ConnectionManager to a fake radio."""
    built: list[_FakeRadio] = []

    def factory(host, portNumber=4403):
        r = _FakeRadio(host, portNumber)
        built.append(r)
        return r

    monkeypatch.setattr(
        connection,
        "_import_meshtastic",
        lambda: types.SimpleNamespace(tcp_interface=types.SimpleNamespace(TCPInterface=factory)),
    )
    mgr = connection.get_manager()
    mgr.connect("radio.test")
    yield built[-1]
    mgr.disconnect()


# ----------------------------------------------------------------------
# observation pipeline
# ----------------------------------------------------------------------


def test_e2e_received_text_reaches_the_tools_and_the_kb(radio):
    radio.deliver_text("hello from the mesh")

    recent = json.loads(tools.recent_messages({}))["messages"]
    assert recent[0]["text"] == "hello from the mesh"
    assert recent[0]["from"] == PEER_ID

    summary = json.loads(tools.kb_summary({}))
    assert summary["packets"] == 1
    assert summary["encrypted_packets"] == 0
    assert summary["nodes"] == 1


def test_e2e_encrypted_traffic_is_counted_but_never_surfaced(radio):
    radio.deliver_encrypted()
    radio.deliver_text("plain")

    summary = json.loads(tools.kb_summary({}))
    assert summary["packets"] == 2
    assert summary["encrypted_packets"] == 1
    assert summary["decoded_packets"] == 1

    # Only the plaintext message is readable; the encrypted one contributed metadata.
    recent = json.loads(tools.recent_messages({}))["messages"]
    assert [m["text"] for m in recent] == ["plain"]

    rows = json.loads(tools.kb_interactions({}))["interactions"]
    enc = next(r for r in rows if r["encrypted"] == 1)
    assert enc["portnum"] == "ENCRYPTED"
    assert enc["payload_size"] == 6
    assert enc["channel"] == 3


def test_e2e_traffic_builds_a_node_graph(radio):
    radio.deliver_text("a", from_id="!11112222", to_id=MY_ID)
    radio.deliver_text("b", from_id="!33334444", to_id="^all", channel=1)
    radio.deliver_text("c", from_id="!11112222", to_id=MY_ID)

    nodes = json.loads(tools.kb_nodes({"sort": "packets"}))["nodes"]
    assert nodes[0]["node_id"] == "!11112222"
    assert nodes[0]["packets"] == 2
    assert nodes[0]["last_snr"] == 6.0

    neighbors = json.loads(tools.kb_neighbors({"node_id": "!11112222"}))["neighbors"]
    assert neighbors[0]["peer"] == MY_ID
    assert neighbors[0]["count"] == 2


def test_e2e_observation_survives_a_reconnect(radio, monkeypatch):
    """A drop must not lose the observer's subscription or the accumulated KB."""
    radio.deliver_text("before the drop")
    mgr = connection.get_manager()

    mgr._on_connection_lost(interface=radio)
    assert mgr.is_connected() is False

    # The supervisor reopens; wait for the new interface rather than sleeping blind.
    import time

    deadline = time.time() + 5
    while time.time() < deadline and not mgr.is_connected():
        time.sleep(0.05)
    assert mgr.is_connected(), "supervisor did not reconnect"

    mgr._iface.deliver_text("after the drop")
    texts = [m["text"] for m in json.loads(tools.recent_messages({}))["messages"]]
    assert "after the drop" in texts
    assert "before the drop" in texts
    # Exactly one delivery per packet — no duplicate subscription from the reconnect.
    assert json.loads(tools.kb_summary({}))["packets"] == 2


# ----------------------------------------------------------------------
# send path
# ----------------------------------------------------------------------


def test_e2e_send_reaches_the_radio(radio):
    result = json.loads(
        tools.send_text({"text": "outbound", "channel_index": 2, "want_ack": False})
    )
    assert result["sent"] is True
    assert radio.sent[0]["payload"] == b"outbound"
    assert radio.sent[0]["channelIndex"] == 2
    assert radio.sent[0]["pkiEncrypted"] is False


def test_e2e_dm_is_pki_encrypted(radio):
    json.loads(
        tools.send_text({"text": "secret", "dest_id": PEER_ID, "pki": True, "wait_ack": False})
    )
    assert radio.sent[0]["destinationId"] == PEER_ID
    assert radio.sent[0]["pkiEncrypted"] is True


# ----------------------------------------------------------------------
# full bridge loop: inbound packet -> routing policy -> outbound reply
# ----------------------------------------------------------------------


def test_e2e_bridge_replies_to_a_dm(radio):
    """The simulator loop from __main__, driven over the real pubsub topic."""
    from pubsub import pub

    replies: list = []

    def on_rx(packet, interface=None):
        result = gb.process_inbound(packet, MY_ID, lambda t, i: f"ack: {t}")
        if result and result["action"] == "reply":
            replies.append(result)
            tgt = result["target"]
            tools.send_text(
                {
                    "text": result["reply"],
                    "dest_id": tgt["dest_id"],
                    "channel_index": tgt["channel_index"],
                    "pki": tgt["pki"],
                    "wait_ack": False,
                }
            )

    pub.subscribe(on_rx, "meshtastic.receive")
    try:
        radio.deliver_text("are you there?")
    finally:
        pub.unsubscribe(on_rx, "meshtastic.receive")

    assert len(replies) == 1
    assert replies[0]["chat_id"] == PEER_ID
    assert radio.sent[0]["payload"] == b"ack: are you there?"
    assert radio.sent[0]["pkiEncrypted"] is True  # DM replies are end-to-end


def test_e2e_bridge_stays_silent_on_a_public_channel(radio):
    """The default policy must not answer broadcast traffic — that is the loop risk."""
    from pubsub import pub

    seen: list = []

    def on_rx(packet, interface=None):
        result = gb.process_inbound(packet, MY_ID, lambda t, i: "reply")
        if result:
            seen.append(result["action"])
            if result["action"] == "reply":
                tools.send_text({"text": result["reply"], "wait_ack": False})

    pub.subscribe(on_rx, "meshtastic.receive")
    try:
        radio.deliver_text("hi everyone", to_id="^all", channel=0)
    finally:
        pub.unsubscribe(on_rx, "meshtastic.receive")

    assert seen == ["skip"]
    assert radio.sent == []  # nothing transmitted


def test_e2e_bridge_replies_on_an_allowlisted_channel(radio):
    from pubsub import pub

    replies: list = []

    def on_rx(packet, interface=None):
        result = gb.process_inbound(
            packet, MY_ID, lambda t, i: "roger", allowed_channels={1}
        )
        if result and result["action"] == "reply":
            replies.append(result)

    pub.subscribe(on_rx, "meshtastic.receive")
    try:
        radio.deliver_text("on ch1", to_id="^all", channel=1)
        radio.deliver_text("on ch0", to_id="^all", channel=0)
    finally:
        pub.unsubscribe(on_rx, "meshtastic.receive")

    assert len(replies) == 1
    assert replies[0]["chat_id"] == "ch:1"
    assert replies[0]["target"]["channel_index"] == 1
    assert replies[0]["target"]["pki"] is False


def test_e2e_bridge_ignores_our_own_echo(radio):
    """Loop guard: our own transmission coming back must not trigger a reply."""
    from pubsub import pub

    results: list = []

    def on_rx(packet, interface=None):
        results.append(gb.process_inbound(packet, MY_ID, lambda t, i: "reply"))

    pub.subscribe(on_rx, "meshtastic.receive")
    try:
        radio.deliver_text("my own words", from_id=MY_ID, to_id=PEER_ID)
    finally:
        pub.unsubscribe(on_rx, "meshtastic.receive")

    assert results == [None]


def test_e2e_bridge_never_replies_to_opaque_traffic(radio):
    from pubsub import pub

    results: list = []

    def on_rx(packet, interface=None):
        results.append(
            gb.process_inbound(packet, MY_ID, lambda t, i: "reply", allowed_channels=gb.ALL_CHANNELS)
        )

    pub.subscribe(on_rx, "meshtastic.receive")
    try:
        radio.deliver_encrypted()
    finally:
        pub.unsubscribe(on_rx, "meshtastic.receive")

    # Even with every channel allowed, an undecryptable frame yields nothing.
    assert results == [None]


# ----------------------------------------------------------------------
# radio inspection tools against the fake node DB
# ----------------------------------------------------------------------


def test_e2e_list_nodes_reads_the_radio_node_db(radio):
    radio.nodes = {
        PEER_ID: {
            "user": {"shortName": "PR", "longName": "Peer", "hwModel": "TBEAM"},
            "deviceMetrics": {"batteryLevel": 74},
            "snr": 5.0,
        }
    }
    data = json.loads(tools.list_nodes({}))
    assert data["count"] == 1
    assert data["nodes"][0]["short_name"] == "PR"
    assert data["nodes"][0]["battery"] == 74


def test_e2e_status_reports_the_live_link(radio):
    assert connection.get_manager().status() == {
        "connected": True,
        "state": "connected",
        "host": "radio.test",
        "consecutive_failures": 0,
        "slow_retry": False,
        "node_id": MY_ID,
        "true_node_id": MY_ID,
        "node_num": int(MY_ID[1:], 16),
        "short_name": None,
        "long_name": None,
    }
