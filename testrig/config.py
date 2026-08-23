"""Loading and validation of ``.testrig.env``.

Every host-specific value lives in ``.testrig.env``, which is gitignored. A
tracked ``.testrig.env.example`` carries placeholders only. This split is what
keeps homelab details out of the repository, so the loader refuses placeholder
values rather than letting a half-configured rig run against the wrong host.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

#: Values copied verbatim from the example file. Treated as "not configured".
PLACEHOLDERS = frozenset(
    {
        "",
        "changeme",
        "CHANGEME",
        "your-host.example.com",
        "your-user",
        "your-profile",
        "your-test-channel",
    }
)


class ConfigError(RuntimeError):
    """Raised when ``.testrig.env`` is missing, unreadable, or incomplete."""


@dataclass(frozen=True)
class RigConfig:
    """Resolved rig configuration."""

    host: str
    user: str
    profile: str
    test_channel: str
    remote_dir: str
    service: str
    gateway_log: str
    hermes_home: str
    hermes_python: str
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def ssh_target(self) -> str:
        """``user@host`` form used for every ssh/rsync invocation."""
        return f"{self.user}@{self.host}" if self.user else self.host

    def secrets(self) -> list[str]:
        """Literal values the scrubber must redact from all rig output."""
        return [self.host, self.user, self.profile, self.test_channel]


def parse_env(text: str) -> dict[str, str]:
    """Parse a dotenv-style file into a mapping.

    Supports ``KEY=value``, ``export KEY=value``, ``#`` comments, blank lines and
    quoted values. Deliberately tiny: the rig must stay dependency-light, and a
    real dotenv parser would be the only third-party import in the tree.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # Strip a trailing inline comment, but only outside quotes — a channel
        # name or password may legitimately contain '#'.
        if value[:1] not in ("'", '"'):
            value = value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def _require(values: dict[str, str], key: str, path: Path) -> str:
    value = (values.get(key) or "").strip()
    if value in PLACEHOLDERS:
        raise ConfigError(
            f"{key} is not set in {path} (it is blank or still the placeholder "
            f"from .testrig.env.example). Fill it in before running the rig."
        )
    return value


def load_config(path: str | os.PathLike[str] | None = None) -> RigConfig:
    """Load and validate ``.testrig.env``.

    Raises :class:`ConfigError` with an actionable message when the file is
    missing — pointing at the tracked example — or when a required key is still
    a placeholder.
    """
    env_path = Path(path) if path else Path(__file__).resolve().parent.parent / ".testrig.env"
    if not env_path.exists():
        raise ConfigError(
            f"{env_path} not found.\n"
            f"The test rig needs host-specific settings that are deliberately kept "
            f"out of git.\n"
            f"Copy the tracked example and fill it in:\n"
            f"    cp {env_path.parent / '.testrig.env.example'} {env_path}\n"
            f"Then edit it. {env_path.name} is gitignored and must stay that way."
        )

    try:
        values = parse_env(env_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Could not read {env_path}: {exc}") from exc

    host = _require(values, "TESTRIG_HOST", env_path)
    profile = _require(values, "TESTRIG_PROFILE", env_path)
    user = (values.get("TESTRIG_USER") or "").strip()
    if user in PLACEHOLDERS:
        user = ""  # optional: fall back to ssh_config's user for the host

    hermes_home = (values.get("TESTRIG_HERMES_HOME") or "").strip()
    if hermes_home in PLACEHOLDERS:
        hermes_home = f"~/.hermes/profiles/{profile}"

    remote_dir = (values.get("TESTRIG_REMOTE_DIR") or "").strip()
    if remote_dir in PLACEHOLDERS:
        remote_dir = "~/.cache/meshtastic-testrig"

    gateway_log = (values.get("TESTRIG_GATEWAY_LOG") or "").strip()
    if gateway_log in PLACEHOLDERS:
        gateway_log = f"{hermes_home}/logs/gateway.log"

    hermes_python = (values.get("TESTRIG_HERMES_PYTHON") or "").strip()
    if hermes_python in PLACEHOLDERS:
        hermes_python = "~/.hermes/hermes-agent/venv/bin/python"

    test_channel = (values.get("TESTRIG_TEST_CHANNEL") or "").strip()
    if test_channel in PLACEHOLDERS:
        test_channel = ""  # only required for --transmit; checked at use site

    known = {
        "TESTRIG_HOST",
        "TESTRIG_USER",
        "TESTRIG_PROFILE",
        "TESTRIG_TEST_CHANNEL",
        "TESTRIG_REMOTE_DIR",
        "TESTRIG_SERVICE",
        "TESTRIG_GATEWAY_LOG",
        "TESTRIG_HERMES_HOME",
        "TESTRIG_HERMES_PYTHON",
    }
    return RigConfig(
        host=host,
        user=user,
        profile=profile,
        test_channel=test_channel,
        remote_dir=remote_dir,
        service=(values.get("TESTRIG_SERVICE") or "").strip(),
        gateway_log=gateway_log,
        hermes_home=hermes_home,
        hermes_python=hermes_python,
        extra={k: v for k, v in values.items() if k not in known},
    )


def remote_quote(value: str) -> str:
    """Quote a value for safe interpolation into a remote shell command.

    ``~`` must stay unquoted to expand, so a leading ``~/`` is preserved and only
    the remainder is quoted.
    """
    if value.startswith("~/"):
        return "~/" + shlex.quote(value[2:])
    if value == "~":
        return "~"
    return shlex.quote(value)
