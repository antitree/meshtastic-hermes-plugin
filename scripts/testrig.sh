#!/usr/bin/env bash
# Run the live integration test rig against the host in .testrig.env.
#
# Default run is READ-ONLY and ZERO-AIRTIME: it opens no radio connection and
# transmits nothing. See docs/testing.md.
#
#   ./scripts/testrig.sh              # default: zero airtime
#   ./scripts/testrig.sh --transmit   # opt in to the transmit check
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -f .testrig.env ]]; then
  cat >&2 <<'EOF'
error: .testrig.env not found.

The test rig needs host-specific settings that are deliberately kept out of git.
Copy the tracked example and fill it in:

    cp .testrig.env.example .testrig.env
    $EDITOR .testrig.env

.testrig.env is gitignored and must stay that way.
EOF
  exit 2
fi

PY="${TESTRIG_PYTHON:-}"
if [[ -z "$PY" ]]; then
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
  done
fi
if [[ -z "$PY" ]]; then
  echo "error: no python3 on PATH (set TESTRIG_PYTHON)" >&2
  exit 2
fi

exec "$PY" -m testrig.runner "$@"
