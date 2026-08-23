# Changelog

All notable changes to this project are recorded here.

The version is bumped by automation, not by hand — see
`.github/workflows/version-bump.yml` (automatic patch bump on every merged
PR) and `.github/workflows/release.yml` (manual minor release). Both call
`scripts/bump_version.py`, which is the only thing that rewrites a version.

Patch bumps insert a stub section here automatically. Replace the stub with
real prose before cutting a minor release: the release workflow publishes
whatever section matches the new version as the GitHub Release notes.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.10] - 2026-08-23

### Changed

- Patch version bump.

## [0.1.9] - 2026-08-23

### Changed

- Patch version bump.

## [0.1.8] - 2026-08-23

### Added

- **Mention gating on channels, on by default.** A channel message is answered
  only when it starts with this node's short name, long name, or node id
  (case-insensitive, optional leading `@`, node id with or without `!`, optional
  trailing `:`/`,`). A mention mid-sentence does not count, and the mention is
  stripped before the agent sees the text. Direct messages are always answered
  and never need one. Disable with `MESHTASTIC_REQUIRE_MENTION=false`; a loud
  warning is logged when gating is off *and* the reply scope is broad, because
  that combination permits an unbounded bot-to-bot transmission loop.
- **Three-state connection status.** `status()` reports a new `state` field of
  `connecting` / `connected` / `disconnected`. Soft failures (connection
  refused, timeout, DNS) stay `connecting` — a booting node refusing TCP is
  expected. After `MESHTASTIC_FAILURE_THRESHOLD` (default 10) consecutive
  failures the state becomes `disconnected` while retrying continues at
  `MESHTASTIC_SLOW_RETRY_SECONDS` (default 300), so the link still self-heals.
- **A live integration test rig** (`testrig/`, `scripts/testrig.sh`) that runs
  against a real Hermes install over SSH, read-only and zero-airtime by
  default. See `docs/testing.md`.

### Changed

- **Reply channels are configured by name, not index.**
  `MESHTASTIC_REPLY_CHANNELS="in.secure"` resolves against the radio's channel
  table on every connect, so renaming or reordering channels moves the
  allowlist with the name. Numeric indices still work but now warn: an index is
  a radio *slot*, not a channel identity, so reordering can silently repoint the
  bot and broadcast a private reply on the wrong channel. An unknown name warns
  and is skipped; the rest of the allowlist still applies.
- The `connected` boolean is retained for backwards compatibility and is true
  only in the `connected` state.
- Documentation reorganized around the three gates that jointly decide whether
  the bot replies — channel allowlist, mention gating, and Hermes' sender
  allowlist — with a complete environment-variable reference.

### Fixed

- `MeshtasticAdapter.connect()` now accepts the keyword-only `is_reconnect`
  argument that the real Hermes gateway passes. The previous signature was
  stale against upstream and raised `TypeError` on connect.
- A false "MESHTASTIC_HOST is unset" setup banner. The platform registered no
  `setup_fn`, so Hermes fell back to a static hint that printed unconditionally
  without checking whether the variable was set. The banner also printed twice
  because both `plugin.yaml` manifests declared `MESHTASTIC_HOST` in
  `requires_env`; the tools manifest now uses `optional_env`.

### Removed

- CI no longer runs the Hermes plugin security scanner. It was abandoned as
  not useful — it is regex matching over raw file text, and satisfying it meant
  changing byte sequences rather than behavior. `docs/security-scanner.md` is
  retained for reference only and nothing gates on its verdict.

## [0.1.7] - 2026-08-23

### Changed

- Patch version bump.

## [0.1.6] - 2026-08-23

### Changed

- Patch version bump.

## [0.1.5] - 2026-08-23

### Changed

- Patch version bump.

## [0.1.4] - 2026-08-23

### Changed

- Patch version bump.

## [0.1.3] - 2026-08-23

### Changed

- Patch version bump.

## [0.1.2] - 2026-08-23

### Changed

- Patch version bump.

## [0.1.1] - 2026-08-23

### Changed

- Patch version bump.

## [0.1.0] - 2026-08-23

Baseline for the `antitree` fork of `thpham/meshtastic-hermes-plugin`. The
fork continues upstream's `0.1.x` line rather than restarting at `1.0.0`;
the first automatic bump takes it to `0.1.1`.

### Added

- Meshtastic tools plugin (`meshtastic_hermes`): connect/disconnect, send
  text, recent messages, node and channel listing, device metrics, and a
  SQLite knowledge base of observed traffic metadata.
- Meshtastic platform adapter (`meshtastic_platform`): bidirectional
  gateway relaying inbound mesh text to the agent and replies back over
  the radio.
- CI (`.github/workflows/tests.yml`): test matrix across Python 3.10–3.13,
  manifest verification, version-consistency check, and packaging checks.
- Release automation: automatic patch bump on merged PRs, manual minor
  release with GitHub Release notes drawn from this file.
