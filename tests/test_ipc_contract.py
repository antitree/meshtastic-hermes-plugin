from meshtastic_hermes.ipc import hello_payload, message_payload, validate_send_request


def test_send_request_is_pinned_to_channel_identity():
    request = {"op": "send", "text": "hello", "channel_name": "in.secure", "channel_index": 1}
    assert validate_send_request(request, channel_name="in.secure", channel_index=1) == ("hello", None)
    assert validate_send_request({**request, "channel_index": 0}, channel_name="in.secure", channel_index=1)[1]
    assert validate_send_request({**request, "channel_name": "LongFast"}, channel_name="in.secure", channel_index=1)[1]


def test_send_request_rejects_oversized_text():
    request = {"op": "send", "text": "x" * 201, "channel_name": "in.secure", "channel_index": 1}
    assert validate_send_request(request, channel_name="in.secure", channel_index=1)[1]


def test_payloads_are_stable_json_shapes():
    assert hello_payload("!ed61becd", "in.secure", 1)["type"] == "hello"
    payload = message_payload({"text": "/status", "from_id": "!aabbccdd"}, "in.secure", 1)
    assert payload["channel_index"] == 1
    assert payload["text"] == "/status"

