# Testing

Two layers, with different jobs.

## Unit suite

```
pytest
```

Fast, hermetic, runs in CI. It fakes the radio and **hand-copies a stub of
Hermes' `BasePlatformAdapter`**. That stub is the layer's blind spot: it cannot
notice when the real base class upstream changes. A stale `connect()` signature
shipped for months because only the stub ever checked it.

## Live test rig

The rig closes that gap. It SSHes to a host running a **real** Hermes install
with a **real** radio attached, and runs the checks that the faked suite
structurally cannot.

```
cp .testrig.env.example .testrig.env
$EDITOR .testrig.env
./scripts/testrig.sh
```

The default run is **read-only and zero-airtime**. It opens no radio connection
and transmits nothing.

### Safety rules

These are design constraints, not preferences. The rig enforces them.

- **Never a second TCP connection to the node.** A Meshtastic node accepts
  exactly one TCP client at a time, and the user's gateway service holds that
  slot. A rig that dialed the node would knock the running bot offline. The rig
  therefore opens no radio connection and never calls `hermes meshtastic status`
  (that command *does* connect). All live radio facts are read from state files
  and logs the **running gateway** already publishes.
- **Never restart, stop, or reconfigure the gateway.** There is no flag that
  does this today. `TESTRIG_SERVICE` is recorded for diagnostics only.
- **Never write into the real Hermes profile or plugin directory.** The rig syncs
  to a throwaway scratch dir and deletes it afterwards. Because it deletes that
  directory, `assert_safe_remote_dir` *refuses* a `TESTRIG_REMOTE_DIR` that
  points inside `~/.hermes/profiles`, `~/.hermes/plugins`, or
  `~/.hermes/hermes-agent`.
- **Zero airtime by default.** Transmitting is opt-in behind `--transmit`. This
  codebase has **no rate limiting or cooldown**, so unattended transmit is
  genuinely risky on a regulated medium.
- **All output is scrubbed.** Node ids, node short/long names, hostnames and IPs
  are redacted from everything the rig prints, so rig output cannot leak homelab
  details into a commit, PR, or issue.

### Configuration

All host-specific values live in `.testrig.env`, which is **gitignored**. The
tracked `.testrig.env.example` carries placeholders only. This split is the
entire mechanism keeping homelab details out of the repository — keep it that
way, and never commit `.testrig.env`.

| Key | Meaning |
| --- | --- |
| `TESTRIG_HOST` | Host running Hermes + the gateway. Needs passwordless SSH. |
| `TESTRIG_USER` | SSH user. Blank falls back to `~/.ssh/config`. |
| `TESTRIG_PROFILE` | Hermes profile whose gateway owns the radio. |
| `TESTRIG_TEST_CHANNEL` | Dedicated channel for `--transmit`. Never a real one. |
| `TESTRIG_REMOTE_DIR` | Throwaway scratch dir. Synced, then deleted. |
| `TESTRIG_HERMES_HOME` | Profile home. Default `~/.hermes/profiles/<profile>`. |
| `TESTRIG_GATEWAY_LOG` | Gateway log read for live evidence. |
| `TESTRIG_HERMES_PYTHON` | Interpreter of the real Hermes install. |
| `TESTRIG_SERVICE` | Diagnostics only; the rig never touches the service. |

### Flags

| Flag | Effect |
| --- | --- |
| *(none)* | Read-only, zero-airtime run. |
| `--transmit` | Enables the transmit check. See its status below. |
| `--keep-scratch` | Leaves the remote scratch dir for debugging. |
| `--no-scrub` | Prints raw output. **Never** use when pasting anywhere. |

Exit code is `1` if any check FAILs, else `0`. `SKIP` and `NOT_IMPLEMENTED` do
not fail the run — they are reported honestly rather than counted as passes.

### What the checks cover

