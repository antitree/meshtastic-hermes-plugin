"""Regression tests for remediation item 4 — location and mesh-metadata exposure.

The assertions here are deliberately shaped around *sentinels*, not key names. A
test that only checks ``"lat" not in payload`` passes happily while the very same
coordinate sits one level down under ``position`` or is echoed back inside a
``note``. So each test plants a value that could not occur by accident
(``SENTINEL_LAT`` and friends) and asserts it appears NOWHERE in the serialized
JSON the tool actually returns.

Coordinates here are synthetic on purpose: no real location belongs in a tracked
file, which is the whole point of the item under test.
"""

from __future__ import annotations

import json

import pytest

from meshtastic_hermes import privacy, tools

# Values that cannot plausibly appear for any other reason, so finding one anywhere
# in a response is proof of a leak rather than a coincidence.
SENTINEL_LAT = 41.424242424
SENTINEL_LON = -87.135791357
SENTINEL_ALT = 424242
SENTINEL_TEXT = "SENTINEL-PLAINTEXT-DO-NOT-DISCLOSE"

LOCATION_ENV = "MESHTASTIC_EXPOSE_LOCATION"
RECENT_TEXT_ENV = "MESHTASTIC_EXPOSE_RECENT_TEXT"
METADATA_ENV = "MESHTASTIC_EXPOSE_TRAFFIC_METADATA"


@pytest.fixture(autouse=True)
def _gates_off(monkeypatch):
    """Every test starts from the shipped default: all three gates CLOSED.

    Explicit rather than assumed — a stray value inherited from the developer's own
    environment would otherwise turn these into no-ops that pass for the wrong reason.
    """
    for env in (LOCATION_ENV, RECENT_TEXT_ENV, METADATA_ENV):
        monkeypatch.delenv(env, raising=False)


def _raw(result: str) -> str:
    """The tool's response exactly as the model receives it: one JSON string."""
    assert isinstance(result, str)
    return result


def _data(result: str) -> dict:
    return json.loads(result)


def _assert_no_sentinels(result: str, *sentinels) -> None:
    """No sentinel appears anywhere in the serialized response.

    Scanning the raw JSON text — not a dict lookup — is what catches a nested copy
    of the same value under a different key, which is the actual failure mode a
    key-name assertion misses.
    """
    blob = _raw(result)
    for sentinel in sentinels:
        assert str(sentinel) not in blob, f"{sentinel!r} leaked into {blob}"


# ----------------------------------------------------------------------
# fakes — a live radio node DB, and the persisted KB
# ----------------------------------------------------------------------


class _FakeIface:
    """Just enough of a TCPInterface for the read tools, carrying a sentinel position."""

    def __init__(self):
        import types

        self.myInfo = types.SimpleNamespace(my_node_num=0xAABBCCDD)
        self.nodes = {
            "!aabbccdd": {
                "user": {"shortName": "ME", "longName": "Local Node"},
                "deviceMetrics": {"batteryLevel": 91, "voltage": 4.1, "uptimeSeconds": 60},
                "position": {
                    "latitude": SENTINEL_LAT,
                    "longitude": SENTINEL_LON,
                    "altitude": SENTINEL_ALT,
                },
                "snr": 5.5,
                "lastHeard": 1700000000,
            },
            "!11112222": {
                "user": {"shortName": "PR", "longName": "Peer"},
                "deviceMetrics": {"batteryLevel": 40},
                "position": {
                    "latitude": SENTINEL_LAT,
                    "longitude": SENTINEL_LON,
                    "altitude": SENTINEL_ALT,
                },
                "snr": 2.0,
            },
        }
        self.localNode = None

    def close(self):
        pass


@pytest.fixture
def iface():
    """Point the process-wide ConnectionManager at a fake radio.

    Same injection point the existing tool tests use (``mgr._iface``), so these run
    against the real handler code path rather than a shortcut.
    """
    from meshtastic_hermes.connection import get_manager

    fake = _FakeIface()
    get_manager()._iface = fake
    return fake


@pytest.fixture
def kb():
    """The shared KB, seeded so BOTH sensitive paths carry the same sentinels.

    The ``nodes`` table has its own ``lat``/``lon`` columns, populated independently
    of the radio's live node DB. That second path is exactly what the spec's last
    validation bullet is about.
    """
    from meshtastic_hermes.observer import get_observer

    graph = get_observer().kb
    graph.upsert_node(
        "!11112222",
        1.0,
        short_name="PR",
        long_name="Peer",
        lat=SENTINEL_LAT,
        lon=SENTINEL_LON,
    )
    graph.record_packet({"ts": 1.0, "from_node": "!11112222", "to_node": "!aabbccdd"})
    graph.record_packet({"ts": 2.0, "from_node": "!11112222", "to_node": "!aabbccdd"})
    graph.record_packet({"ts": 3.0, "from_node": "!aabbccdd", "to_node": "!11112222"})
    return graph


