"""Redaction of homelab details from anything the rig prints or writes.

The rig reads real gateway logs and real radio identity. That output routinely
ends up pasted into a commit message, a PR, or an issue, so scrubbing is a hard
requirement rather than a nicety: everything the rig emits goes through
:class:`Scrubber` first.

Two layers, applied in order:

1. *Known secrets* — the concrete values pulled from ``.testrig.env`` and from
   live gateway state (the node's short name, long name, node id, the host, the
   test channel). These are matched literally, longest-first, so a value that
   contains another value still redacts correctly.
2. *Generic patterns* — node ids (``!a1b2c3d4``), IPv4 addresses, and dotted
   hostnames. These catch identifiers the rig never learned about, which is the
   common case for a log line mentioning some *other* node on the mesh.

Layer 2 alone is not enough (a short name like ``NODE`` matches no pattern) and
layer 1 alone is not enough (the rig cannot enumerate every node on the mesh),
so both always run.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# ``!`` followed by exactly 8 hex digits — the Meshtastic node-id form. The
# trailing boundary stops ``!deadbeef1`` from being partially redacted.
_NODE_ID_RE = re.compile(r"![0-9a-fA-F]{8}\b")

# Dotted-quad IPv4. Bounded so it does not chew a version string like 1.2.3.4.5.
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# A dotted hostname (``host.example.com``). Requires at least two dot-separated
# labels and an alphabetic final label so it does not collide with the IPv4 rule
# or with ordinary sentence punctuation.
_HOSTNAME_RE = re.compile(r"\b(?:[A-Za-z0-9][A-Za-z0-9-]*\.)+[A-Za-z]{2,}\b")

# Placeholders. Distinct per class so a reader can still tell *what* was removed,
# which keeps a scrubbed failure message diagnosable.
NODE_ID = "<node-id>"
IPV4 = "<ip>"
HOSTNAME = "<host>"
SECRET = "<redacted>"

# Dotted strings that are public knowledge and carry no homelab information.
# Redacting these makes output actively harder to read for zero privacy gain.
_HOSTNAME_ALLOWLIST = frozenset(
    {
        "gateway.platforms.base",
        "gateway.config",
        "gateway.run",
        "meshtastic_platform.adapter",
        "meshtastic_hermes.connection",
        "gateway_state.json",
        "channel_directory.json",
        "config.yaml",
        "plugin.yaml",
    }
)


class Scrubber:
    """Redact homelab identifiers from rig output.

    ``secrets`` are literal values (hostnames, node names, channel names) learned
    from config or live state. Blank/short values are ignored: redacting a
    1-character string would shred unrelated text.
    """

    #: Literal secrets shorter than this are ignored as too generic to redact.
    MIN_SECRET_LEN = 3

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        cleaned = set()
        for raw in secrets:
            value = (raw or "").strip()
            if len(value) >= self.MIN_SECRET_LEN:
                cleaned.add(value)
        # Longest first: redact "NODEBOX" before "NODE", otherwise the short
        # match leaves "<redacted>BOX" behind and leaks the tail.
        self._secrets = sorted(cleaned, key=len, reverse=True)

    def add(self, *values: str | None) -> None:
        """Learn additional literal secrets (e.g. identity read from live state)."""
        merged = set(self._secrets)
        for raw in values:
            value = (raw or "").strip()
            if len(value) >= self.MIN_SECRET_LEN:
                merged.add(value)
        self._secrets = sorted(merged, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        """Return *text* with every known and pattern-matched identifier removed."""
        if not text:
            return text

        out = text
        # Layer 1: literal secrets, case-insensitively (logs vary in casing).
        for secret in self._secrets:
            out = re.sub(re.escape(secret), SECRET, out, flags=re.IGNORECASE)

        # Layer 2: generic patterns. Node ids before hostnames so a node id is
        # never partially consumed by another rule.
        out = _NODE_ID_RE.sub(NODE_ID, out)
        out = _IPV4_RE.sub(IPV4, out)
        out = _HOSTNAME_RE.sub(self._scrub_hostname, out)
        return out

    @staticmethod
    def _scrub_hostname(match: re.Match[str]) -> str:
        """Keep known-public dotted names readable; redact everything else."""
        value = match.group(0)
        if value in _HOSTNAME_ALLOWLIST:
            return value
        # Python module paths and filenames are structural, not identifying.
        if value.endswith((".py", ".json", ".yaml", ".yml", ".md", ".log", ".sqlite")):
            return value
        return HOSTNAME
