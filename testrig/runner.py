"""Local driver: sync the working tree to the remote scratch dir, run the probe,
scrub and print the results.

Safety invariants enforced here (see ``docs/testing.md``):

* Sync target is a scratch directory, never the user's Hermes profile or the
  installed plugin directory. :func:`assert_safe_remote_dir` refuses paths that
  look like either.
* The gateway service is never restarted, stopped or reconfigured.
* Nothing is transmitted unless ``--transmit`` is passed.
* Every line printed goes through the scrubber.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .config import ConfigError, RigConfig, load_config, remote_quote
from .scrub import Scrubber

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]

#: Remote paths the rig must never sync into or delete. The rig deletes the
#: scratch dir on cleanup, so a misconfigured TESTRIG_REMOTE_DIR pointing at the
#: real profile would destroy the user's setup.
_FORBIDDEN_FRAGMENTS = (
    "/.hermes/profiles",
    "/.hermes/plugins",
    "/.hermes/hermes-agent",
)


class RigError(RuntimeError):
    """A rig-level failure (ssh, sync, probe) as opposed to a failed check."""


def assert_safe_remote_dir(remote_dir: str) -> None:
    """Refuse a scratch dir that overlaps the user's real Hermes install."""
    normalized = remote_dir.rstrip("/")
    if not normalized or normalized in ("~", "/", "."):
        raise RigError(f"TESTRIG_REMOTE_DIR={remote_dir!r} is unsafe (too broad)")
    probe = normalized.replace("~", "/home/USER", 1) if normalized.startswith("~") else normalized
    for fragment in _FORBIDDEN_FRAGMENTS:
        if fragment in probe:
            raise RigError(
                f"TESTRIG_REMOTE_DIR={remote_dir!r} points inside the user's real Hermes "
                f"install ({fragment}). The rig syncs and then DELETES this directory; "
                f"it must be a throwaway location such as ~/.cache/meshtastic-testrig."
            )


