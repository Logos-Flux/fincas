"""Rate limits, pauses and provider back-pressure (docs/crm/phase6-adapters.md 2.9).

- Each adapter declares a conservative :class:`RateLimit` (requests per
  period per account); ``IntegrationAccount.settings["rate_limit"]``
  (``{"requests": N, "per_seconds": S}``) may raise it to the plan's figure.
  :func:`take` enforces it **per process** with a token bucket. A caller
  that finds no token gets the wait back and returns ``Retry(after=wait)``
  at once: nothing here sleeps (the outbox's rule, 1.5).
- A 429 (or the provider's equivalent) sets ``paused_until`` on the account,
  or on the credential for a per-user grant (:func:`pause`); the poller and
  the adapter consumers skip paused accounts until it passes.

The clock is injectable (``clock=``) so tests drive the bucket and the
pauses with a fake one; production uses ``time.monotonic`` for buckets and
``timezone.now`` for pauses.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Callable

from django.utils import timezone

__all__ = [
    "RateLimit",
    "effective_limit",
    "pause",
    "paused_for",
    "reset",
    "retry_after_seconds",
    "take",
]

#: Longest pause a provider's ``Retry-After`` may impose: a typo'd header
#: must not silence an account for a year.
MAX_PAUSE = timedelta(hours=6)


@dataclass(frozen=True)
class RateLimit:
    requests: int = 1
    per_seconds: float = 1.0

    def __post_init__(self):
        if not isinstance(self.requests, int) or isinstance(self.requests, bool) or self.requests < 1:
            raise ValueError("RateLimit.requests must be a positive int.")
        if not isinstance(self.per_seconds, (int, float)) or self.per_seconds <= 0:
            raise ValueError("RateLimit.per_seconds must be positive.")

    @property
    def rate(self) -> float:
        return self.requests / float(self.per_seconds)


def effective_limit(default: RateLimit, account_settings: dict | None) -> RateLimit:
    """The adapter's limit, or the account's ``settings["rate_limit"]`` when valid."""
    raw = (account_settings or {}).get("rate_limit")
    if isinstance(raw, dict):
        try:
            return RateLimit(requests=raw.get("requests"), per_seconds=raw.get("per_seconds"))
        except (TypeError, ValueError):
            return default
    return default


class _Bucket:
    __slots__ = ("tokens", "stamp")

    def __init__(self, tokens: float, stamp: float):
        self.tokens = tokens
        self.stamp = stamp


_buckets: dict[str, _Bucket] = {}
_lock = threading.Lock()


#: The bucket clock. Fixture replays swap it (``core.testing.integration_fixtures``).
_monotonic: Callable[[], float] = time.monotonic


def take(key: str, limit: RateLimit, *, clock: Callable[[], float] | None = None) -> float:
    """Spend one token from ``key``'s bucket. Returns 0.0 when spent, else seconds to wait.

    The bucket holds at most ``limit.requests`` tokens and refills at
    ``limit.rate`` per second. Never sleeps.
    """
    now = (clock or _monotonic)()
    with _lock:
        bucket = _buckets.get(key)
        if bucket is None:
            bucket = _buckets[key] = _Bucket(float(limit.requests), now)
        elapsed = max(0.0, now - bucket.stamp)
        bucket.tokens = min(float(limit.requests), bucket.tokens + elapsed * limit.rate)
        bucket.stamp = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0.0
        return (1.0 - bucket.tokens) / limit.rate


def reset(key: str | None = None) -> None:
    """Forget one bucket (or all): tests, and an operator's resume."""
    with _lock:
        if key is None:
            _buckets.clear()
        else:
            _buckets.pop(key, None)


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """A ``Retry-After`` header (delta seconds or an HTTP date) as seconds, capped at MAX_PAUSE."""
    if not value:
        return None
    value = value.strip()
    seconds: float | None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - (now or timezone.now())).total_seconds()
    if seconds != seconds or seconds < 0:  # NaN or in the past
        return 0.0
    return min(seconds, MAX_PAUSE.total_seconds())


def pause(target, seconds: float, *, now: datetime | None = None, reason: str = "") -> datetime:
    """Set ``paused_until`` on an account or a credential (never shortens a pause).

    ``reason`` is an error code (``rate_limited``, ``credentials_rejected``)
    written to the account's ``last_error``; never a provider body.
    """
    now = now or timezone.now()
    until = now + min(timedelta(seconds=max(0.0, seconds)), MAX_PAUSE)
    updates = {}
    if target.paused_until is None or target.paused_until < until:
        target.paused_until = updates["paused_until"] = until
    if reason and hasattr(target, "last_error"):  # an account; a grant's pause is its own
        target.last_error = updates["last_error"] = reason[:500]
        target.last_error_at = updates["last_error_at"] = now
    if updates:
        type(target)._base_manager.filter(pk=target.pk).update(**updates)
    return target.paused_until


def paused_for(target, *, now: datetime | None = None) -> float:
    """Seconds until ``target`` (an account or a credential) resumes; 0.0 if not paused."""
    until = getattr(target, "paused_until", None)
    if until is None:
        return 0.0
    return max(0.0, (until - (now or timezone.now())).total_seconds())
