"""Connect-target policy — security remediation item 2.

`meshtastic_connect` used to accept any caller-supplied host/port, which let a
single tool call repoint the process-wide Meshtastic link away from the
configured radio. MESHTASTIC_HOST is now authoritative, and a rejected connect
must be a complete no-op: it may not overwrite the manager's target, and it may
not disturb an existing healthy connection.

No radio and no sockets: the manager is driven with a fake `meshtastic` module,
the same way tests/test_connection.py does it.
"""

from __future__ import annotations

import json
import types

import pytest

from meshtastic_hermes import connection, tools


def _data(raw: str) -> dict:
    return json.loads(raw)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test in this module states its own policy env explicitly."""
    for var in (
        "MESHTASTIC_HOST",
        "MESHTASTIC_ALLOW_DYNAMIC_HOSTS",
        "MESHTASTIC_ALLOWED_HOSTS",
    ):
        monkeypatch.delenv(var, raising=False)


class _FakeIface:
    def __init__(self, host, portNumber=4403):
        self.host = host
        self.port = portNumber
        self.closed = False
        self.myInfo = types.SimpleNamespace(my_node_num=0xAABBCCDD)
        self.nodes = {"!aabbccdd": {"user": {"shortName": "MESH", "longName": "Meshy"}}}

    def close(self):
        self.closed = True


@pytest.fixture
def fake_radio(monkeypatch):
    built: list[_FakeIface] = []

    def factory(host, portNumber=4403):
        iface = _FakeIface(host, portNumber)
        built.append(iface)
        return iface

    mesh = types.SimpleNamespace(tcp_interface=types.SimpleNamespace(TCPInterface=factory))
    monkeypatch.setattr(connection, "_import_meshtastic", lambda: mesh)
    return built


@pytest.fixture
def calls(monkeypatch):
    """Record what the tool would hand to ConnectionManager.connect()."""
    recorded: list[tuple] = []
    mgr = connection.get_manager()
    monkeypatch.setattr(
        mgr,
        "connect",
        lambda h, p=connection.DEFAULT_TCP_PORT: recorded.append((h, p))
        or {"connected": True, "state": "connected", "host": h},
    )
    return recorded


# ----------------------------------------------------------------------
# MESHTASTIC_HOST is authoritative
# ----------------------------------------------------------------------


def test_env_host_is_used_when_no_host_is_supplied(monkeypatch, calls):
    monkeypatch.setenv("MESHTASTIC_HOST", "radio.local")
    data = _data(tools.connect({}))
    assert calls == [("radio.local", 4403)]
    assert data["status"] == "connected"


def test_supplied_host_matching_the_env_host_is_accepted(monkeypatch, calls):
    monkeypatch.setenv("MESHTASTIC_HOST", "radio.local")
    tools.connect({"host": "radio.local"})
    assert calls == [("radio.local", 4403)]


def test_a_different_supplied_host_is_rejected(monkeypatch, calls):
    monkeypatch.setenv("MESHTASTIC_HOST", "radio.local")
    data = _data(tools.connect({"host": "other.local"}))
    assert data["code"] == "host_not_allowed"
    assert "MESHTASTIC_HOST" in data["error"]
    assert calls == []  # the manager was never called at all


def test_env_host_wins_even_when_dynamic_hosts_are_enabled(monkeypatch, calls):
    """The opt-in is for the MESHTASTIC_HOST-unset case, not an override switch."""
    monkeypatch.setenv("MESHTASTIC_HOST", "radio.local")
    monkeypatch.setenv("MESHTASTIC_ALLOW_DYNAMIC_HOSTS", "true")
    assert _data(tools.connect({"host": "other.local"}))["code"] == "host_not_allowed"
    assert calls == []


# ----------------------------------------------------------------------
# Dynamic hosts
# ----------------------------------------------------------------------


def test_dynamic_hosts_are_rejected_by_default(calls):
    data = _data(tools.connect({"host": "192.0.2.10"}))
    assert data["code"] == "dynamic_hosts_disabled"
    assert calls == []


def test_no_host_and_no_env_host_is_rejected(calls):
    data = _data(tools.connect({}))
    assert data["code"] == "no_host"
    assert "MESHTASTIC_HOST is not set" in data["error"]
    assert calls == []


def test_dynamic_hosts_opt_in_accepts_the_supplied_host(monkeypatch, calls):
    monkeypatch.setenv("MESHTASTIC_ALLOW_DYNAMIC_HOSTS", "true")
    tools.connect({"host": "192.0.2.10"})
    assert calls == [("192.0.2.10", 4403)]


@pytest.mark.parametrize("raw", ["1", "TRUE", "yes", "on"])
def test_truthy_spellings_enable_dynamic_hosts(monkeypatch, calls, raw):
    monkeypatch.setenv("MESHTASTIC_ALLOW_DYNAMIC_HOSTS", raw)
    tools.connect({"host": "192.0.2.10"})
    assert calls == [("192.0.2.10", 4403)]


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "ture", "maybe"])
def test_non_truthy_values_fail_closed(monkeypatch, calls, raw):
    """A typo must leave the restriction ON, not silently open the door."""
    monkeypatch.setenv("MESHTASTIC_ALLOW_DYNAMIC_HOSTS", raw)
    assert _data(tools.connect({"host": "192.0.2.10"}))["code"] == "dynamic_hosts_disabled"
    assert calls == []


# ----------------------------------------------------------------------
# MESHTASTIC_ALLOWED_HOSTS
# ----------------------------------------------------------------------


def test_allowlist_permits_a_listed_hostname(monkeypatch, calls):
    monkeypatch.setenv("MESHTASTIC_ALLOW_DYNAMIC_HOSTS", "true")
    monkeypatch.setenv("MESHTASTIC_ALLOWED_HOSTS", "your-host.example.com, 192.0.2.10")
    tools.connect({"host": "your-host.example.com"})
    assert calls == [("your-host.example.com", 4403)]


def test_allowlist_rejects_an_unlisted_host(monkeypatch, calls):
    monkeypatch.setenv("MESHTASTIC_ALLOW_DYNAMIC_HOSTS", "true")
    monkeypatch.setenv("MESHTASTIC_ALLOWED_HOSTS", "your-host.example.com")
    data = _data(tools.connect({"host": "192.0.2.10"}))
    assert data["code"] == "host_not_in_allowlist"
    assert calls == []


def test_allowlist_matches_a_cidr_range(monkeypatch, calls):
    monkeypatch.setenv("MESHTASTIC_ALLOW_DYNAMIC_HOSTS", "true")
    monkeypatch.setenv("MESHTASTIC_ALLOWED_HOSTS", "192.0.2.0/24")
    tools.connect({"host": "192.0.2.10"})
    assert calls == [("192.0.2.10", 4403)]


def test_allowlist_cidr_does_not_match_an_outside_address(monkeypatch, calls):
    monkeypatch.setenv("MESHTASTIC_ALLOW_DYNAMIC_HOSTS", "true")
    monkeypatch.setenv("MESHTASTIC_ALLOWED_HOSTS", "192.0.2.0/24")
    assert _data(tools.connect({"host": "198.51.100.7"}))["code"] == "host_not_in_allowlist"
    assert calls == []


def test_allowlist_cidr_entry_does_not_match_a_hostname(monkeypatch, calls):
    """No DNS resolution: a name never inherits a CIDR entry's authorization."""
    monkeypatch.setenv("MESHTASTIC_ALLOW_DYNAMIC_HOSTS", "true")
    monkeypatch.setenv("MESHTASTIC_ALLOWED_HOSTS", "192.0.2.0/24")
    assert _data(tools.connect({"host": "evil.example"}))["code"] == "host_not_in_allowlist"
    assert calls == []