def _ssh(cfg: RigConfig, command: str, *, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", *SSH_OPTS, cfg.ssh_target, command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def check_ssh(cfg: RigConfig) -> None:
    """Fail fast with an actionable message if the host is unreachable."""
    try:
        proc = _ssh(cfg, "echo TESTRIG_SSH_OK", timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise RigError(
            f"SSH to the configured host timed out after {exc.timeout}s. "
            f"The rig needs passwordless (key-based) SSH."
        ) from exc
    except FileNotFoundError as exc:
        raise RigError("ssh is not installed or not on PATH") from exc

    if proc.returncode != 0 or "TESTRIG_SSH_OK" not in proc.stdout:
        raise RigError(
            "SSH connectivity check failed. The rig requires passwordless SSH to the "
            "host in TESTRIG_HOST.\n"
            f"  exit={proc.returncode}\n  stderr={proc.stderr.strip()[:400]}"
        )


def sync_tree(cfg: RigConfig, repo_root: Path) -> None:
    """Copy the CURRENT working tree (including uncommitted edits) to scratch.

    Uses ``git archive`` of the working tree via tar so the sync reflects exactly
    what is being tested, without requiring rsync on either end.
    """
    remote = remote_quote(cfg.remote_dir)
    proc = _ssh(cfg, f"rm -rf {remote} && mkdir -p {remote}", timeout=60)
    if proc.returncode != 0:
        raise RigError(f"could not prepare remote scratch dir: {proc.stderr.strip()[:300]}")

    tar = subprocess.run(
        [
            "tar",
            "-cf",
            "-",
            "--exclude=.git",
            "--exclude=__pycache__",
            "--exclude=.pytest_cache",
            "--exclude=.ruff_cache",
            "--exclude=.testrig.env",
            "meshtastic_hermes",
            "meshtastic_platform",
            "testrig",
        ],
        cwd=repo_root,
        capture_output=True,
        timeout=120,
    )
    if tar.returncode != 0:
        raise RigError(f"tar of the working tree failed: {tar.stderr.decode()[:300]}")

    push = subprocess.run(
        ["ssh", *SSH_OPTS, cfg.ssh_target, f"tar -xf - -C {remote}"],
        input=tar.stdout,
        capture_output=True,
        timeout=180,
    )
    if push.returncode != 0:
        raise RigError(f"could not unpack the tree on the remote: {push.stderr.decode()[:300]}")


def run_probe(cfg: RigConfig, *, transmit: bool) -> dict:
    """Execute the read-only probe on the remote inside the Hermes venv."""
    remote = remote_quote(cfg.remote_dir)
    env = [
        f"TESTRIG_SCRATCH=$(cd {remote} && pwd)",
        f"TESTRIG_HERMES_HOME={remote_quote(cfg.hermes_home)}",
        f"TESTRIG_GATEWAY_LOG={remote_quote(cfg.gateway_log)}",
        f"TESTRIG_TEST_CHANNEL={remote_quote(cfg.test_channel)}",
        f"TESTRIG_TRANSMIT={'1' if transmit else '0'}",
        # The gateway's own HERMES_HOME, so hermes_cli.config resolves the right
        # profile .env without us passing --profile to a mutating command.
        f"HERMES_HOME={remote_quote(cfg.hermes_home)}",
    ]
    cmd = (
        f"cd {remote} && " + " ".join(env) + f" {remote_quote(cfg.hermes_python)} "
        f"{remote}/testrig/remote_probe.py"
    )
    proc = _ssh(cfg, cmd, timeout=300)
    if proc.returncode != 0:
        raise RigError(
            f"remote probe failed (exit {proc.returncode}):\n{proc.stderr.strip()[:1200]}"
        )
    stdout = proc.stdout.strip()
    # The Hermes plugin system logs to stderr, but be tolerant of stray stdout.
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise RigError(f"probe produced no JSON result:\n{stdout[:1200]}")


def cleanup(cfg: RigConfig) -> None:
    """Remove the remote scratch dir. Best-effort; never fails the run."""
    try:
        assert_safe_remote_dir(cfg.remote_dir)
        _ssh(cfg, f"rm -rf {remote_quote(cfg.remote_dir)}", timeout=60)
    except Exception:
        pass


_ORDER = {"FAIL": 0, "PASS": 1, "SKIP": 2, "NOT_IMPLEMENTED": 3}


def format_report(payload: dict, scrubber: Scrubber) -> tuple[str, int]:
    """Render a scrubbed pass/fail summary. Returns (text, exit_code)."""
    checks = payload.get("checks", [])
    lines = ["", "Meshtastic live test rig", "=" * 60]
    counts: dict[str, int] = {}
    for check in sorted(checks, key=lambda c: _ORDER.get(c.get("status", ""), 9)):
        status = check.get("status", "?")
        counts[status] = counts.get(status, 0) + 1
        lines.append(f"[{status:<15}] {check.get('name')}")
        detail = scrubber.scrub(str(check.get("detail", "")))
        for dline in detail.splitlines():
            lines.append(f"    {dline}")
    lines.append("=" * 60)
    summary = ", ".join(f"{v} {k.lower()}" for k, v in sorted(counts.items()))
    lines.append(f"Summary: {summary or 'no checks run'}")
    lines.append("")
    return "\n".join(lines), (1 if counts.get("FAIL") else 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="testrig",
        description="Run live integration checks against a real Hermes install over SSH.",
    )
    parser.add_argument("--config", help="path to .testrig.env")
    parser.add_argument(
        "--transmit",
        action="store_true",
        help="enable the transmit check (OFF by default; the default run is zero-airtime)",
    )
    parser.add_argument(
        "--keep-scratch",
        action="store_true",
        help="leave the remote scratch directory in place for debugging",
    )
    parser.add_argument(
        "--no-scrub",
        action="store_true",
        help="DANGEROUS: print raw output. Never use when pasting into a commit, PR or issue.",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    scrubber = Scrubber(cfg.secrets())
    try:
        assert_safe_remote_dir(cfg.remote_dir)
        check_ssh(cfg)
        sync_tree(cfg, Path(__file__).resolve().parent.parent)
        payload = run_probe(cfg, transmit=args.transmit)
    except RigError as exc:
        print(f"error: {scrubber.scrub(str(exc))}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired as exc:
        print(f"error: remote command timed out after {exc.timeout}s", file=sys.stderr)
        return 2
    finally:
        if not args.keep_scratch:
            cleanup(cfg)

    # Learn the live node's real names so they are redacted too.
    scrubber.add(*payload.get("identity_secrets", []))
    if args.no_scrub:
        scrubber = Scrubber()

    text, code = format_report(payload, scrubber)
    print(text)
    if not args.transmit:
        print("Note: default run is zero-airtime — nothing was transmitted.\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
