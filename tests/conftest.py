"""Shared test fixtures.

Two fixtures, and the distinction between them matters:

``_isolate_kb_path`` is session-scoped because it only sets an environment
variable — there is no per-test state to unwind.

``_reset_shared_state`` is *function*-scoped because the plugin keeps its
ConnectionManager and Observer (and therefore the whole knowledge base) in a
fixed ``sys.modules`` slot — see the ``_SHARED_KEY`` comment in connection.py.
That slot is process-global and survives everything pytest does between tests,
so without a per-test reset, one test's connection state and KB rows are still
there for the next one. Ordering then decides outcomes, and any test that
asserts on a count ("0 packets", "not connected") passes alone and fails in the
suite. Resetting per test is what makes these tests independent.
"""

import os
import sys

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_kb_path():
    """Keep the knowledge base off the real ~/.hermes during tests.

    Tests that exercise the live tools (e.g. kb_summary) would otherwise create the
    default DB under $HOME, which fails in sandboxed/read-only-HOME environments
    (Nix build, CI). Point it at in-memory SQLite for the whole session. Path-
    resolution tests still monkeypatch this var per-test and are restored after.
    """
    os.environ["MESHTASTIC_HERMES_DB"] = ":memory:"
    yield


@pytest.fixture(autouse=True)
def _reset_shared_state():
    """Drop the process-wide ConnectionManager/Observer singletons around each test.

    Also unsubscribes any pubsub listeners a test left behind: subscriptions are
    global to the pubsub library, so a leaked observer would keep receiving
    packets published by later tests and silently inflate their KB counts.
    """
    _purge()
    yield
    _purge()


def _purge():
    from meshtastic_hermes.connection import _SHARED_KEY

    st = sys.modules.get(_SHARED_KEY)
    if st is not None:
        mgr = getattr(st, "manager", None)
        if mgr is not None:
            # Stop the reconnect supervisor thread; without this a test that called
            # connect() leaves a live thread retrying for the rest of the session.
            mgr._want_connected = False
            mgr._stop.set()
        obs = getattr(st, "observer", None)
        if obs is not None:
            try:
                obs.kb.close()
            except Exception:  # noqa: BLE001, S110 - teardown must never fail a test
                pass
        del sys.modules[_SHARED_KEY]

    try:
        from pubsub import pub

        pub.unsubAll("meshtastic.receive")
        pub.unsubAll("meshtastic.connection.lost")
    except Exception:  # noqa: BLE001, S110 - teardown must never fail a test
        pass