# ----------------------------------------------------------------------
# Port validation
# ----------------------------------------------------------------------


@pytest.mark.parametrize("port", ["nope", -1, 0, 65536, 99999, 4403.5, True, [4403]])
def test_invalid_ports_are_rejected(monkeypatch, calls, port):
    monkeypatch.setenv("MESHTASTIC_HOST", "radio.local")
    data = _data(tools.connect({"port": port}))
    assert data["code"] == "invalid_port"
    assert calls == []


@pytest.mark.parametrize("port,expected", [(None, 4403), (1, 1), (65535, 65535), ("4403", 4403)])
def test_valid_ports_are_accepted(monkeypatch, calls, port, expected):
    monkeypatch.setenv("MESHTASTIC_HOST", "radio.local")
    args = {} if port is None else {"port": port}
    tools.connect(args)
    assert calls == [("radio.local", expected)]


def test_port_defaults_to_4403():
    assert connection.validate_port(None) == connection.DEFAULT_TCP_PORT == 4403


# ----------------------------------------------------------------------
# A rejected connect must not mutate anything (the regression that matters)
# ----------------------------------------------------------------------


def test_rejected_connect_does_not_overwrite_the_manager_target(monkeypatch, fake_radio):
    monkeypatch.setenv("MESHTASTIC_HOST", "radio.local")
    mgr = connection.get_manager()
    assert _data(tools.connect({}))["status"] == "connected"
    assert mgr._host == "radio.local"
    assert mgr._port == 4403

    data = _data(tools.connect({"host": "attacker.example", "port": 31337}))
    assert data["code"] == "host_not_allowed"
    # The target config the supervisor reconnects with is untouched.
    assert mgr._host == "radio.local"
    assert mgr._port == 4403


