import json

from meshtastic_hermes import tools


def test_meshagatchi_tool_uses_structured_sidecar_request(monkeypatch):
    captured = {}

    def fake_round_trip(path, payload):
        captured.update(path=path, payload=payload)
        return {"type": "event.result", "id": "x", "status": "applied", "applied_deltas": {"health": -2}}

    monkeypatch.setenv("MESHTASTIC_MESHAGATCHI_SOCKET", "/tmp/meshagatchi.sock")
    monkeypatch.setattr(tools.ipc, "round_trip", fake_round_trip)
    result = json.loads(tools.meshagatchi_submit_event({
        "event_id": "quake", "description": "quake",
        "effects": [{"property": "health", "delta": -2}],
    }))
    assert result["status"] == "applied"
    assert captured["payload"]["op"] == "event.submit"
    assert captured["payload"]["event"]["event_id"] == "quake"
