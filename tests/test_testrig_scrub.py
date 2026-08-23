"""Tests for the rig's output scrubber.

The scrubber is the control that keeps homelab details out of commits, PRs and
issues, so these tests assert on the *absence* of the original identifiers rather
than only on the presence of a placeholder — a substitution that leaves a tail
behind would otherwise pass.
"""

from __future__ import annotations

import pytest

from testrig.scrub import HOSTNAME, IPV4, NODE_ID, SECRET, Scrubber


def test_redacts_node_ids():
    out = Scrubber().scrub("inbound from !deadbeef on ch:1")
    assert "!deadbeef" not in out
    assert NODE_ID in out
    assert "ch:1" in out  # structural detail survives


@pytest.mark.parametrize("node_id", ["!deadbeef", "!CAFEF00D", "!00000000", "!ffffffff"])
def test_redacts_node_ids_any_case(node_id):
    assert node_id not in Scrubber().scrub(f"node {node_id} seen")


def test_does_not_redact_near_miss_node_ids():
    """Only exactly-8 hex digits is a node id; do not chew adjacent text."""
    text = "!abc and !deadbeefff and !zzzzzzzz"
    out = Scrubber().scrub(text)
    assert out == text


def test_redacts_ipv4():
    out = Scrubber().scrub("MESHTASTIC_HOST=192.0.2.10 port 4403")
    assert "192.0.2.10" not in out
    assert IPV4 in out
    assert "4403" in out  # a bare port is not an address


def test_redacts_hostname():
    out = Scrubber().scrub("connecting to rig-host.example.com now")
    assert "rig-host.example.com" not in out
    assert HOSTNAME in out


def test_keeps_public_module_paths_readable():
    text = "gateway.platforms.base and meshtastic_platform.adapter registered"
    assert Scrubber().scrub(text) == text


def test_keeps_filenames_readable():
    text = "read gateway_state.json and adapter.py and gateway.log"
    assert Scrubber().scrub(text) == text


def test_redacts_configured_secrets():
    scrubber = Scrubber(["rig-host.example.com", "testprof", "chan.example"])
    out = scrubber.scrub("profile testprof on rig-host.example.com channel chan.example")
    assert "testprof" not in out
    assert "rig-host.example.com" not in out
    assert SECRET in out


def test_longest_secret_wins_so_no_tail_leaks():
    """A short name contained in a long name must not leave the remainder behind."""
    # "NODE" is a prefix of "NODEBOX". Matching the short one first would leave
    # the "BOX" tail behind and leak it.
    scrubber = Scrubber(["NODE", "NODEBOX"])
    out = scrubber.scrub("shortname NODEBOX here")
    assert "NODEBOX" not in out
    assert "BOX" not in out.replace("<redacted>", "")


def test_secrets_are_case_insensitive():
    out = Scrubber(["NODE"]).scrub("shortname node reported")
    assert "node" not in out.lower().replace("<redacted>", "")


def test_ignores_too_short_secrets():
    """A 1-2 char 'secret' would shred unrelated text, so it is ignored."""
    text = "a b on the channel"
    assert Scrubber(["a", "b", ""]).scrub(text) == text


def test_add_learns_identity_at_runtime():
    scrubber = Scrubber()
    scrubber.add("Example LongName", None, "NODE")
    out = scrubber.scrub("long=Example LongName short=NODE")
    assert "LongName" not in out
    assert "NODE" not in out.replace("<redacted>", "")


def test_scrub_handles_empty():
    assert Scrubber().scrub("") == ""


def test_real_gateway_log_line_is_fully_scrubbed():
    """End-to-end on the exact shape of line the rig reads from a live gateway."""
    line = (
        "2026-08-23 16:48:53 INFO gateway.run: inbound message: platform=meshtastic "
        "user=!deadbeef chat=ch:1 msg='hello' host=rig-host.example.com ip=192.0.2.10"
    )
    out = Scrubber(["rig-host.example.com", "testprof"]).scrub(line)
    for leak in ("!deadbeef", "rig-host.example.com", "192.0.2.10"):
        assert leak not in out
    assert "gateway.run" in out
    assert "platform=meshtastic" in out
