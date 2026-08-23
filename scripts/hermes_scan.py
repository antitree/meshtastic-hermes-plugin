#!/usr/bin/env python3
"""Run Hermes' real plugin security scanner against this repository.

Why this exists
---------------
``hermes plugins install`` scans a plugin before installing it and refuses
outright on a ``dangerous`` verdict (``--force`` does not override). There is
no ``hermes plugins scan`` subcommand, so the only way to know our verdict
before publishing was to attempt a real install. This script closes that gap
by calling the *same* code the installer calls:

    tools.plugin_guard.scan_plugin(...)  ->  ScanResult(verdict=...)
    tools.plugin_guard.should_allow_plugin_install(result)

That module is pure-stdlib (re / pathlib / hashlib / json / dataclasses). It
needs no Hermes runtime, no config.yaml, no API keys and no network at scan
time, which is what makes this viable as a CI check.

Usage
-----
    python scripts/hermes_scan.py --hermes-src /path/to/hermes-agent
    python scripts/hermes_scan.py --hermes-src ... --max-verdict caution

Exit codes: 0 = verdict at or below --max-verdict, 1 = worse, 2 = setup error.

See docs/security-scanner.md for the full analysis of what each rule matches.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ordered least->most severe. A verdict is acceptable when its index is <=
# the index of --max-verdict.
VERDICT_ORDER = ["safe", "caution", "dangerous"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hermes-src",
        required=True,
        type=Path,
        help="Path to a hermes-agent checkout (provides tools/plugin_guard.py).",
    )
    parser.add_argument(
        "--plugin-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Plugin tree to scan (default: this repository).",
    )
    parser.add_argument(
        "--max-verdict",
        choices=VERDICT_ORDER,
        default="caution",
        help=(
            "Worst verdict that still passes. Default 'caution' because only a "
            "'dangerous' verdict actually blocks installation."
        ),
    )
    args = parser.parse_args()

    hermes_src = args.hermes_src.resolve()
    if not (hermes_src / "tools" / "plugin_guard.py").is_file():
        print(
            f"error: {hermes_src} does not look like a hermes-agent checkout "
            "(no tools/plugin_guard.py)",
            file=sys.stderr,
        )
        return 2

    sys.path.insert(0, str(hermes_src))
    try:
        from tools.plugin_guard import scan_plugin, should_allow_plugin_install
    except Exception as exc:  # pragma: no cover - depends on the pinned checkout
        print(f"error: could not import Hermes' plugin scanner: {exc}", file=sys.stderr)
        return 2

    result = scan_plugin(args.plugin_dir.resolve(), source="antitree/meshtastic-hermes-plugin")

    # Report every finding, worst first, so CI logs show the whole picture and
    # not just whatever tripped the gate.
    findings = sorted(
        result.findings,
        key=lambda f: (
            -(["low", "medium", "high", "critical"].index(f.severity)
              if f.severity in ("low", "medium", "high", "critical") else 0),
            f.file,
            f.line,
        ),
    )
    for f in findings:
        print(f"{f.severity.upper():9} {f.category:22} {f.pattern_id:22} {f.file}:{f.line}")

    _allowed, reason = should_allow_plugin_install(result)
    print()
    print(f"findings: {len(result.findings)}")
    print(f"verdict:  {result.verdict}")
    print(f"install:  {reason}")

    if VERDICT_ORDER.index(result.verdict) > VERDICT_ORDER.index(args.max_verdict):
        print(
            f"\nFAIL: verdict '{result.verdict}' is worse than "
            f"--max-verdict '{args.max_verdict}'.",
            file=sys.stderr,
        )
        return 1

    print(f"\nOK: verdict '{result.verdict}' is within --max-verdict '{args.max_verdict}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
