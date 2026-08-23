"""Process-wide transmit rate limiter — remediation item 3.

This is a **loop breaker**, not a politeness feature. LoRa is a shared, legally
regulated medium, and every packet this plugin emits is airtime taken from everyone
else in range. Before this module existed, nothing bounded outbound volume: the
adapter capped a *single* reply to five parts, but two bots that answer each other
on the same channel would trade five-part replies forever, and a model in a tool
loop could call ``meshtastic_send_text`` without limit.

Three things make this module work where a naive limiter would not:

1. **It counts transmitted PACKETS, not logical messages.** A long reply is chunked
   into several mesh packets by the adapter, and each chunk is its own transmission
   with its own airtime. Every outbound path funnels through
   :func:`meshtastic_hermes.tools.send_text`, and the check lives there — one call,
   one token — so a five-part reply spends five tokens, never one.

2. **It is shared across BOTH copies of this package.** Hermes loads the tools
   plugin under a mangled package name while the platform adapter imports
   ``meshtastic_hermes`` top-level, so two independent module objects exist with two
   sets of globals. State therefore lives in a fixed ``sys.modules`` slot, exactly
   as ``connection.py`` does for the ConnectionManager. Module globals here would
   give the tool path and the adapter path a bucket each, i.e. double the configured
   limit and no shared loop breaker at all.

3. **It is pure of everything else.** No radio, no gateway, no LLM. The clock is an
   injected ``time_fn`` (defaulting to :func:`time.monotonic`), so tests drive it
   with fake time instead of sleeping.

Buckets are keyed three ways and **all applicable buckets must admit** a send:

``global``
    every outbound packet, whatever its destination.
``dm:<dest_id>``
    packets directed at one node id. Per-peer, so one chatty peer cannot consume
    the whole budget of another.
``ch:<index>``
    broadcasts on one channel index. Independent of DM buckets: answering a DM
    should not spend the channel's broadcast budget, and vice versa.

On top of the buckets there is a **cooldown**: a minimum spacing between two
consecutive sends to the *same* destination. The buckets bound volume over a minute;
the cooldown bounds how fast a tight bot-to-bot exchange can spin before the bucket
even notices.

Monotonic time is used deliberately: a wall-clock step (NTP, DST, a manual date
change) must never hand out a windfall of free transmissions.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_SENDS_PER_MINUTE_ENV = "MESHTASTIC_MAX_SENDS_PER_MINUTE"
MAX_CHANNEL_SENDS_PER_MINUTE_ENV = "MESHTASTIC_MAX_CHANNEL_SENDS_PER_MINUTE"
MAX_DM_SENDS_PER_MINUTE_ENV = "MESHTASTIC_MAX_DM_SENDS_PER_MINUTE"
REPLY_COOLDOWN_SECONDS_ENV = "MESHTASTIC_REPLY_COOLDOWN_SECONDS"

# Conservative defaults. A Meshtastic text packet at a default LoRa preset occupies
# a meaningful slice of a slow, shared channel, and the duty-cycle budget in most
# regions is small. These numbers are deliberately lower than "what the radio can
# physically do": the point is that an unattended bot's *failure* mode stays quiet.
DEFAULT_MAX_SENDS_PER_MINUTE = 10
DEFAULT_MAX_CHANNEL_SENDS_PER_MINUTE = 5
DEFAULT_MAX_DM_SENDS_PER_MINUTE = 6
DEFAULT_REPLY_COOLDOWN_SECONDS = 5.0

WINDOW_SECONDS = 60.0

# The error code every outbound path reports when the limiter refuses a send.
RATE_LIMITED = "rate_limited"


class RateLimited(Exception):
    """Raised when a send is refused by the limiter.

    Carries ``retry_after_s`` so the caller can render the documented JSON shape
    ``{"error": "rate_limited", "retry_after_s": 12}`` without re-deriving it, and a
    ``code`` attribute matching the :class:`meshtastic_hermes.policy.ToolSendRejected`
    convention so both rejection families are handled the same way.
    """

    code = RATE_LIMITED

    def __init__(self, message: str, retry_after_s: float, scope: str) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s
        self.scope = scope


# ── configuration ────────────────────────────────────────────────────────────────
#
# Config FAILS CLOSED. A value that cannot be parsed, or that is <= 0, falls back to
# the conservative default and logs. It must never be read as "unlimited": a typo in
# a rate limit that silently disabled the limit would defeat the whole item.


def _env_positive(name: str, default: float, cast: Callable[[str], float]) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = cast(raw.strip())
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not a number — falling back to the conservative default %s. "
            "Rate limits fail CLOSED: an unparseable limit is never treated as unlimited.",
            name,
            raw,
            default,
        )
        return default
    if value <= 0:
        logger.warning(
            "%s=%r must be > 0 — falling back to the conservative default %s. "
            "There is no way to spell 'no limit' here on purpose.",
            name,
            raw,
            default,
        )
        return default
    return value


@dataclass(frozen=True)
class LimitConfig:
    """A snapshot of the configured limits.

    Read from the environment per :func:`from_env` call rather than cached, so a test
    (or an operator restarting the gateway with new settings) sees the change without
    a stale process-wide copy fighting it.
    """

    max_sends_per_minute: int = DEFAULT_MAX_SENDS_PER_MINUTE
    max_channel_sends_per_minute: int = DEFAULT_MAX_CHANNEL_SENDS_PER_MINUTE
    max_dm_sends_per_minute: int = DEFAULT_MAX_DM_SENDS_PER_MINUTE
    reply_cooldown_seconds: float = DEFAULT_REPLY_COOLDOWN_SECONDS

    @classmethod
    def from_env(cls) -> LimitConfig:
        return cls(
            max_sends_per_minute=int(
                _env_positive(MAX_SENDS_PER_MINUTE_ENV, DEFAULT_MAX_SENDS_PER_MINUTE, float)
            ),
            max_channel_sends_per_minute=int(
                _env_positive(
                    MAX_CHANNEL_SENDS_PER_MINUTE_ENV,
                    DEFAULT_MAX_CHANNEL_SENDS_PER_MINUTE,
                    float,
                )
            ),
            max_dm_sends_per_minute=int(
                _env_positive(MAX_DM_SENDS_PER_MINUTE_ENV, DEFAULT_MAX_DM_SENDS_PER_MINUTE, float)
            ),
            reply_cooldown_seconds=float(
                _env_positive(
                    REPLY_COOLDOWN_SECONDS_ENV, DEFAULT_REPLY_COOLDOWN_SECONDS, float
                )
            ),
        )


# ── the limiter ──────────────────────────────────────────────────────────────────


class TransmitLimiter:
    """Sliding-window packet counters plus a per-destination cooldown.

    A sliding window (a deque of timestamps per bucket) rather than a fixed window,
    because a fixed window lets a burst straddle the boundary and emit ``2 * limit``
    packets back-to-back — precisely the runaway this exists to stop.

    Thread-safe: the adapter sends from an asyncio executor thread, the radio RX
    callback runs on the ``meshtastic`` library's reader thread, and the reconnect
    supervisor is a third. The lock is held only around in-memory bookkeeping, never
    across the transmit itself.
    """

    def __init__(
        self,
        config: LimitConfig | None = None,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._time = time_fn
        self._config = config
        self._lock = threading.Lock()
        self._events: dict[str, list[float]] = {}
        self._last_send: dict[str, float] = {}

    @property
    def config(self) -> LimitConfig:
        """The configured limits — from the environment unless one was injected."""
        return self._config if self._config is not None else LimitConfig.from_env()

    # -- internals (call with the lock held) --------------------------------------

    def _prune(self, key: str, now: float) -> list[float]:
        stamps = [t for t in self._events.get(key, ()) if now - t < WINDOW_SECONDS]
        self._events[key] = stamps
        return stamps

    def _window_retry_after(self, key: str, limit: int, now: float) -> float:
        """Seconds until this bucket has room, assuming it is currently full."""
        stamps = self._events.get(key) or []
        if len(stamps) < limit:
            return 0.0
        # The oldest stamp that still counts is the one whose expiry frees a slot.
        oldest = stamps[-limit]
        return max(0.0, WINDOW_SECONDS - (now - oldest))

    @staticmethod
    def _keys(dest_id: str | None, channel_index: int | None) -> tuple[str, str]:
        """The (bucket key, cooldown key) pair for one destination.

        A DM is keyed by node id and a broadcast by channel index, and the two
        namespaces never collide, so the buckets are independent by construction.
        """
        if dest_id:
            return f"dm:{dest_id}", f"dm:{dest_id}"
        return f"ch:{channel_index if channel_index is not None else 0}", (
            f"ch:{channel_index if channel_index is not None else 0}"
        )

    # -- public API ----------------------------------------------------------------

    def check(
        self,
        dest_id: str | None = None,
        channel_index: int | None = None,
        *,
        continuation: bool = False,
    ) -> None:
        """Consume one token for ONE outbound packet, or raise :class:`RateLimited`.

        Call this immediately before the transmit, once per packet. It is atomic:
        either every applicable bucket admits the packet and all are charged, or
        nothing is charged and the send is refused. A partial charge would let a
        refused send still eat the global budget.

        ``continuation=True`` marks a packet that is a later chunk of a reply whose
        first chunk was already admitted. Such a packet still costs a full token in
        every bucket — chunks are real airtime, which is the whole point of the
        per-packet accounting — but it skips the COOLDOWN gate. The cooldown exists
        to space out *conversational turns*; applying it between the parts of one
        answer would make any reply longer than a single 200-byte packet permanently
        undeliverable at any sane cooldown setting.
        """
        cfg = self.config
        specific_key, cooldown_key = self._keys(dest_id, channel_index)
        specific_limit = cfg.max_dm_sends_per_minute if dest_id else cfg.max_channel_sends_per_minute
        scope = "dm" if dest_id else "channel"

        with self._lock:
            now = self._time()

            # Cooldown first: it is the cheapest signal and the one that actually
            # breaks a tight two-bot ping-pong before any bucket fills.
            last = self._last_send.get(cooldown_key)
            if last is not None and not continuation:
                elapsed = now - last
                if elapsed < cfg.reply_cooldown_seconds:
                    raise RateLimited(
                        f"Cooldown active for {cooldown_key}: "
                        f"{cfg.reply_cooldown_seconds:g}s minimum between sends to the "
                        f"same destination ({REPLY_COOLDOWN_SECONDS_ENV}).",
                        retry_after_s=round(cfg.reply_cooldown_seconds - elapsed, 3),
                        scope="cooldown",
                    )

            self._prune("global", now)
            self._prune(specific_key, now)

            if len(self._events["global"]) >= cfg.max_sends_per_minute:
                raise RateLimited(
                    f"Global transmit limit reached: {cfg.max_sends_per_minute} sends per "
                    f"minute ({MAX_SENDS_PER_MINUTE_ENV}).",
                    retry_after_s=round(
                        self._window_retry_after("global", cfg.max_sends_per_minute, now), 3
                    ),
                    scope="global",
                )

            if len(self._events[specific_key]) >= specific_limit:
                env = (
                    MAX_DM_SENDS_PER_MINUTE_ENV
                    if dest_id
                    else MAX_CHANNEL_SENDS_PER_MINUTE_ENV
                )
                raise RateLimited(
                    f"Per-{scope} transmit limit reached for {specific_key}: "
                    f"{specific_limit} sends per minute ({env}).",
                    retry_after_s=round(
                        self._window_retry_after(specific_key, specific_limit, now), 3
                    ),
                    scope=scope,
                )

            # All gates passed — charge every bucket for this ONE packet.
            self._events["global"].append(now)
            self._events[specific_key].append(now)
            self._last_send[cooldown_key] = now

    def snapshot(self) -> dict:
        """Diagnostics: current per-bucket usage. Never used to make a decision."""
        with self._lock:
            now = self._time()
            return {
                key: len([t for t in stamps if now - t < WINDOW_SECONDS])
                for key, stamps in self._events.items()
            }

    def reset(self) -> None:
        """Drop all counters. For tests and for an explicit operator reset only."""
        with self._lock:
            self._events.clear()
            self._last_send.clear()


# ── process-wide singleton, shared across BOTH copies of this package ────────────
#
# See the module docstring and the identical construction in connection.py. The
# limiter MUST NOT be a module global: Hermes loads the tools plugin under a mangled
# package name while the adapter imports `meshtastic_hermes` top-level, so a module
# global would give each copy its own bucket — half the enforcement, and no shared
# loop breaker between the adapter and the tool.
_SHARED_KEY = "meshtastic_hermes._rate_limit_state"


def _shared_state():
    import types

    st = sys.modules.get(_SHARED_KEY)
    if st is None:
        st = types.ModuleType(_SHARED_KEY)
        st.limiter = None
        sys.modules[_SHARED_KEY] = st
    return st


def get_limiter() -> TransmitLimiter:
    """The one limiter every outbound path in this process shares."""
    st = _shared_state()
    if st.limiter is None:
        st.limiter = TransmitLimiter()
    return st.limiter


def set_limiter(limiter: TransmitLimiter | None) -> None:
    """Install a limiter (e.g. one driven by fake time) into the shared slot.

    Passing ``None`` clears it, so the next :func:`get_limiter` builds a fresh default.
    """
    _shared_state().limiter = limiter


def reset_limiter() -> None:
    """Clear the shared limiter's counters. Used by the per-test reset fixture."""
    st = sys.modules.get(_SHARED_KEY)
    if st is not None and getattr(st, "limiter", None) is not None:
        st.limiter.reset()


def check_send(
    dest_id: str | None = None,
    channel_index: int | None = None,
    *,
    continuation: bool = False,
) -> None:
    """Charge the shared limiter for one outbound packet, or raise :class:`RateLimited`.

    See :meth:`TransmitLimiter.check` for what ``continuation`` means; in short it is
    "this is part 2+ of an answer already in flight", which is charged a token like
    any other packet but is not held behind the per-destination cooldown.
    """
    get_limiter().check(
        dest_id=dest_id, channel_index=channel_index, continuation=continuation
    )
