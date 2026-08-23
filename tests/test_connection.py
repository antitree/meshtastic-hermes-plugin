"""ConnectionManager tests — no radio, no socket.

The manager is driven by injecting a fake `meshtastic` module so `_open()` builds
a fake TCPInterface instead of dialing a node. That exercises the real
subscribe/close/reconnect bookkeeping, which is where the interesting bugs have
historically been (duplicate subscriptions, stale-interface handling, and the
regression where `_close_locked` nulled the reconnect target host).
"""

from __future__ import annotations

import sys
import threading
import types

import pytest

from meshtastic_hermes import connection


class _FakeIface:
    def __init__(self, host, portNumber=4403):
        self.host = host
        self.port = portNumber
        self.closed = False
        self.myInfo = types.SimpleNamespace(my_node_num=0xAABBCCDD)
        self.nodes = {
            "!aabbccdd": {"user": {"shortName": "MESH", "longName": "Meshy Gateway"}}
        }

    def close(self):
        self.closed = True


@pytest.fixture
def fake_radio(monkeypatch):
    """Install a fake `meshtastic` package so _open() never touches a socket."""
    built: list[_FakeIface] = []

    def factory(host, portNumber=4403):
        iface = _FakeIface(host, portNumber)
        built.append(iface)
        return iface

    tcp_mod = types.SimpleNamespace(TCPInterface=factory)
    mesh = types.SimpleNamespace(tcp_interface=tcp_mod)
    monkeypatch.setattr(connection, "_import_meshtastic", lambda: mesh)
    return built


@pytest.fixture
def mgr():
    """A standalone manager whose supervisor thread is always stopped afterwards."""
    m = connection.ConnectionManager()
    yield m
    m._want_connected = False
    m._stop.set()
    if m._supervisor:
        m._supervisor.join(timeout=2)


# ----------------------------------------------------------------------
# connect / status
# ----------------------------------------------------------------------


def test_connect_opens_interface_and_reports_status(mgr, fake_radio):
    status = mgr.connect("10.0.0.7", 4403)
    assert status == {
        "connected": True,
        "host": "10.0.0.7",
        "node_id": "!aabbccdd",
        "true_node_id": "!aabbccdd",
        "node_num": 0xAABBCCDD,
        "short_name": "MESH",
        "long_name": "Meshy Gateway",
    }
    assert len(fake_radio) == 1
    assert fake_radio[0].host == "10.0.0.7"


def test_connect_subscribes_the_observer_once_per_open(mgr, fake_radio):
    from pubsub import pub

    mgr.connect("10.0.0.7")
    topic = pub.getDefaultTopicMgr().getTopic("meshtastic.receive")
    obs = mgr._observer
    assert topic.hasListener(obs.on_receive)

    # Reconnecting must not accumulate duplicate handlers — a duplicate made
    # "connection lost" fire N times in the field.
    mgr.connect("10.0.0.7")
    assert len(topic.getListeners()) == 1


def test_connect_failure_is_not_fatal_and_starts_the_supervisor(mgr, monkeypatch):
    def boom():
        raise OSError("Connection refused")

    monkeypatch.setattr(connection, "_import_meshtastic", boom)
    status = mgr.connect("unreachable.example")
    # Reports disconnected but does NOT raise: the supervisor retries.
    assert status["connected"] is False
    assert status["host"] == "unreachable.example"
    assert mgr._want_connected is True
    assert mgr._supervisor is not None and mgr._supervisor.is_alive()


def test_connect_reraises_when_the_radio_library_is_missing(mgr, monkeypatch):
    """No point supervising a retry when the package itself isn't installed."""

    def boom():
        raise connection.MeshtasticUnavailable("not installed")

    monkeypatch.setattr(connection, "_import_meshtastic", boom)
    with pytest.raises(connection.MeshtasticUnavailable):
        mgr.connect("10.0.0.7")


def test_status_when_never_connected(mgr):
    assert mgr.status() == {
        "connected": False,
        "host": None,
        "node_id": None,
        "true_node_id": None,
        "node_num": None,
        "short_name": None,
        "long_name": None,
    }


def test_my_node_id_without_myinfo(mgr, fake_radio):
    mgr.connect("10.0.0.7")
    mgr._iface.myInfo = None
    assert mgr.my_node_id() is None


def test_local_node_identity_tolerates_missing_names(mgr, fake_radio):
    mgr.connect("10.0.0.7")
    mgr._iface.nodes = {}
    assert mgr.local_node_identity() == {
        "node_id": "!aabbccdd",
        "true_node_id": "!aabbccdd",
        "node_num": 0xAABBCCDD,
        "short_name": None,
        "long_name": None,
    }


def test_iface_property_raises_when_disconnected(mgr):
    with pytest.raises(RuntimeError, match="Not connected"):
        _ = mgr.iface


def test_iface_property_returns_the_live_interface(mgr, fake_radio):
    mgr.connect("10.0.0.7")
    assert mgr.iface is fake_radio[0]


# ----------------------------------------------------------------------
# disconnect
# ----------------------------------------------------------------------


def test_disconnect_closes_and_stops_supervising(mgr, fake_radio):
    mgr.connect("10.0.0.7")
    assert mgr.disconnect() == {"connected": False}
    assert fake_radio[0].closed is True
    assert mgr.is_connected() is False
    assert mgr._want_connected is False
    assert mgr._stop.is_set()