@pytest.fixture
def buffered_text():
    """Put a sentinel plaintext message into the observer's RAM buffer."""
    from meshtastic_hermes.observer import get_observer

    obs = get_observer()
    obs.on_receive(
        {
            "fromId": "!11112222",
            "toId": "^all",
            "channel": 1,
            "rxTime": 100.0,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": SENTINEL_TEXT},
        }
    )
    return obs


# ----------------------------------------------------------------------
# env parsing — exposure switches must FAIL CLOSED
# ----------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " true "])
def test_exposure_flags_accept_explicit_truthy_values(monkeypatch, value):
    monkeypatch.setenv(LOCATION_ENV, value)
    monkeypatch.setenv(RECENT_TEXT_ENV, value)
    monkeypatch.setenv(METADATA_ENV, value)
    assert privacy.expose_location() is True
    assert privacy.expose_recent_text() is True
    assert privacy.expose_traffic_metadata() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "ture", "y", "enabled"])
def test_exposure_flags_fail_closed_on_anything_else(monkeypatch, value):
    """A typo must REDACT. Same polarity as MESHTASTIC_DEBUG_LOG_TEXT (item 5)."""
    monkeypatch.setenv(LOCATION_ENV, value)
    monkeypatch.setenv(RECENT_TEXT_ENV, value)
    monkeypatch.setenv(METADATA_ENV, value)
    assert privacy.expose_location() is False
    assert privacy.expose_recent_text() is False
    assert privacy.expose_traffic_metadata() is False


def test_exposure_flags_default_off_when_unset():
    assert privacy.expose_location() is False
    assert privacy.expose_recent_text() is False
    assert privacy.expose_traffic_metadata() is False


# ----------------------------------------------------------------------
# Validation bullet 1: location absent by default from the LIVE radio DB
# ----------------------------------------------------------------------


def test_list_nodes_omits_location_by_default(iface):
    result = tools.list_nodes({})
    _assert_no_sentinels(result, SENTINEL_LAT, SENTINEL_LON, SENTINEL_ALT)
    data = _data(result)
    # Still useful: the non-sensitive fields survive, so the tool is not just broken.
    assert data["count"] == 2
    assert data["nodes"][0]["short_name"] == "ME"
    assert data["nodes"][0]["battery"] == 91
    # And the model is told the position was WITHHELD, not that there is none.
    assert data["location_redacted"] is True


def test_node_info_omits_location_by_default(iface):
    result = tools.node_info({"node_id": "!11112222"})
    _assert_no_sentinels(result, SENTINEL_LAT, SENTINEL_LON, SENTINEL_ALT)
    data = _data(result)
    assert data["id"] == "!11112222"
    assert data["long_name"] == "Peer"
    assert data["location_redacted"] is True


def test_device_metrics_omits_local_position_by_default(iface):
    """Our OWN position is gated too — it locates the operator just as precisely."""
    result = tools.device_metrics({})
    _assert_no_sentinels(result, SENTINEL_LAT, SENTINEL_LON, SENTINEL_ALT)
    data = _data(result)
    assert data["battery_level"] == 91
    assert data["uptime_seconds"] == 60
    assert data["location_redacted"] is True


# ----------------------------------------------------------------------
# Validation bullet 2: MESHTASTIC_EXPOSE_LOCATION=true restores location
# ----------------------------------------------------------------------


def test_expose_location_restores_fields_on_list_nodes(iface, monkeypatch):
    monkeypatch.setenv(LOCATION_ENV, "true")
    data = _data(tools.list_nodes({}))
    assert data["nodes"][0]["lat"] == SENTINEL_LAT
    assert data["nodes"][0]["lon"] == SENTINEL_LON
    assert "location_redacted" not in data


def test_expose_location_restores_fields_on_node_info(iface, monkeypatch):
    monkeypatch.setenv(LOCATION_ENV, "true")
    data = _data(tools.node_info({"node_id": "!11112222"}))
    assert data["lat"] == SENTINEL_LAT
    assert data["lon"] == SENTINEL_LON


def test_expose_location_restores_fields_on_device_metrics(iface, monkeypatch):
    monkeypatch.setenv(LOCATION_ENV, "true")
    data = _data(tools.device_metrics({}))
    assert data["lat"] == SENTINEL_LAT
    assert data["lon"] == SENTINEL_LON
    assert data["altitude"] == SENTINEL_ALT


