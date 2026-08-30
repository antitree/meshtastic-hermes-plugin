"""Tool-send transmit policy — security remediation item 1.

`meshtastic_send_text` used to accept arbitrary `text`/`channel_index`/`dest_id`
and hand them straight to `iface.sendData`, with `channel_index` silently
defaulting to `0` — the public Primary channel. A tool call (or a prompt-injected
one) could therefore broadcast in the clear to every radio in range.

The core regression these tests exist to hold is the negative one: when policy
rejects a send, `iface.sendData` is NEVER called. Every rejection test asserts on
the fake interface, not merely on the returned JSON.

No radio: a fake interface is injected into the process-wide ConnectionManager.
"""

from __future__ import annotations

import json
import types

import pytest

from meshtastic_hermes import connection, policy, tools
from meshtastic_hermes import gateway_bridge as gb

TOOL_SEND_ENV = (
    "MESHTASTIC_TOOL_SEND_CHANNELS",
    # Removed in 0.2.0, still cleared: a stale value in the developer's own shell
    # must not change any outcome here, which is itself part of the contract.
    "MESHTASTIC_TOOL_SEND_ALLOW_PRIMARY",
    "MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test states its own policy explicitly; nothing leaks in from outside.

    This also covers the reply-policy vars, so a test cannot accidentally pass
    because reply configuration authorized a tool send (it must not).
    """
    for var in (*TOOL_SEND_ENV, "MESHTASTIC_REPLY_CHANNELS", "MESHTASTIC_REPLY_ALL"):
        monkeypatch.delenv(var, raising=False)


def _chan(index_role_name):
    role, name = index_role_name
    return types.SimpleNamespace(role=role, settings=types.SimpleNamespace(name=name))


# index 0 = unnamed PRIMARY (public), 1 = "in.secure", 2 = "ops"
TABLE = [
    {"index": 0, "name": "", "role": 1},
    {"index": 1, "name": "in.secure", "role": 2},
    {"index": 2, "name": "ops", "role": 2},
]


class _FakeIface:
    """Records every sendData call so a rejected send is provably silent."""

    def __init__(self):
        self.sent: list = []
        self.nodes = {}
        self.myInfo = types.SimpleNamespace(my_node_num=0xAABBCCDD)
        self.localNode = types.SimpleNamespace(
            channels=[_chan((1, "")), _chan((2, "in.secure")), _chan((2, "ops"))]
        )

    def sendData(self, payload, **kw):
        self.sent.append((payload, kw))
        cb = kw.get("onResponse")
        if cb is not None:
            cb({"fromId": kw.get("destinationId"), "decoded": {"routing": {"errorReason": "NONE"}}})


@pytest.fixture
def iface():
    fake = _FakeIface()
    connection.get_manager()._iface = fake
    return fake


def _data(raw):
    return json.loads(raw)


# ----------------------------------------------------------------------
# the tool handler: rejection must be silent on the wire
# ----------------------------------------------------------------------


def test_bare_send_text_is_rejected_by_default(iface):
    """The headline case: `meshtastic_send_text({"text": "hi"})` sends nothing."""
    data = _data(tools.send_text({"text": "hi"}))
    assert data["code"] == "broadcast_disabled"
    assert iface.sent == []


def test_rejected_send_never_reaches_senddata(iface):
    """THE regression: every rejection path leaves the radio untouched."""
    rejected = [
        {"text": "hi"},                                          # no destination at all
        {"text": "hi", "channel_index": 0},                      # explicit public Primary
        {"text": "hi", "channel_index": 1},                      # broadcast, not enabled
        {"text": "hi", "channel_name": "in.secure"},             # named, not enabled
        {"text": "hi", "dest_id": "!11112222"},                  # DM without pki
        {"text": "hi", "pki": True},                             # pki without dest_id
        {"text": "hi", "dest_id": "!11112222", "pki": False},    # explicit plaintext DM
    ]
    for args in rejected:
        data = _data(tools.send_text(args))
        assert "error" in data, args
        assert "code" in data, args
    assert iface.sent == [], "policy rejected the send but something still transmitted"


def test_send_text_does_not_default_the_channel_to_zero(iface, monkeypatch):
    """No implicit channel 0. Broadcasting must be an explicit, named decision."""
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "in.secure")
    data = _data(tools.send_text({"text": "hi"}))
    # Even with broadcasting fully enabled, an unspecified channel is an error,
    # not a fallback to channel 0.
    assert data["code"] == "channel_required"
    assert iface.sent == []


def test_pki_dm_still_works(iface):
    data = _data(
        tools.send_text({"text": "secret", "dest_id": "!11112222", "pki": True, "wait_ack": False})
    )
    assert data["sent"] is True
    assert data["encryption"] == "pki"
    payload, kw = iface.sent[0]
    assert payload == b"secret"
    assert kw["destinationId"] == "!11112222"
    assert kw["pkiEncrypted"] is True


def test_the_channel_allowlist_is_the_whole_broadcast_permission(iface, monkeypatch):
    """One variable. Naming a channel is what authorizes sending to it.

    This is the regression for the consolidation: there used to be a second switch
    that also had to be on, so the common misconfiguration was an allowlist that
    named the right channel and still refused every send.
    """
    # Nothing configured -> DM-only, broadcasts refused.
    data = _data(tools.send_text({"text": "hi", "channel_name": "in.secure"}))
    assert data["code"] == "broadcast_disabled"
    assert iface.sent == []

    # Naming the channel is the ONLY thing needed.
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "in.secure")
    data = _data(tools.send_text({"text": "hi", "channel_name": "in.secure"}))
    assert data["sent"] is True
    assert iface.sent[0][1]["channelIndex"] == 1


def test_removed_broadcast_switch_neither_enables_nor_blocks(iface, monkeypatch):
    """A stale MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST is inert, not authoritative.

    Both directions matter: on its own it must not open broadcasting (that would be
    the old sharp edge surviving), and set alongside a real allowlist it must not
    block one either.
    """
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST", "true")
    data = _data(tools.send_text({"text": "hi", "channel_name": "in.secure"}))
    assert data["code"] == "broadcast_disabled"
    assert iface.sent == []

    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "in.secure")
    data = _data(tools.send_text({"text": "hi", "channel_name": "in.secure"}))
    assert data["sent"] is True


def test_broadcast_requires_the_channel_to_be_allowlisted(iface, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "in.secure")
    data = _data(tools.send_text({"text": "hi", "channel_name": "ops"}))
    assert data["code"] == "channel_not_allowed"
    assert iface.sent == []


def test_internal_sidecar_allowlist_does_not_widen_normal_tool_policy(iface, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "in.secure")
    data = _data(tools.send_text(
        {"text": "meshagatchi reply", "channel_name": "LongFast"},
        channel_spec="LongFast",
    ))
    assert "error" not in data
    assert data["channel_index"] == 0
    assert len(iface.sent) == 1


def test_primary_is_authorized_by_naming_it(iface, monkeypatch):
    """Naming Primary is the Primary opt-in — no separate flag involved."""
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "Primary")
    data = _data(tools.send_text({"text": "hi", "channel_index": 0}))
    assert data["sent"] is True
    assert iface.sent[0][1]["channelIndex"] == 0


def test_wildcard_does_not_cover_primary(iface, monkeypatch):
    """`all` means every channel the operator set up — not the public one.

    This is the carve-out that replaces the old MESHTASTIC_TOOL_SEND_ALLOW_PRIMARY
    flag: a wildcard must never be a way to transmit in the clear by accident.
    """
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "all")

    # Private channels: allowed by the wildcard.
    data = _data(tools.send_text({"text": "hi", "channel_name": "in.secure"}))
    assert data["sent"] is True

    # Primary: refused, despite the wildcard.
    data = _data(tools.send_text({"text": "hi", "channel_index": 0}))
    assert data["code"] == "primary_not_allowed"
    assert len(iface.sent) == 1, "the wildcard let a send onto the public Primary"


def test_primary_named_alongside_a_wildcard_is_allowed(iface, monkeypatch):
    """`all,Primary` is an explicit naming, so it opens Primary too."""
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "all,Primary")
    data = _data(tools.send_text({"text": "hi", "channel_index": 0}))
    assert data["sent"] is True


def test_allowing_a_private_channel_does_not_allow_primary(iface, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "in.secure")
    data = _data(tools.send_text({"text": "hi", "channel_index": 0}))
    # Primary is not on the allowlist at all, so it fails before the Primary flag.
    assert data["code"] == "channel_not_allowed"
    assert iface.sent == []


def test_reply_policy_does_not_authorize_tool_sends(iface, monkeypatch):
    """Being allowed to REPLY on a channel is not permission to originate there."""
    monkeypatch.setenv("MESHTASTIC_REPLY_CHANNELS", "in.secure")
    monkeypatch.setenv("MESHTASTIC_REPLY_ALL", "true")
    data = _data(tools.send_text({"text": "hi", "channel_name": "in.secure"}))
    assert data["code"] == "broadcast_disabled"
    assert iface.sent == []


def test_names_resolve_against_the_radio_channel_table(iface, monkeypatch):
    """The allowlist NAME is matched to an index from the radio, not guessed."""
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "ops")
    data = _data(tools.send_text({"text": "hi", "channel_name": "ops"}))
    assert data["sent"] is True
    assert data["channel_index"] == 2  # "ops" lives at index 2 on this radio

    # Move "ops" to a different slot: the name follows the channel, the index does not.
    iface.localNode.channels = [
        _chan((1, "")),
        _chan((2, "ops")),
        _chan((2, "in.secure")),
    ]
    iface.sent.clear()
    data = _data(tools.send_text({"text": "hi", "channel_name": "ops"}))
    assert data["channel_index"] == 1


def test_unknown_channel_name_is_refused(iface, monkeypatch):
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "in.secure")
    data = _data(tools.send_text({"text": "hi", "channel_name": "nope"}))
    assert data["code"] == "unknown_channel"
    assert iface.sent == []


def test_empty_text_is_still_rejected(iface):
    assert "error" in _data(tools.send_text({"text": "   "}))
    assert iface.sent == []


# ----------------------------------------------------------------------
# integration: through the REGISTERED tool handler, not the module function
# ----------------------------------------------------------------------


def test_registered_handler_enforces_the_policy(iface, monkeypatch):
    """The handler Hermes actually calls must enforce this, not just policy.py.

    A correct-but-unwired helper is exactly the failure this rules out, so this
    goes through `build_registry()` and pulls the handler out of the registry the
    way the agent's tool loop would.
    """
    from meshtastic_hermes.__main__ import build_registry

    ctx = build_registry()
    entry = ctx.tools["meshtastic_send_text"]
    handler = entry["handler"]
    connection.get_manager()._iface = iface  # build_registry() may reset the manager

    # The model is told about the policy, so it does not assume free sends.
    description = entry["schema"]["description"]
    assert "MESHTASTIC_TOOL_SEND_CHANNELS" in description
    assert "Primary" in description  # the wildcard carve-out is stated to the model

    data = _data(handler({"text": "hi"}))
    assert data["code"] == "broadcast_disabled"
    assert iface.sent == []

    # ...and the same registered handler still sends an authorized PKI DM.
    data = _data(handler({"text": "hi", "dest_id": "!11112222", "pki": True, "wait_ack": False}))
    assert data["sent"] is True
    assert iface.sent[0][1]["pkiEncrypted"] is True


# ----------------------------------------------------------------------
# the pure helper
# ----------------------------------------------------------------------


def test_validate_tool_send_default_denies_broadcast():
    with pytest.raises(policy.ToolSendRejected) as exc:
        policy.validate_tool_send({"text": "hi"}, TABLE)
    assert exc.value.code == "broadcast_disabled"


def test_validate_tool_send_allows_pki_dm():
    target = policy.validate_tool_send({"dest_id": "!11112222", "pki": True}, TABLE)
    assert target.dest_id == "!11112222"
    assert target.pki is True
    assert target.is_dm is True


def test_validate_tool_send_rejects_plaintext_dm():
    with pytest.raises(policy.ToolSendRejected) as exc:
        policy.validate_tool_send({"dest_id": "!11112222"}, TABLE)
    assert exc.value.code == "dm_requires_pki"


def test_validate_tool_send_rejects_pki_without_dest():
    with pytest.raises(policy.ToolSendRejected) as exc:
        policy.validate_tool_send({"pki": True}, TABLE)
    assert exc.value.code == "pki_requires_dest"


def test_validate_tool_send_rejects_bogus_channel_index(monkeypatch):
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "in.secure")
    for bad in ("abc", -1, None.__class__, True):
        with pytest.raises(policy.ToolSendRejected) as exc:
            policy.validate_tool_send({"channel_index": bad}, TABLE)
        assert exc.value.code in {"invalid_channel", "channel_required"}


def test_numeric_tool_send_channels_warn_like_reply_channels(monkeypatch, caplog):
    """Numeric entries work but warn — an index is a SLOT, not a channel identity.

    This mirrors the MESHTASTIC_REPLY_CHANNELS behavior deliberately.
    """
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "1")
    with caplog.at_level("WARNING", logger=policy.logger.name):
        target = policy.validate_tool_send({"channel_index": 1}, TABLE)
    assert target.channel_index == 1
    assert "MESHTASTIC_TOOL_SEND_CHANNELS" in caplog.text
    assert "SLOT" in caplog.text


def test_names_are_not_warned_about(monkeypatch, caplog):
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "in.secure")
    with caplog.at_level("WARNING", logger=policy.logger.name):
        policy.validate_tool_send({"channel_name": "in.secure"}, TABLE)
    assert "SLOT" not in caplog.text


def test_broadcast_permission_tracks_the_allowlist_only(monkeypatch):
    """`allow_broadcast()` is derived from the allowlist now, not from a flag.

    Nothing else can turn it on — including the removed flags, at any spelling.
    """
    assert policy.allow_broadcast() is False

    for stale in (
        "MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST",
        "MESHTASTIC_TOOL_SEND_ALLOW_PRIMARY",
    ):
        monkeypatch.setenv(stale, "true")
    assert policy.allow_broadcast() is False, "a removed flag still grants broadcast"

    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "in.secure")
    assert policy.allow_broadcast() is True

    # Empty / whitespace is "unset", not "everything".
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "   ")
    assert policy.allow_broadcast() is False


def test_stale_removed_vars_are_warned_about(monkeypatch, caplog):
    """Silence would strand an operator whose .env still carries the old switch."""
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST", "true")
    with caplog.at_level("WARNING", logger="meshtastic_hermes.policy"):
        policy.tool_send_channel_spec()
    assert "MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST" in caplog.text
    assert "no longer used" in caplog.text
    assert "MESHTASTIC_TOOL_SEND_CHANNELS" in caplog.text


def test_allowed_tool_send_channels_uses_all_sentinel(monkeypatch):
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "all")
    assert policy.allowed_tool_send_channels(TABLE) == gb.ALL_CHANNELS


def test_all_channels_still_does_not_bypass_the_primary_flag(monkeypatch):
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "all")
    with pytest.raises(policy.ToolSendRejected) as exc:
        policy.validate_tool_send({"channel_index": 0}, TABLE)
    assert exc.value.code == "primary_not_allowed"
    # a non-primary channel is fine under "all"
    assert policy.validate_tool_send({"channel_index": 2}, TABLE).channel_index == 2


def test_primary_alias_resolves_to_the_unnamed_primary(monkeypatch):
    """"Primary"/"LongFast" target an UNNAMED primary — reusing gateway_bridge's rule."""
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "Primary")
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_ALLOW_PRIMARY", "true")
    target = policy.validate_tool_send({"channel_name": "Primary"}, TABLE)
    assert target.channel_index == 0


def test_validate_tool_send_is_pure(monkeypatch):
    """It must not transmit or mutate: safe to call before touching the radio."""
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "ops")
    table = [dict(row) for row in TABLE]
    args = {"text": "hi", "channel_name": "ops"}
    policy.validate_tool_send(args, table)
    assert table == TABLE
    assert args == {"text": "hi", "channel_name": "ops"}


def test_empty_channel_table_fails_closed(monkeypatch):
    """Disconnected (or a radio that reports nothing): names cannot resolve."""
    monkeypatch.setenv("MESHTASTIC_TOOL_SEND_CHANNELS", "in.secure")
    with pytest.raises(policy.ToolSendRejected) as exc:
        policy.validate_tool_send({"channel_name": "in.secure"}, [])
    assert exc.value.code == "unknown_channel"