def test_disconnect_preserves_the_reconnect_target(mgr, fake_radio):
    """Regression: _close_locked once nulled _host, so _open() dialed None forever."""
    mgr.connect("10.0.0.7", 4444)
    mgr.disconnect()
    assert mgr._host == "10.0.0.7"
    assert mgr._port == 4444


def test_close_survives_an_interface_that_raises_on_close(mgr, fake_radio):
    mgr.connect("10.0.0.7")

    def angry():
        raise OSError("already dead")

    mgr._iface.close = angry
    mgr.disconnect()  # must not raise
    assert mgr.is_connected() is False


# ----------------------------------------------------------------------
# connection.lost handling
# ----------------------------------------------------------------------


def test_connection_lost_marks_disconnected_and_stashes_the_iface(mgr, fake_radio):
    mgr.connect("10.0.0.7")
    dead = mgr._iface
    mgr._on_connection_lost(interface=dead)
    assert mgr.is_connected() is False
    assert dead in mgr._stale_ifaces


def test_connection_lost_from_a_stale_interface_is_ignored(mgr, fake_radio):
    """Regression: a late loss event from an OLD iface nulled the healthy NEW one."""
    mgr.connect("10.0.0.7")
    old = mgr._iface
    mgr.connect("10.0.0.7")  # reconnect — a new iface is now current
    new = mgr._iface
    assert new is not old

    mgr._on_connection_lost(interface=old)
    assert mgr.is_connected() is True  # the healthy link survived
    assert mgr._iface is new
    assert old in mgr._stale_ifaces


def test_connection_lost_without_an_interface_argument(mgr, fake_radio):
    mgr.connect("10.0.0.7")
    current = mgr._iface
    mgr._on_connection_lost(interface=None)
    assert mgr.is_connected() is False
    assert current in mgr._stale_ifaces


def test_stale_interfaces_are_closed_on_the_next_close(mgr, fake_radio):
    """Unclosed stale ifaces keep heartbeat timers writing to dead sockets."""
    mgr.connect("10.0.0.7")
    dead = mgr._iface
    mgr._on_connection_lost(interface=dead)
    assert dead.closed is False  # not closed on the reader thread, deliberately

    mgr.disconnect()
    assert dead.closed is True
    assert mgr._stale_ifaces == []


# ----------------------------------------------------------------------
# supervisor
# ----------------------------------------------------------------------


def test_supervisor_reopens_a_dropped_link(mgr, fake_radio):
    mgr.connect("10.0.0.7")
    mgr._on_connection_lost(interface=mgr._iface)
    assert mgr.is_connected() is False

    deadline = threading.Event()
    for _ in range(100):
        if mgr.is_connected():
            break
        deadline.wait(0.05)
    assert mgr.is_connected() is True, "supervisor did not reconnect"
    assert len(fake_radio) >= 2


def test_ensure_supervisor_does_not_start_a_second_thread(mgr, fake_radio):
    mgr.connect("10.0.0.7")
    first = mgr._supervisor
    mgr._ensure_supervisor()
    assert mgr._supervisor is first


def test_supervise_exits_promptly_when_no_longer_wanted(mgr):
    mgr._want_connected = False
    mgr._supervise()  # returns immediately rather than looping


def test_supervise_backs_off_after_a_failed_reopen(mgr, monkeypatch):
    """A failing _open() must not spin: it waits, then the stop flag ends the loop."""
    attempts = []

    def boom():
        attempts.append(1)
        mgr._want_connected = False  # end the loop after one attempt
        raise OSError("refused")

    monkeypatch.setattr(mgr, "_open", boom)
    mgr._want_connected = True
    mgr._supervise()
    assert attempts == [1]


# ----------------------------------------------------------------------
# module-level singletons
# ----------------------------------------------------------------------


def test_get_manager_is_a_process_wide_singleton():
    assert connection.get_manager() is connection.get_manager()


def test_shared_state_is_created_once():
    st1 = connection._shared_state()
    st2 = connection._shared_state()
    assert st1 is st2
    assert sys.modules[connection._SHARED_KEY] is st1


def test_import_meshtastic_returns_the_real_package():
    """meshtastic is a hard dependency, so the happy path must work."""
    mesh = connection._import_meshtastic()
    assert hasattr(mesh, "tcp_interface")


def test_enable_debug_logging_is_off_for_falsey_values(monkeypatch):
    for value in ("", "0", "false", "no", "off", "  "):
        monkeypatch.setenv("MESHTASTIC_DEBUG", value)
        assert connection.enable_debug_logging() is False


def test_debug_filter_defers_warnings_to_an_existing_root_handler(monkeypatch):
    """With a gateway root handler present, WARNING+ must not be emitted twice."""
    import logging

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        root.handlers = [h for h in root.handlers if not getattr(h, "_mesh_debug", False)]
        existing = logging.StreamHandler()
        existing.setLevel(logging.WARNING)
        root.addHandler(existing)

        monkeypatch.setenv("MESHTASTIC_DEBUG", "1")
        assert connection.enable_debug_logging() is True
        h = next(h for h in root.handlers if getattr(h, "_mesh_debug", False))

        warn = logging.LogRecord(
            "meshtastic_hermes.tools", logging.WARNING, "", 0, "m", None, None
        )
        info = logging.LogRecord("meshtastic_hermes.tools", logging.INFO, "", 0, "m", None, None)
        assert not h.filter(warn)  # left to the pre-existing handler
        assert h.filter(info)      # below WARNING, ours to emit
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
