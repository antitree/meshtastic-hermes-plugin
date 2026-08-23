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

## [0.1.8] - 2026-08-23

### Changed

- Patch version bump.

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
