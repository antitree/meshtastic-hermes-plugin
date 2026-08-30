"""Tool-handler coverage beyond the send paths already covered in test_tools.py.

The contract these all check: a handler ALWAYS returns a JSON string and NEVER
raises, so the Hermes tool loop keeps running when the radio is absent or angry.
"""

from __future__ import annotations

import json
import types

import pytest

from meshtastic_hermes import connection, tools


class _FakeIface:
    def __init__(self, nodes=None, channels=None, node_num=0xAABBCCDD):
        self.nodes = nodes if nodes is not None else {}
        self.localNode = types.SimpleNamespace(channels=channels or [])
        self.myInfo = types.SimpleNamespace(my_node_num=node_num)
        self.sent: list = []

    def sendData(self, payload, **kw):
        self.sent.append((payload, kw))


@pytest.fixture
def iface():
    """Inject a fake interface into the process-wide manager."""
    mgr = connection.get_manager()
    fake = _FakeIface(
        # index 0 = unnamed PRIMARY (public), index 1 = "in.secure". The tool-send
        # policy resolves channel names against this table.
        channels=[
            types.SimpleNamespace(role=1, settings=types.SimpleNamespace(name="")),
            types.SimpleNamespace(role=2, settings=types.SimpleNamespace(name="in.secure")),
        ]
    )
    mgr._iface = fake
    return fake


@pytest.fixture
def tool_send_allowed(monkeypatch):
    """Grant the tool permission to broadcast on "in.secure" (remediation item 1).

    Tool broadcasts are refused by default; tests below that are about SEND
    MECHANICS rather than policy opt in explicitly.
    """
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "in.secure")


def _data(raw):
    return json.loads(raw)


# ----------------------------------------------------------------------
# the _guard contract
# ----------------------------------------------------------------------


def test_handlers_return_json_error_when_disconnected():
    """Every radio-dependent tool must degrade to {"error": ...}, not raise."""
    for handler in (
        tools.send_text,
        tools.list_nodes,
        tools.node_info,
        tools.list_channels,
        tools.device_metrics,
    ):
        # send_text gets a payload that PASSES tool-send policy (a PKI DM), so it
        # reaches the connection check — the point of this test — rather than being
        # turned back earlier by the policy gate.
        payload = (
            {"text": "hi", "dest_id": "!11112222", "pki": True}
            if handler is tools.send_text
            else {}
        )
        data = _data(handler(payload))
        assert "error" in data, handler.__name__
        assert "Not connected" in data["error"]


def test_guard_maps_missing_radio_to_a_typed_error(monkeypatch):
    def boom(_args):
        raise connection.MeshtasticUnavailable("install meshtastic")

    data = _data(tools._guard(boom)({}))
    assert data["code"] == "radio_unavailable"
    assert data["error"] == "install meshtastic"


def test_guard_catches_unexpected_exceptions():
    def boom(_args):
        raise ValueError("something odd")

    data = _data(tools._guard(boom)({}))
    assert data["code"] == "internal"
    assert "something odd" in data["error"]


def test_guard_tolerates_none_args():
    called = {}

    def handler(args):
        called["args"] = args
        return "{}"

    tools._guard(handler)(None)
    assert called["args"] == {}


# ----------------------------------------------------------------------
# connect / disconnect
# ----------------------------------------------------------------------


def test_connect_without_host_or_env(monkeypatch):
    monkeypatch.delenv("MESHTASTIC_HOST", raising=False)
    data = _data(tools.connect({}))
    assert "MESHTASTIC_HOST is not set" in data["error"]


def test_connect_uses_the_env_host(monkeypatch):
    monkeypatch.setenv("MESHTASTIC_HOST", "env.example")
    calls = []
    mgr = connection.get_manager()
    monkeypatch.setattr(
        mgr, "connect", lambda h, p: calls.append((h, p)) or {"connected": True, "host": h}
    )
    data = _data(tools.connect({}))
    assert calls == [("env.example", 4403)]
    assert data["status"] == "connected"


def test_connect_honors_an_explicit_port(monkeypatch):
    # An explicit host is only accepted when the operator opted in; see
    # test_connect_policy.py for the restriction itself.
    monkeypatch.delenv("MESHTASTIC_HOST", raising=False)
    monkeypatch.setenv("MESHTASTIC_ALLOW_DYNAMIC_HOSTS", "true")
    calls = []
    mgr = connection.get_manager()
    monkeypatch.setattr(mgr, "connect", lambda h, p: calls.append((h, p)) or {"connected": True})
    tools.connect({"host": "1.2.3.4", "port": 9999})
    assert calls == [("1.2.3.4", 9999)]


