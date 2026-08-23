"""ConnectionManager.channel_table() — reading channel NAMES off the radio.

This is the one place the plugin touches the radio's channel table; the allowlist
policy that consumes it is pure (see tests/test_channel_names.py). The fake stands
in for `meshtastic.node.Node`'s real shape: `iface.localNode.channels`, each entry
carrying `.settings.name`, `.index` and `.role` (0=DISABLED, 1=PRIMARY, 2=SECONDARY).
"""

from __future__ import annotations

import types

from meshtastic_hermes import connection


def _channel(index, name, role):
    return types.SimpleNamespace(
        index=index, role=role, settings=types.SimpleNamespace(name=name, psk=b"\x01")
    )


def _iface(*channels):
    return types.SimpleNamespace(localNode=types.SimpleNamespace(channels=list(channels)))


def _mgr_with(iface):
    m = connection.ConnectionManager()
    m._iface = iface
    return m


def test_channel_table_reports_index_name_and_role():
    m = _mgr_with(_iface(_channel(0, "", 1), _channel(1, "in.secure", 2)))
    assert m.channel_table() == [
        {"index": 0, "name": "", "role": 1},
        {"index": 1, "name": "in.secure", "role": 2},
    ]


def test_channel_table_skips_disabled_slots():
    """DISABLED entries are empty slots, not channels the radio can transmit on."""
    m = _mgr_with(_iface(_channel(0, "", 1), _channel(1, "", 0), _channel(2, "in.secure", 2)))
    assert [row["index"] for row in m.channel_table()] == [0, 2]


def test_channel_table_falls_back_to_position_when_index_is_absent():
    ch = types.SimpleNamespace(role=2, settings=types.SimpleNamespace(name="in.secure"))
    m = _mgr_with(_iface(_channel(0, "", 1), ch))
    assert m.channel_table()[1] == {"index": 1, "name": "in.secure", "role": 2}


def test_channel_table_is_empty_when_disconnected():
    assert connection.ConnectionManager().channel_table() == []


def test_channel_table_tolerates_a_radio_without_a_local_node():
    assert _mgr_with(types.SimpleNamespace()).channel_table() == []


def test_channel_table_tolerates_settings_without_a_name():
    ch = types.SimpleNamespace(index=1, role=2, settings=types.SimpleNamespace())
    assert _mgr_with(_iface(ch)).channel_table() == [{"index": 1, "name": "", "role": 2}]


def test_channel_table_feeds_resolution_end_to_end():
    """The radio-side reader and the pure policy agree on the row shape."""
    from meshtastic_hermes import gateway_bridge as gb

    m = _mgr_with(_iface(_channel(0, "", 1), _channel(2, "in.secure", 2)))
    allowed, resolved = gb.resolve_channel_spec(
        gb.parse_channel_spec("in.secure"), m.channel_table()
    )
    assert allowed == {2}
    assert resolved == {"in.secure": 2}
