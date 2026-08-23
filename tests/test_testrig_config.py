"""Tests for rig config parsing and the runner's safety guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from testrig.config import ConfigError, load_config, parse_env, remote_quote
from testrig.runner import RigError, assert_safe_remote_dir, format_report
from testrig.scrub import Scrubber

MINIMAL = "TESTRIG_HOST=h.example.com\nTESTRIG_USER=u\nTESTRIG_PROFILE=p\n"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".testrig.env"
    path.write_text(text)
    return path


# --- parse_env --------------------------------------------------------------


def test_parse_env_basic():
    assert parse_env("A=1\nB=two\n") == {"A": "1", "B": "two"}


def test_parse_env_ignores_comments_and_blanks():
    assert parse_env("# note\n\nA=1\n") == {"A": "1"}


def test_parse_env_handles_export_and_quotes():
    assert parse_env("export A='x y'\nB=\"z\"\n") == {"A": "x y", "B": "z"}


def test_parse_env_strips_inline_comment_outside_quotes():
    assert parse_env("A=1 # note\n") == {"A": "1"}


def test_parse_env_keeps_hash_inside_quotes():
    assert parse_env("A='a#b'\n") == {"A": "a#b"}


def test_parse_env_allows_equals_in_value():
    assert parse_env("A=b=c\n") == {"A": "b=c"}


# --- load_config ------------------------------------------------------------


def test_missing_file_points_at_the_example(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "nope.env")
    message = str(exc.value)
    assert ".testrig.env.example" in message
    assert "gitignored" in message


def test_placeholder_host_is_rejected(tmp_path):
    path = _write(tmp_path, "TESTRIG_HOST=your-host.example.com\nTESTRIG_PROFILE=p\n")
    with pytest.raises(ConfigError, match="TESTRIG_HOST"):
        load_config(path)


def test_missing_profile_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="TESTRIG_PROFILE"):
        load_config(_write(tmp_path, "TESTRIG_HOST=h.example.com\n"))


def test_defaults_are_derived_from_profile(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL))
    assert cfg.hermes_home == "~/.hermes/profiles/p"
    assert cfg.gateway_log == "~/.hermes/profiles/p/logs/gateway.log"
    assert cfg.remote_dir == "~/.cache/meshtastic-testrig"
    assert cfg.hermes_python.endswith("venv/bin/python")


def test_explicit_values_override_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL + "TESTRIG_REMOTE_DIR=~/scratch/rig\n"))
    assert cfg.remote_dir == "~/scratch/rig"


def test_ssh_target_uses_user_when_present(tmp_path):
    assert load_config(_write(tmp_path, MINIMAL)).ssh_target == "u@h.example.com"


def test_ssh_target_falls_back_to_bare_host(tmp_path):
    cfg = load_config(_write(tmp_path, "TESTRIG_HOST=h.example.com\nTESTRIG_PROFILE=p\n"))
    assert cfg.ssh_target == "h.example.com"


def test_secrets_include_host_and_profile(tmp_path):
    assert "h.example.com" in load_config(_write(tmp_path, MINIMAL)).secrets()


# --- remote_quote -----------------------------------------------------------


def test_remote_quote_preserves_tilde_for_expansion():
    assert remote_quote("~/a b").startswith("~/")


def test_remote_quote_escapes_injection():
    quoted = remote_quote("a; rm -rf /")
    assert quoted.startswith("'") and "; rm -rf /" in quoted


# --- safety guard -----------------------------------------------------------


@pytest.mark.parametrize(
    "unsafe",
    [
        "~/.hermes/profiles/testprof",
        "~/.hermes/plugins/meshtastic-hermes-plugin",
        "~/.hermes/hermes-agent",
        "/home/u/.hermes/profiles/x",
        "~",
        "/",
        "",
    ],
)
def test_refuses_scratch_dirs_that_would_destroy_the_real_install(unsafe):
    """The rig rm -rf's this directory, so overlap with the real install is fatal."""
    with pytest.raises(RigError):
        assert_safe_remote_dir(unsafe)


@pytest.mark.parametrize("safe", ["~/.cache/meshtastic-testrig", "/tmp/rig", "~/scratch/x"])
def test_allows_throwaway_scratch_dirs(safe):
    assert_safe_remote_dir(safe)


# --- reporting --------------------------------------------------------------


def test_report_exits_nonzero_on_failure():
    payload = {"checks": [{"name": "x", "status": "FAIL", "detail": "boom"}]}
    text, code = format_report(payload, Scrubber())
    assert code == 1
    assert "FAIL" in text


def test_report_exits_zero_when_only_skips_and_passes():
    payload = {
        "checks": [
            {"name": "a", "status": "PASS", "detail": "ok"},
            {"name": "b", "status": "SKIP", "detail": "n/a"},
            {"name": "c", "status": "NOT_IMPLEMENTED", "detail": "n/a"},
        ]
    }
    _, code = format_report(payload, Scrubber())
    assert code == 0


def test_report_scrubs_check_details():
    payload = {"checks": [{"name": "x", "status": "PASS", "detail": "node !deadbeef"}]}
    text, _ = format_report(payload, Scrubber())
    assert "!deadbeef" not in text


def test_report_lists_failures_first():
    payload = {
        "checks": [
            {"name": "ok", "status": "PASS", "detail": ""},
            {"name": "bad", "status": "FAIL", "detail": ""},
        ]
    }
    text, _ = format_report(payload, Scrubber())
    assert text.index("bad") < text.index("ok")