def test_rejected_connect_does_not_interrupt_a_healthy_connection(monkeypatch, fake_radio):
    monkeypatch.setenv("MESHTASTIC_HOST", "radio.local")
    mgr = connection.get_manager()
    tools.connect({})
    iface = mgr._iface
    assert iface is not None and iface.host == "radio.local"

    for args in (
        {"host": "attacker.example"},
        {"port": 0},
        {"port": "not-a-port"},
    ):
        assert "error" in _data(tools.connect(args))
        assert mgr._iface is iface  # same live interface object
        assert iface.closed is False  # never torn down
        assert mgr._host == "radio.local"
        assert mgr._port == 4403
        assert mgr.status()["connected"] is True

    assert len(fake_radio) == 1  # no extra interface was ever built


def test_rejected_port_does_not_mutate_the_manager_directly(fake_radio):
    """Even a direct ConnectionManager.connect() validates before assigning."""
    mgr = connection.ConnectionManager()
    try:
        mgr.connect("192.0.2.10", 4403)
        assert mgr._host == "192.0.2.10"
        with pytest.raises(connection.ConnectTargetRejected):
            mgr.connect("attacker.example", 70000)
        assert mgr._host == "192.0.2.10"
        assert mgr._port == 4403
        assert mgr._iface is not None and mgr._iface.closed is False
    finally:
        mgr._want_connected = False
        mgr._stop.set()
        if mgr._supervisor:
            mgr._supervisor.join(timeout=2)


# ----------------------------------------------------------------------
# validate_connect_target as a unit
# ----------------------------------------------------------------------


def test_validate_connect_target_returns_host_and_port(monkeypatch):
    monkeypatch.setenv("MESHTASTIC_HOST", "radio.local")
    assert connection.validate_connect_target(None, None) == ("radio.local", 4403)
    assert connection.validate_connect_target("radio.local", 5000) == ("radio.local", 5000)


def test_validate_connect_target_rejects_a_non_string_host(monkeypatch):
    monkeypatch.setenv("MESHTASTIC_HOST", "radio.local")
    with pytest.raises(connection.ConnectTargetRejected) as exc:
        connection.validate_connect_target(1234, None)
    assert exc.value.code == "invalid_host"


def test_blank_host_falls_back_to_the_env_host(monkeypatch):
    monkeypatch.setenv("MESHTASTIC_HOST", "radio.local")
    assert connection.validate_connect_target("   ", None)[0] == "radio.local"