| Check | Status | What it proves |
| --- | --- | --- |
| `base_contract` | **live** | The adapter satisfies the **real** `BasePlatformAdapter`: it subclasses it, has no unimplemented abstract methods, and every overridden method accepts what the gateway will pass. This is the check that catches `connect(is_reconnect=…)` drift. |
| `registration` | **live** | Both plugins import and `register()` cleanly under the real Hermes runtime; tools/skills register and the platform registration carries `check_fn` and `setup_fn`. |
| `setup_banner` | **live** | A `setup_fn` is registered (which is what suppresses Hermes' static "MESHTASTIC_HOST is unset" fallback banner), and the real `hermes_cli.config.get_env_value` resolves the host on the live profile. |
| `channel_resolution` | **live** | The configured reply channels — read through the adapter's own env parser against the live profile — are present on the real radio and resolve **by name** to indices. |
| `receive_path` | **live** | Real inbound packets were decoded and routed by the running gateway, with normalized chat ids. Evidence comes from the gateway's log; zero airtime. |
| `mention_gating` | **currently ineffective** | Intended to verify gating against the node's real short name, long name and node id. It does not do so today — see below. |
| `transmit` | **not implemented** | See below. |

#### Why `base_contract` matters most

It is the only check that compares this repo against the *actual* upstream class
rather than the vendored stub. It is also verified to be falsifiable:
reintroducing the historical `async def connect(self)` signature makes it fail
with an explicit diff:

```
[FAIL           ] base_contract
    adapter does not match the real base-class contract:
        connect:
          base: (self, *, is_reconnect: bool = False) -> bool
          impl: (self) -> 'bool'
```

#### Why `mention_gating` reports `NOT_IMPLEMENTED`

Not because the feature is missing — mention gating **is** implemented, and is on by
default (see [usage](usage.md#gate-2--mention-gating-channels-only)). The probe simply
looks for the wrong function.

`check_mention_gating` searches `meshtastic_hermes.gateway_bridge` for a helper named
`mentions_us`, `is_mention`, `should_reply_to_channel`, or `_mentions_us`, and reports
`NOT_IMPLEMENTED` when it finds none. None of those names has ever existed. The real
entry points are `match_mention(text, *, short_name, long_name, node_id)` — which
returns the text with the mention stripped, or `None` — and `apply_mention_gate`, which
applies it to a whole inbound message. The probe also calls its candidate as
`gate(text, identity)`, which matches neither signature.

So the check is a false negative: it says "nothing to verify" about a feature that is
live. Until the probe is updated to call `match_mention`, treat a `NOT_IMPLEMENTED` here
as "unverified", not as "absent". The unit suite does cover the logic
(`tests/test_mention_gating.py`, `tests/test_mention_gating_adapter.py`); what is missing
is the check against the node's *real* names on live hardware.

#### Why `transmit` is not implemented

Sending would require one of two things, and both are ruled out:

1. A second TCP connection to the node — forbidden, the gateway owns the single
   slot.
2. Driving the user's live gateway to emit on a real channel — unsafe in a
   codebase with no rate limiting.

`--transmit` therefore currently reports `NOT_IMPLEMENTED` rather than sending.
Implementing it properly needs a dedicated test channel on the radio *and* a
cooldown in the plugin first.

### What is NOT covered

- **Transmit / end-to-end round trip.** Nothing is ever sent. A reply's content
  is never verified on air.
- **Mention gating against the node's real names.** The feature is implemented and
  unit-tested, but the rig's probe looks for helper names that do not exist, so it
  reports `NOT_IMPLEMENTED` rather than checking anything (see above).
- **Radio-level behavior**: signal quality, retries, ACKs, mesh routing,
  multi-hop delivery, encryption correctness on the wire.
- **Channel *indices* on the radio's own channel table.** The rig resolves names
  against the gateway's published channel directory, not by reading the radio's
  channel table directly — that would need a TCP connection.
- **Long-running behavior**: reconnect storms, connection-drop recovery, leaked
  threads. These need a soak test, not a point-in-time probe.
- **Anything on a host other than the configured one.** A version mismatch
  between the local reference checkout of Hermes and the one on the live host is
  itself worth investigating; the live host is the source of truth.

### Rig code is itself unit-tested

The parts that run locally are covered by the normal suite:
`tests/test_testrig_scrub.py` (redaction of node ids, IPs, hostnames, and the
longest-match rule that stops name tails leaking),
`tests/test_testrig_config.py` (config parsing, placeholder rejection, and the
scratch-dir safety guard), and `tests/test_testrig_signature.py` (the
signature-compatibility rule, including the historical `connect()` bug).
