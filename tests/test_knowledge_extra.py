"""Knowledge-base gaps: on-disk path handling and no-op writes."""

from __future__ import annotations

from meshtastic_hermes import knowledge


def test_on_disk_db_creates_its_parent_directory(tmp_path):
    db = tmp_path / "nested" / "deeper" / "kb.sqlite"
    kb = knowledge.NodeGraph(str(db))
    try:
        kb.record_packet({"ts": 1.0, "from_node": "!a", "to_node": "^all"})
        assert db.exists()
        assert kb.summary()["db_path"] == str(db)
    finally:
        kb.close()


def test_data_survives_reopening_the_same_file(tmp_path):
    db = str(tmp_path / "kb.sqlite")
    kb = knowledge.NodeGraph(db)
    kb.record_packet({"ts": 1.0, "from_node": "!a", "to_node": "!b"})
    kb.close()

    reopened = knowledge.NodeGraph(db)
    try:
        assert reopened.summary()["packets"] == 1
    finally:
        reopened.close()


def test_upsert_node_ignores_an_empty_node_id():
    kb = knowledge.NodeGraph(":memory:")
    try:
        kb.upsert_node("", 1.0, short_name="nope")
        assert kb.nodes() == []
    finally:
        kb.close()


def test_record_packet_without_a_sender_records_no_node():
    """An interaction row is still written; there is just no sender to roll up."""
    kb = knowledge.NodeGraph(":memory:")
    try:
        kb.record_packet({"ts": 1.0, "to_node": "^all"})
        assert kb.summary()["packets"] == 1
        assert kb.nodes() == []
    finally:
        kb.close()


def test_upsert_node_only_overwrites_provided_columns():
    kb = knowledge.NodeGraph(":memory:")
    try:
        kb.upsert_node("!a", 1.0, short_name="AB", long_name="Alpha Bravo")
        kb.upsert_node("!a", 2.0, short_name=None, hw_model="TBEAM")
        node = kb.nodes()[0]
        assert node["short_name"] == "AB"        # not clobbered by the None
        assert node["long_name"] == "Alpha Bravo"
        assert node["hw_model"] == "TBEAM"
        assert node["last_seen"] == 2.0
        assert node["first_seen"] == 1.0
    finally:
        kb.close()


def test_nodes_sort_orders():
    kb = knowledge.NodeGraph(":memory:")
    try:
        kb.upsert_node("!a", 1.0, long_name="Zulu")
        kb.upsert_node("!b", 2.0, long_name="Alpha")
        assert [n["node_id"] for n in kb.nodes(sort="last_seen")] == ["!b", "!a"]
        assert [n["node_id"] for n in kb.nodes(sort="first_seen")] == ["!a", "!b"]
        assert [n["node_id"] for n in kb.nodes(sort="name")] == ["!b", "!a"]
        # an unknown sort key falls back to last_seen rather than erroring
        assert [n["node_id"] for n in kb.nodes(sort="bogus")] == ["!b", "!a"]
    finally:
        kb.close()


def test_top_talkers_ranks_by_transmission_count():
    kb = knowledge.NodeGraph(":memory:")
    try:
        for _ in range(3):
            kb.record_packet({"ts": 1.0, "from_node": "!loud", "to_node": "^all"})
        kb.record_packet({"ts": 1.0, "from_node": "!quiet", "to_node": "^all"})
        talkers = kb.top_talkers()
        assert talkers[0] == {"node_id": "!loud", "count": 3}
        assert talkers[1] == {"node_id": "!quiet", "count": 1}
    finally:
        kb.close()


def test_default_db_path_priority(monkeypatch, tmp_path):
    monkeypatch.setenv("MESHTASTIC_HERMES_DB", "/explicit/kb.sqlite")
    assert knowledge.default_db_path() == "/explicit/kb.sqlite"

    monkeypatch.delenv("MESHTASTIC_HERMES_DB")
    monkeypatch.setenv("HERMES_HOME", "/var/lib/hermes/.hermes")
    assert knowledge.default_db_path() == "/var/lib/hermes/.hermes/meshtastic_kb.sqlite"

    monkeypatch.delenv("HERMES_HOME")
    # systemd may hand over a colon-separated list; the first entry wins.
    monkeypatch.setenv("STATE_DIRECTORY", "/var/lib/one:/var/lib/two")
    assert knowledge.default_db_path() == "/var/lib/one/meshtastic_kb.sqlite"

    monkeypatch.delenv("STATE_DIRECTORY")
    monkeypatch.setattr(knowledge.Path, "home", classmethod(lambda cls: tmp_path))
    assert knowledge.default_db_path() == str(tmp_path / ".hermes" / "meshtastic_kb.sqlite")
