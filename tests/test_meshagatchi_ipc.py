"""Adapter-level coverage for the optional Meshagatchi sidecar socket."""

pytest_plugins = ["test_adapter_runtime"]

import asyncio
import json
from types import SimpleNamespace

import pytest

from test_adapter_runtime import _FakeManager, _make, _patch_manager


@pytest.mark.asyncio
async def test_ipc_forwards_channel_one_and_routes_send(adapter_mod, monkeypatch, tmp_path):
    socket_path = tmp_path / "meshagatchi.sock"
    monkeypatch.setenv("MESHTASTIC_MESHAGATCHI_SOCKET", str(socket_path))
    monkeypatch.setenv("MESHTASTIC_MESHAGATCHI_CHANNEL", "in.secure")
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "in.secure")
    manager = _FakeManager(channels=[
        {"index": 0, "name": "", "role": 1},
        {"index": 1, "name": "in.secure", "role": 2},
    ])
    _patch_manager(monkeypatch, manager)
    adapter = _make(adapter_mod, monkeypatch)
    sent = []

    async def fake_send(chat_id, content, **kwargs):
        sent.append((chat_id, content))
        return SimpleNamespace(success=True, error=None)

    adapter.send = fake_send
    await adapter.connect()
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    hello = json.loads((await reader.readline()).decode())
    assert hello["channel_index"] == 1
    assert hello["channel_name"] == "in.secure"

    writer.write((json.dumps({
        "op": "register", "id": "r1", "version": 1, "role": "meshagatchi",
        "channel_name": "in.secure", "channel_index": 1, "pet_name": "Meshagatchi",
        "max_command_hops": 1, "benign_min_hops": 2, "benign_max_hops": 3,
    }) + "\n").encode())
    await writer.drain()
    assert json.loads((await reader.readline()).decode())["ok"] is True

    writer.write((json.dumps({
        "op": "send", "text": "reply", "channel_name": "in.secure", "channel_index": 1,
    }) + "\n").encode())
    await writer.drain()
    assert json.loads((await reader.readline()).decode())["ok"] is True
    assert sent == [("ch:1", "reply")]

    await adapter._publish_meshagatchi({
        "text": "@Meshagatchi /status", "from_id": "!user", "message_id": "42",
        "is_dm": False, "channel": 1, "hops": 0,
    })
    event = json.loads((await reader.readline()).decode())
    assert event["type"] == "message"
    assert event["channel_index"] == 1
    assert event["text"] == "/status"

    writer.close()
    await writer.wait_closed()
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_event_request_is_forwarded_to_registered_sidecar(adapter_mod, monkeypatch, tmp_path):
    socket_path = tmp_path / "meshagatchi.sock"
    monkeypatch.setenv("MESHTASTIC_MESHAGATCHI_SOCKET", str(socket_path))
    monkeypatch.setenv("MESHTASTIC_MESHAGATCHI_CHANNEL", "in.secure")
    manager = _FakeManager(channels=[
        {"index": 0, "name": "", "role": 1},
        {"index": 1, "name": "in.secure", "role": 2},
    ])
    _patch_manager(monkeypatch, manager)
    adapter = _make(adapter_mod, monkeypatch)
    await adapter.connect()

    bot_reader, bot_writer = await asyncio.open_unix_connection(str(socket_path))
    await bot_reader.readline()
    bot_writer.write((json.dumps({"op": "register", "id": "r1", "version": 1,
                                  "role": "meshagatchi", "channel_name": "in.secure",
                                  "channel_index": 1, "pet_name": "Meshagatchi",
                                  "max_command_hops": 1, "benign_min_hops": 2,
                                  "benign_max_hops": 3}) + "\n").encode())
    await bot_writer.drain()
    assert json.loads(await bot_reader.readline())["ok"] is True

    tool_reader, tool_writer = await asyncio.open_unix_connection(str(socket_path))
    await tool_reader.readline()
    tool_writer.write((json.dumps({"op": "event.submit", "id": "e1", "version": 1,
                                   "channel_name": "in.secure", "channel_index": 1,
                                   "event": {"event_id": "quake", "description": "quake",
                                              "effects": [{"property": "health", "delta": -2}]}}) + "\n").encode())
    await tool_writer.drain()
    forwarded = json.loads(await bot_reader.readline())
    assert forwarded["type"] == "event.submit"
    assert forwarded["event"]["event_id"] == "quake"
    bot_writer.write((json.dumps({"type": "event.result", "id": "e1", "ok": True,
                                  "status": "applied", "applied_deltas": {"health": -2}}) + "\n").encode())
    await bot_writer.drain()
    result = json.loads(await tool_reader.readline())
    assert result["status"] == "applied"

    tool_writer.close()
    bot_writer.close()
    await tool_writer.wait_closed()
    await bot_writer.wait_closed()
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_meshagatchi_filter_requires_trigger_and_hop_tier(adapter_mod, monkeypatch, tmp_path):
    socket_path = tmp_path / "meshagatchi.sock"
    monkeypatch.setenv("MESHTASTIC_MESHAGATCHI_SOCKET", str(socket_path))
    monkeypatch.setenv("MESHTASTIC_MESHAGATCHI_CHANNEL", "in.secure")
    manager = _FakeManager(channels=[
        {"index": 0, "name": "", "role": 1},
        {"index": 1, "name": "in.secure", "role": 2},
    ])
    _patch_manager(monkeypatch, manager)
    adapter = _make(adapter_mod, monkeypatch)
    await adapter.connect()
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    await reader.readline()
    writer.write((json.dumps({
        "op": "register", "id": "r1", "version": 1, "role": "meshagatchi",
        "channel_name": "in.secure", "channel_index": 1, "pet_name": "BoneMurder",
        "max_command_hops": 1, "benign_min_hops": 2, "benign_max_hops": 3,
    }) + "\n").encode())
    await writer.drain()
    assert json.loads(await reader.readline())["ok"] is True

    async def publish(text, hops):
        await adapter._publish_meshagatchi({
            "text": text, "from_id": "!user", "message_id": str(hops),
            "is_dm": False, "channel": 1, "hops": hops,
        })

    async def no_message():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.readline(), 0.03)

    await publish("/ping", 0)
    await no_message()
    await publish("@BoneMurder /feed cookie", 2)
    await no_message()
    await publish("@BoneMurder /ping", 2)
    benign = json.loads(await asyncio.wait_for(reader.readline(), 1))
    assert benign["text"] == "/ping"
    assert benign["hops"] == 2
    await publish("@BoneMurder /feed cookie", 1)
    full = json.loads(await asyncio.wait_for(reader.readline(), 1))
    assert full["text"] == "/feed cookie"
    assert full["raw_text"] == "@BoneMurder /feed cookie"

    writer.close()
    await writer.wait_closed()
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_personality_uses_host_llm_without_exposing_socket_or_credentials(adapter_mod, monkeypatch, tmp_path):
    class Result:
        text = "Dramatic tiny disaster."

    class LLM:
        async def acomplete(self, **kwargs):
            assert "socket_path" not in json.dumps(kwargs)
            assert "MESHTASTIC_HOST" not in json.dumps(kwargs)
            return Result()

    monkeypatch.setattr(adapter_mod, "_HERMES_CTX", SimpleNamespace(llm=LLM()))
    socket_path = tmp_path / "meshagatchi.sock"
    monkeypatch.setenv("MESHTASTIC_MESHAGATCHI_SOCKET", str(socket_path))
    monkeypatch.setenv("MESHTASTIC_MESHAGATCHI_CHANNEL", "in.secure")
    manager = _FakeManager(channels=[
        {"index": 0, "name": "", "role": 1},
        {"index": 1, "name": "in.secure", "role": 2},
    ])
    _patch_manager(monkeypatch, manager)
    adapter = _make(adapter_mod, monkeypatch)
    await adapter.connect()
    response = await adapter._personality({
        "op": "personality.request", "id": "p1", "context": {"state": {"health": 80}, "persona": "dramatic"}
    })
    assert response["ok"] is True
    assert response["text"] == "Dramatic tiny disaster."
    await adapter.disconnect()