# ----------------------------------------------------------------------
# Validation bullet 3: recent_messages returns no plaintext by default
# ----------------------------------------------------------------------


def test_recent_messages_withholds_plaintext_by_default(buffered_text):
    result = tools.recent_messages({})
    _assert_no_sentinels(result, SENTINEL_TEXT)
    data = _data(result)
    # Redacted metadata, not a dropped row: the model can still see that a message
    # arrived, from whom, on which channel, and when.
    assert data["count"] == 1
    row = data["messages"][0]
    assert row["from"] == "!11112222"
    assert row["channel"] == 1
    assert row["ts"] == 100.0
    assert row["text_redacted"] is True
    assert row["text_len"] == len(SENTINEL_TEXT)
    assert len(row["text_sha256"]) == 8
    assert data["text_redacted"] is True


def test_recent_messages_hash_matches_the_debug_log_helper(buffered_text):
    """The tool and the journal describe the same body identically (item 5's shape)."""
    from meshtastic_platform.adapter import debug_text_for_log

    row = _data(tools.recent_messages({}))["messages"][0]
    rendered = debug_text_for_log(SENTINEL_TEXT)
    assert f"text_len={row['text_len']}" in rendered
    assert f"text_sha256={row['text_sha256']}" in rendered


def test_recent_text_gate_is_not_opened_by_the_debug_log_switch(buffered_text, monkeypatch):
    """MESHTASTIC_DEBUG_LOG_TEXT governs the JOURNAL, never a tool response.

    They are different audiences: the journal is the operator's own machine, while a
    tool response goes to the model and onward. Enabling one must not widen the other.
    """
    monkeypatch.setenv("MESHTASTIC_DEBUG_LOG_TEXT", "true")
    monkeypatch.setenv("MESHTASTIC_DEBUG", "1")
    _assert_no_sentinels(tools.recent_messages({}), SENTINEL_TEXT)


# ----------------------------------------------------------------------
# Validation bullet 4: MESHTASTIC_EXPOSE_RECENT_TEXT=true restores plaintext
# ----------------------------------------------------------------------


def test_expose_recent_text_restores_plaintext(buffered_text, monkeypatch):
    monkeypatch.setenv(RECENT_TEXT_ENV, "true")
    data = _data(tools.recent_messages({}))
    assert data["messages"][0]["text"] == SENTINEL_TEXT
    assert "text_redacted" not in data


# ----------------------------------------------------------------------
# Validation bullet 5: detailed KB tools gated by traffic metadata
# ----------------------------------------------------------------------


def test_kb_interactions_withholds_records_by_default(kb):
    data = _data(tools.kb_interactions({}))
    assert data["interactions"] == []
    assert data["traffic_metadata_redacted"] is True
    assert data["required_env"] == METADATA_ENV
    # The COUNT survives — "has there been traffic" names nobody.
    assert data["count"] == 3


def test_kb_neighbors_withholds_the_social_graph_by_default(kb):
    data = _data(tools.kb_neighbors({"node_id": "!11112222"}))
    assert data["neighbors"] == []
    assert data["traffic_metadata_redacted"] is True
    # The peer id itself is the disclosure here, so it must not appear in the rows.
    assert "!aabbccdd" not in json.dumps(data["neighbors"])


def test_kb_nodes_withholds_rows_by_default(kb):
    data = _data(tools.kb_nodes({}))
    assert "nodes" not in data or data["nodes"] == []
    assert data["traffic_metadata_redacted"] is True


def test_kb_summary_counts_remain_available_by_default(kb):
    """Aggregate counts are NOT gated — they name nobody and locate nobody."""
    data = _data(tools.kb_summary({}))
    assert data["packets"] == 3
    assert data["nodes"] >= 1
    # ...but top_talkers names and ranks specific nodes, so it rides the gate.
    assert "top_talkers" not in data
    assert data["top_talkers_redacted"] is True


def test_expose_traffic_metadata_restores_the_detailed_views(kb, monkeypatch):
    monkeypatch.setenv(METADATA_ENV, "true")

    interactions = _data(tools.kb_interactions({}))
    assert len(interactions["interactions"]) == 3

    neighbors = _data(tools.kb_neighbors({"node_id": "!11112222"}))
    assert neighbors["neighbors"][0]["peer"] == "!aabbccdd"

    nodes = _data(tools.kb_nodes({}))
    assert any(n["node_id"] == "!11112222" for n in nodes["nodes"])

    assert "top_talkers" in _data(tools.kb_summary({}))