def test_disconnect_returns_json():
    assert _data(tools.disconnect({})) == {
        "connected": False,
        "state": "disconnected",
    }


# ----------------------------------------------------------------------
# send_text
# ----------------------------------------------------------------------


def test_send_text_rejects_empty_text():
    assert "No text provided" in _data(tools.send_text({"text": "   "}))["error"]


def test_send_text_rejects_pki_broadcast():
    """PKI is point-to-point; a broadcast can't be end-to-end encrypted."""
    data = _data(tools.send_text({"text": "hi", "pki": True}))
    assert "pki=true requires dest_id" in data["error"]


def test_send_text_broadcast_does_not_wait_for_an_ack(iface, tool_send_allowed):
    data = _data(tools.send_text({"text": "hello", "channel_name": "in.secure"}))
    assert data["sent"] is True
    assert data["ack"] is None  # a broadcast has no single recipient to ack
    assert data["encryption"] == "channel"
    payload, kw = iface.sent[0]
    assert payload == b"hello"
    assert kw["channelIndex"] == 1
    assert "destinationId" not in kw


def test_send_text_ack_timeout_reports_no_ack(iface):
    data = _data(
        tools.send_text(
            {"text": "hi", "dest_id": "!11112222", "pki": True, "ack_timeout": 0.01}
        )
    )
    assert data["ack"]["status"] == "no_ack"
    assert data["ack"]["reason"] == "TIMEOUT"


def test_send_text_captures_a_delivery_ack(iface):
    def send_and_ack(payload, **kw):
        iface.sent.append((payload, kw))
        kw["onResponse"](
            {"fromId": "!11112222", "decoded": {"routing": {"errorReason": "NONE"}}}
        )

    iface.sendData = send_and_ack
    data = _data(tools.send_text({"text": "hi", "dest_id": "!11112222", "pki": True}))
    assert data["ack"] == {"status": "delivered", "reason": "NONE", "from": "!11112222"}


def test_send_text_reports_a_nak(iface):
    def send_and_nak(payload, **kw):
        iface.sent.append((payload, kw))
        kw["onResponse"](
            {"fromId": "!11112222", "decoded": {"routing": {"errorReason": "NO_RESPONSE"}}}
        )

    iface.sendData = send_and_nak
    data = _data(tools.send_text({"text": "hi", "dest_id": "!11112222", "pki": True}))
    assert data["ack"]["status"] == "failed"
    assert data["ack"]["reason"] == "NO_RESPONSE"


def test_send_text_pki_dm_sets_the_encryption_flag(iface):
    data = _data(
        tools.send_text(
            {"text": "secret", "dest_id": "!11112222", "pki": True, "wait_ack": False}
        )
    )
    assert data["encryption"] == "pki"
    _, kw = iface.sent[0]
    assert kw["pkiEncrypted"] is True
    assert kw["destinationId"] == "!11112222"


def test_send_text_want_ack_false_skips_the_ack_path(iface):
    data = _data(
        tools.send_text(
            {"text": "hi", "dest_id": "!11112222", "pki": True, "want_ack": False}
        )
    )
    assert data["ack"] is None
    _, kw = iface.sent[0]
    assert kw["wantAck"] is False
    assert "onResponse" not in kw


# ----------------------------------------------------------------------
# network inspection
# ----------------------------------------------------------------------


_NODE = {
    "user": {
        "shortName": "AB",
        "longName": "Alpha Bravo",
        "hwModel": "TBEAM",
        "role": "CLIENT",
    },
    "position": {"latitude": 1.5, "longitude": -2.5, "altitude": 30},
    "deviceMetrics": {
        "batteryLevel": 88,
        "voltage": 4.0,
        "channelUtilization": 3.5,
        "airUtilTx": 0.4,
        "uptimeSeconds": 1234,
    },
    "snr": 6.25,
    "lastHeard": 1700000000,
    "hopsAway": 2,
}


