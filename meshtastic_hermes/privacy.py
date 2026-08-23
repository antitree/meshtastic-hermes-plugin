"""Read-tool privacy gates — remediation item 4.

Items 1-3 bounded what this plugin *transmits*. This one bounds what it *discloses*.
Every read tool in ``tools.py`` handed its result to the LLM verbatim, and the LLM's
output goes wherever the conversation goes — a chat window, a summary, another tool,
a log. Three classes of data in those responses do not belong there by default:

**Position.** ``meshtastic_list_nodes``, ``meshtastic_node_info`` and
``meshtastic_device_metrics`` returned ``lat``/``lon``/``altitude`` straight out of
the radio's node DB, and the persisted KB carries ``lat``/``lon`` columns of its own.
These are the real-world coordinates of real people who joined a shared mesh — they
broadcast a position so nearby radios could route to them, not so an agent could
recite where they live. This is the sharpest item in the file and it fails closed.

**Recent plaintext.** ``meshtastic_recent_messages`` returned the decoded bodies held
in the observer's RAM buffer: other people's channel messages and direct messages this
node happened to decrypt. Same data item 5 refused to write to the journal, handed to
the model instead.

**Traffic metadata.** Per-packet interaction records, per-node rollups and inferred
neighbor graphs are a social graph of the mesh. Any single row is dull; the aggregate
is reconnaissance — who talks to whom, from where, how often, when they are awake.

Two design decisions worth stating plainly:

1. **Redaction happens at the BOUNDARY, not at the source.** The observer's buffer and
   the SQLite KB keep complete records. They are legitimate internal stores — the
   gateway needs message text to answer it, the KB needs full rows to compute
   summaries and neighbor counts — and stripping fields there would break the
   plugin's actual function while leaving every other reader unprotected. The gate is
   here, on the way *out* to the model.

2. **Both paths are covered.** The same sensitive fields reach the model by two
   independent routes: the live radio node DB (``iface.nodes``) and the persisted KB
   (``nodes.lat``/``nodes.lon``). A helper wired into only one leaves the other wide
   open, so :func:`redact_location` is shaped to run over *any* mapping and is applied
   on both.

Polarity, matching ``MESHTASTIC_DEBUG_LOG_TEXT`` (item 5): these are EXPOSURE
switches, so they default OFF and require an explicit truthy value. A typo redacts.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

# Same truthy spellings as policy.py / rate_limit.py / the adapter.
_TRUTHY = {"1", "true", "yes", "on"}

EXPOSE_LOCATION_ENV = "MESHTASTIC_EXPOSE_LOCATION"
EXPOSE_RECENT_TEXT_ENV = "MESHTASTIC_EXPOSE_RECENT_TEXT"
EXPOSE_TRAFFIC_METADATA_ENV = "MESHTASTIC_EXPOSE_TRAFFIC_METADATA"

# Every spelling of a coordinate that appears anywhere in a tool response, across
# BOTH sources: the live radio node DB (`latitude`/`longitude`/`altitude`, and the
# `lat`/`lon` keys _node_summary derives from them) and the persisted KB, whose
# `nodes` table has its own `lat`/`lon` columns. Listing every alias here is what
# keeps a future column rename from quietly reopening the hole on one path only.
LOCATION_KEYS = frozenset(
    {
        "lat",
        "lon",
        "lng",
        "latitude",
        "longitude",
        "altitude",
        "alt",
        "latitude_i",
        "longitude_i",
        "position",
    }
)

# What the model is told instead of a coordinate, so it does not read a missing key
# as "this node has no position" and go hunting for it in another tool.
LOCATION_REDACTED_NOTE = (
    "Position fields are withheld by operator policy "
    f"({EXPOSE_LOCATION_ENV} is not enabled)."
)

RECENT_TEXT_REDACTED_NOTE = (
    "Message bodies are withheld by operator policy "
    f"({EXPOSE_RECENT_TEXT_ENV} is not enabled). Per-message metadata (sender, "
    "channel, timestamp, length and a short content hash) is shown instead."
)

TRAFFIC_METADATA_REDACTED_NOTE = (
    "Detailed traffic records are withheld by operator policy "
    f"({EXPOSE_TRAFFIC_METADATA_ENV} is not enabled). Summary counts are shown "
    "instead."
)


def _flag(name: str) -> bool:
    """An exposure flag is on ONLY for an explicit truthy value — a typo redacts."""
    return (os.environ.get(name) or "").strip().lower() in _TRUTHY


def expose_location() -> bool:
    """Whether tool responses may include lat/lon/altitude."""
    return _flag(EXPOSE_LOCATION_ENV)


def expose_recent_text() -> bool:
    """Whether ``meshtastic_recent_messages`` may include decoded message bodies."""
    return _flag(EXPOSE_RECENT_TEXT_ENV)


def expose_traffic_metadata() -> bool:
    """Whether the detailed KB reconnaissance views (per-node rows, per-packet
    interactions, inferred neighbors) may be returned at all."""
    return _flag(EXPOSE_TRAFFIC_METADATA_ENV)


def redact_location(row: Any) -> Any:
    """Strip coordinates from one record (or a list of records) unless enabled.

    Deliberately source-agnostic: it takes any mapping and removes every key in
    :data:`LOCATION_KEYS`, so the SAME function serves the live radio node DB and
    the persisted KB rows. Nested mappings and lists are walked too — a coordinate
    tucked inside a sub-dict is exactly the leak a key-name check on the top level
    alone would miss.

    Returns a COPY; the caller's record (and therefore the internal store it came
    from) is never mutated.
    """
    if expose_location():
        return row
    return _strip(row)


def _strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k not in LOCATION_KEYS}
    if isinstance(value, (list, tuple)):
        return [_strip(v) for v in value]
    return value


def location_note(payload: dict) -> dict:
    """Annotate a response so the model knows position was withheld, not absent.

    Without this the model sees a node record with no coordinates and may conclude
    the node never reported one — then say so, or call another tool to "find" it.
    """
    if expose_location():
        return payload
    return {**payload, "location_redacted": True, "note": LOCATION_REDACTED_NOTE}


def text_digest(text: Any) -> tuple[int | None, str | None]:
    """Describe a message body without disclosing it: ``(length, short sha256)``.

    Deliberately the SAME redaction idiom item 5 established in
    :func:`meshtastic_platform.adapter.debug_text_for_log` — same fields, same
    8-hex-character SHA-256 prefix — so one body is described identically in a
    journal line and in a tool response, and the two can be correlated.

    It is reimplemented here rather than imported for two reasons. The dependency
    would run backwards (the platform adapter imports ``meshtastic_hermes``, not the
    other way round), and more importantly ``debug_text_for_log`` returns the RAW
    text when ``MESHTASTIC_DEBUG_LOG_TEXT`` is on. That switch governs what goes in
    the operator's own journal; it must never widen what a tool hands the model.
    Only ``MESHTASTIC_EXPOSE_RECENT_TEXT`` does that.
    """
    if text is None:
        return None, None
    if not isinstance(text, str):  # defensive: a malformed packet field
        text = str(text)
    return len(text), hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]


def redact_message(msg: dict) -> dict:
    """Reduce one recent-message record to non-content metadata.

    Sender, recipient, channel and timestamp survive — they are routing facts the
    same KB already records for every packet, encrypted ones included. The body is
    replaced by its length and short hash (see :func:`text_digest`).
    """
    out = {k: v for k, v in msg.items() if k != "text"}
    length, digest = text_digest(msg.get("text"))
    out["text_len"] = length
    out["text_sha256"] = digest
    out["text_redacted"] = True
    return out


def redact_messages(messages: list[dict]) -> tuple[list[dict], bool]:
    """Gate a recent-messages list. Returns ``(rows, redacted)``.

    Metadata rather than an error: the model can still legitimately learn that three
    messages arrived on channel 1 in the last minute and act on that, which is useful
    and discloses nothing anyone said.
    """
    if expose_recent_text():
        return messages, False
    return [redact_message(m) for m in messages], True