def test_kb_neighbors_still_validates_its_argument_while_gated():
    """The gate must not swallow ordinary argument errors."""
    assert "node_id is required" in _data(tools.kb_neighbors({}))["error"]


# ----------------------------------------------------------------------
# Validation bullet 6: redaction is CONSISTENT across both sensitive paths
# ----------------------------------------------------------------------


def test_location_is_redacted_on_the_persisted_kb_path_too(kb, monkeypatch):
    """The KB `nodes` table has its own lat/lon columns — a second, independent path.

    Opening the traffic-metadata gate unlocks the ROWS. It must not unlock the
    COORDINATES: that takes MESHTASTIC_EXPOSE_LOCATION, on this path exactly as on
    the live radio path. A redaction helper wired into only one path passes the
    live-DB tests above and still leaks here.
    """
    monkeypatch.setenv(METADATA_ENV, "true")
    result = tools.kb_nodes({})
    _assert_no_sentinels(result, SENTINEL_LAT, SENTINEL_LON)
    data = _data(result)
    row = next(n for n in data["nodes"] if n["node_id"] == "!11112222")
    # The row itself is intact and useful — only the coordinates are gone.
    assert row["long_name"] == "Peer"
    assert row["packets"] == 2
    assert "lat" not in row and "lon" not in row
    assert data["location_redacted"] is True


def test_expose_location_restores_coordinates_on_the_kb_path(kb, monkeypatch):
    monkeypatch.setenv(METADATA_ENV, "true")
    monkeypatch.setenv(LOCATION_ENV, "true")
    row = next(n for n in _data(tools.kb_nodes({}))["nodes"] if n["node_id"] == "!11112222")
    assert row["lat"] == SENTINEL_LAT
    assert row["lon"] == SENTINEL_LON


def test_both_paths_redact_identically_in_one_process(iface, kb):
    """The same coordinate, reachable two ways, is withheld both ways at once.

    This is the trap the spec's last bullet names: `iface.nodes` (live radio) and the
    SQLite `nodes` table are independently populated, and a gate on one says nothing
    about the other.
    """
    for result in (tools.list_nodes({}), tools.kb_nodes({})):
        _assert_no_sentinels(result, SENTINEL_LAT, SENTINEL_LON)


def test_redact_location_walks_nested_structures():
    """A coordinate nested under another key is exactly the leak a top-level key
    check would miss, so the helper recurses."""
    nested = {
        "node": {"id": "!x", "position": {"latitude": SENTINEL_LAT}},
        "peers": [{"lat": SENTINEL_LAT, "lon": SENTINEL_LON, "name": "keep"}],
    }
    out = privacy.redact_location(nested)
    assert str(SENTINEL_LAT) not in json.dumps(out)
    assert out["peers"][0]["name"] == "keep"
    # The caller's own record is untouched — redaction copies, it does not mutate the
    # internal store it was handed.
    assert nested["peers"][0]["lat"] == SENTINEL_LAT


# ----------------------------------------------------------------------
# Integration: the REGISTERED handlers redact, not just the helpers
# ----------------------------------------------------------------------


@pytest.fixture
def registry():
    """Register the plugin through the fake Hermes context (item 1's pattern)."""
    from meshtastic_hermes.__main__ import build_registry

    return build_registry()


def test_registered_read_handlers_redact_location(registry, iface):
    for name in (
        "meshtastic_list_nodes",
        "meshtastic_node_info",
        "meshtastic_device_metrics",
    ):
        result = registry.tools[name]["handler"]({})
        _assert_no_sentinels(result, SENTINEL_LAT, SENTINEL_LON, SENTINEL_ALT)


def test_registered_recent_messages_handler_redacts_plaintext(registry, buffered_text):
    result = registry.tools["meshtastic_recent_messages"]["handler"]({})
    _assert_no_sentinels(result, SENTINEL_TEXT)


def test_registered_kb_handlers_are_gated(registry, kb):
    interactions = _data(registry.tools["meshtastic_kb_interactions"]["handler"]({}))
    assert interactions["traffic_metadata_redacted"] is True

    neighbors = _data(
        registry.tools["meshtastic_kb_neighbors"]["handler"]({"node_id": "!11112222"})
    )
    assert neighbors["traffic_metadata_redacted"] is True


def test_registered_kb_nodes_handler_redacts_location_when_ungated(registry, kb, monkeypatch):
    """Both gates, both paths, through the registered handler."""
    monkeypatch.setenv(METADATA_ENV, "true")
    result = registry.tools["meshtastic_kb_nodes"]["handler"]({})
    _assert_no_sentinels(result, SENTINEL_LAT, SENTINEL_LON)