def test_list_nodes_summarizes_and_limits(iface, monkeypatch):
    # `lat` moved behind MESHTASTIC_EXPOSE_LOCATION in remediation item 4. This test
    # is about SUMMARIZING and LIMITING, so it opts in and keeps asserting the field;
    # the default-redacted behavior is asserted in test_privacy_gates.py.
    monkeypatch.setenv("MESHTASTIC_EXPOSE_LOCATION", "true")
    iface.nodes = {f"!{i:08x}": _NODE for i in range(5)}
    data = _data(tools.list_nodes({"limit": 3}))
    assert data["count"] == 3
    first = data["nodes"][0]
    assert first["short_name"] == "AB"
    assert first["battery"] == 88
    assert first["lat"] == 1.5
    assert first["hops_away"] == 2


def test_list_nodes_with_an_empty_node_db(iface, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_EXPOSE_LOCATION", "true")
    iface.nodes = None
    # With location exposed there is no redaction annotation, so the payload is
    # exactly the empty result it always was.
    assert _data(tools.list_nodes({})) == {"count": 0, "nodes": []}


def test_node_info_defaults_to_our_own_node(iface):
    iface.nodes = {"!aabbccdd": _NODE}
    data = _data(tools.node_info({}))
    assert data["id"] == "!aabbccdd"
    assert data["long_name"] == "Alpha Bravo"


def test_node_info_for_an_unknown_node(iface):
    iface.nodes = {}
    data = _data(tools.node_info({"node_id": "!deadbeef"}))
    assert "not found" in data["error"]
    assert data["node_id"] == "!deadbeef"


def test_node_info_when_our_node_id_is_unknown(iface):
    iface.myInfo = None
    iface.nodes = {}
    assert "Could not determine node id" in _data(tools.node_info({}))["error"]


def test_node_info_tolerates_a_sparse_node_record(iface):
    iface.nodes = {"!aabbccdd": {}}
    data = _data(tools.node_info({}))
    assert data["short_name"] is None
    assert data["battery"] is None


def test_list_channels_skips_disabled_and_names_the_primary(iface):
    iface.localNode = types.SimpleNamespace(
        channels=[
            types.SimpleNamespace(role=1, settings=types.SimpleNamespace(name="", psk=b"\x01")),
            types.SimpleNamespace(role=2, settings=types.SimpleNamespace(name="ops", psk=b"k")),
            types.SimpleNamespace(role=0, settings=types.SimpleNamespace(name="off", psk=b"")),
        ]
    )
    data = _data(tools.list_channels({}))
    assert data["count"] == 2  # the DISABLED channel is omitted
    assert data["channels"][0] == {
        "index": 0,
        "name": "Primary",
        "role": "PRIMARY",
        "has_psk": True,
    }
    assert data["channels"][1]["name"] == "ops"
    assert data["channels"][1]["role"] == "SECONDARY"


def test_list_channels_names_an_unnamed_secondary_by_index(iface):
    iface.localNode = types.SimpleNamespace(
        channels=[
            types.SimpleNamespace(role=1, settings=types.SimpleNamespace(name="P", psk=b"")),
            types.SimpleNamespace(role=2, settings=types.SimpleNamespace(name="", psk=b"")),
        ]
    )
    data = _data(tools.list_channels({}))
    assert data["channels"][1]["name"] == "ch1"
    assert data["channels"][1]["has_psk"] is False


def test_list_channels_with_an_unexpected_role_value(iface):
    iface.localNode = types.SimpleNamespace(
        channels=[types.SimpleNamespace(role=9, settings=types.SimpleNamespace(name="x", psk=b""))]
    )
    assert _data(tools.list_channels({}))["channels"][0]["role"] == "9"


def test_list_channels_with_no_channels(iface):
    iface.localNode = types.SimpleNamespace(channels=None)
    assert _data(tools.list_channels({})) == {"count": 0, "channels": []}


def test_device_metrics_reports_our_own_node(iface, monkeypatch):
    # `altitude` moved behind MESHTASTIC_EXPOSE_LOCATION (item 4); this test is about
    # the metrics fields, so it opts in rather than dropping the assertion.
    monkeypatch.setenv("MESHTASTIC_EXPOSE_LOCATION", "true")
    iface.nodes = {"!aabbccdd": _NODE}
    data = _data(tools.device_metrics({}))
    assert data["node_id"] == "!aabbccdd"
    assert data["battery_level"] == 88
    assert data["voltage"] == 4.0
    assert data["uptime_seconds"] == 1234
    assert data["altitude"] == 30


def test_device_metrics_when_our_node_is_absent_from_the_db(iface):
    iface.nodes = {}
    data = _data(tools.device_metrics({}))
    assert data["node_id"] == "!aabbccdd"
    assert data["battery_level"] is None


def test_device_metrics_without_a_known_node_id(iface):
    iface.myInfo = None
    data = _data(tools.device_metrics({}))
    assert data["node_id"] is None


# ----------------------------------------------------------------------
# knowledge-base tools (fully offline)
# ----------------------------------------------------------------------


def test_recent_messages_reads_the_observer_buffer(monkeypatch):
    # Bodies moved behind MESHTASTIC_EXPOSE_RECENT_TEXT (item 4). This test is about
    # the tool READING THE BUFFER at all, so it opts in; the default-redacted
    # behavior is asserted in test_privacy_gates.py.
    monkeypatch.setenv("MESHTASTIC_EXPOSE_RECENT_TEXT", "true")
    from meshtastic_hermes.observer import get_observer

    obs = get_observer()
    obs.on_receive(
        {
            "fromId": "!11112222",
            "toId": "^all",
            "channel": 0,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello mesh"},
        }
    )
    data = _data(tools.recent_messages({"limit": 5}))
    assert data["messages"][0]["text"] == "hello mesh"


def test_kb_summary_is_valid_json_offline():
    data = _data(tools.kb_summary({}))
    assert data["nodes"] == 0 and data["packets"] == 0
    assert data["db_path"] == ":memory:"


def test_kb_nodes_and_sorting(monkeypatch):
    # The detailed KB views are gated behind MESHTASTIC_EXPOSE_TRAFFIC_METADATA
    # (item 4). This test is about SORTING and LIMITING, so it opts in; the gate
    # itself is asserted in test_privacy_gates.py.
    monkeypatch.setenv("MESHTASTIC_EXPOSE_TRAFFIC_METADATA", "true")
    from meshtastic_hermes.observer import get_observer

    kb = get_observer().kb
    kb.record_packet({"ts": 1.0, "from_node": "!aaa", "to_node": "^all"})
    kb.record_packet({"ts": 2.0, "from_node": "!bbb", "to_node": "^all"})
    kb.record_packet({"ts": 3.0, "from_node": "!bbb", "to_node": "^all"})

    by_packets = _data(tools.kb_nodes({"sort": "packets"}))["nodes"]
    assert [n["node_id"] for n in by_packets] == ["!bbb", "!aaa"]
    assert by_packets[0]["packets"] == 2

    limited = _data(tools.kb_nodes({"limit": 1, "sort": "packets"}))["nodes"]
    assert len(limited) == 1 and limited[0]["node_id"] == "!bbb"


def test_kb_interactions_filters_by_node():
    from meshtastic_hermes.observer import get_observer

    kb = get_observer().kb
    kb.record_packet({"ts": 1.0, "from_node": "!aaa", "to_node": "!bbb"})
    kb.record_packet({"ts": 2.0, "from_node": "!ccc", "to_node": "!ddd"})

    all_rows = _data(tools.kb_interactions({}))
    assert all_rows["count"] == 2
    filtered = _data(tools.kb_interactions({"node_id": "!aaa"}))
    assert filtered["count"] == 1
    since = _data(tools.kb_interactions({"since": 1.5}))
    assert since["count"] == 1


def test_kb_neighbors_requires_a_node_id():
    assert "node_id is required" in _data(tools.kb_neighbors({}))["error"]


def test_kb_neighbors_infers_direct_contacts(monkeypatch):
    # Neighbor inference is gated behind MESHTASTIC_EXPOSE_TRAFFIC_METADATA (item 4).
    # This test is about the INFERENCE, so it opts in; the gate is asserted in
    # test_privacy_gates.py.
    monkeypatch.setenv("MESHTASTIC_EXPOSE_TRAFFIC_METADATA", "true")
    from meshtastic_hermes.observer import get_observer

    kb = get_observer().kb
    kb.record_packet({"ts": 1.0, "from_node": "!aaa", "to_node": "!bbb"})
    kb.record_packet({"ts": 2.0, "from_node": "!bbb", "to_node": "!aaa"})
    kb.record_packet({"ts": 3.0, "from_node": "!aaa", "to_node": "!ccc"})

    data = _data(tools.kb_neighbors({"node_id": "!aaa"}))
    peers = {n["peer"]: n["count"] for n in data["neighbors"]}
    assert peers == {"!bbb": 2, "!ccc": 1}
