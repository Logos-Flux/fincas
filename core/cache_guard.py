"""The shared cache, when it may be unreachable (F-086).

Django's cache API raises the backend's own exception (redis-py's
``ConnectionError``, a socket timeout, ...) when the server is down, and a
request path that reads the cache with no guard answers 500. The staging
outage drill of 2026-10-18 found two such paths: the label registry, which
runs in a context processor on every back-office page, and the login
throttle. This module is the one place that decides what an outage costs:

- **A breaker.** After a failure the backend is not tried again for
  ``BREAKER_S``; callers get the process-local fallback instead. Without it
  a Redis that hangs rather than refuses would cost every label call its
  socket timeout (a tenant-labelled page makes 20 to 30 of them). The next
  call after the breaker closes tries the backend again; a success keeps
  it closed.
- **A fallback.** ``cache_set`` remembers the last value written, per key,
  for ``fallback_ttl`` seconds (default ``FALLBACK_TTL_S``, the label
  registry asks for longer), bounded at ``FALLBACK_MAX`` keys, so an outage
  costs the database once per TTL per process, never once per read.
  ``cache_add`` claims a key in this process only while the backend is
  down: degraded (another worker does not see the claim), never blind.
- **Quiet logs.** ``failed`` logs the first failure of a message, with its
  traceback, at WARNING and repeats at DEBUG for ``WARN_INTERVAL_S``, so
  every outage and every flap produce a bounded number of warnings per
  process.
- **An escape hatch.** ``GOLDEN_CACHE_DOWN_FAIL_OPEN = True`` (settings) makes
  the cache-counted gates (login, OIDC start, the DRF throttles) let
  requests through uncounted during an outage instead of refusing, for an
  operator who must sign in to fix the incident. Off by default: a counter
  that cannot count refuses.
- A Redis that hangs is also bounded by the socket timeouts on the cache
  setting (``golden_erp/settings/base.py``): the breaker opens after the
  first timeout, not before.

Callers that must refuse rather than guess (the throttles) use ``is_down``,
``failed`` and ``ok`` directly; ``CacheUnavailable`` is for a helper that
has to tell its caller the backend is gone.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from typing import Any

from django.conf import settings
from django.core.cache import cache

log = logging.getLogger(__name__)

FALLBACK_TTL_S = 5.0
BREAKER_S = 5.0
WARN_INTERVAL_S = 60.0
FALLBACK_MAX = 2048


class CacheUnavailable(Exception):
    """The cache backend is unreachable and the caller must refuse."""


_lock = threading.Lock()
_warned: dict[str, float] = {}  # message -> when it was last logged at WARNING
_fallback: dict[str, tuple[Any, float]] = {}  # key -> (value, expires at)
_down_until = 0.0
_probe_started = 0.0  # while the breaker is half-open, when the one probe began
PROBE_TIMEOUT_S = 10.0


def fail_open() -> bool:
    return bool(getattr(settings, "GOLDEN_CACHE_DOWN_FAIL_OPEN", False))


def is_down() -> bool:
    """Whether the caller should stay on the fallback.

    Open (a failure within the last ``BREAKER_S``): yes. Closed: no. Half
    open (the breaker just expired): one caller at a time gets to probe the
    backend, the rest stay on the fallback until that probe succeeds
    (``ok``) or fails (``failed``), so a hung backend costs one socket
    timeout per ``BREAKER_S`` per process, not one per concurrent request.
    """
    global _probe_started
    if not _down_until:
        return False
    now = time.monotonic()
    if now < _down_until:
        return True
    with _lock:
        if _probe_started and now - _probe_started < PROBE_TIMEOUT_S:
            return True
        _probe_started = now
    return False


def failed(message: str, logger: logging.Logger = log) -> None:
    """Record a backend failure: open the breaker and log it. Call from inside
    the ``except`` block, so the WARNING carries the traceback."""
    global _down_until, _probe_started
    now = time.monotonic()
    with _lock:
        _down_until = now + BREAKER_S
        _probe_started = 0.0
        last = _warned.get(message)
        loud = last is None or now - last >= WARN_INTERVAL_S
        if loud:
            _warned[message] = now
    if loud:
        logger.warning(message, exc_info=True)
    else:
        logger.debug(message)


def ok() -> None:
    """A backend call succeeded: close the breaker."""
    global _down_until, _probe_started
    if _down_until:
        with _lock:
            _down_until = 0.0
            _probe_started = 0.0


def _ttl(timeout: float | None, fallback_ttl: float | None) -> float:
    ttl = FALLBACK_TTL_S if fallback_ttl is None else float(fallback_ttl)
    return ttl if timeout is None else min(float(timeout), ttl)


def _store(key: str, value: Any, ttl: float, now: float) -> None:
    """Under ``_lock``. A re-remembered key moves to the end, so eviction
    drops the least recently written key, not the first ever written."""
    _fallback.pop(key, None)
    if len(_fallback) >= FALLBACK_MAX:
        for stale in [k for k, (_, expires) in _fallback.items() if expires < now]:
            del _fallback[stale]
        while len(_fallback) >= FALLBACK_MAX:
            del _fallback[next(iter(_fallback))]
    _fallback[key] = (value, now + ttl)


def _remember(key: str, value: Any, ttl: float) -> None:
    with _lock:
        _store(key, value, ttl, time.monotonic())


def _remember_if_absent(key: str, value: Any, ttl: float) -> bool:
    """One atomic check-then-set: True when this call stored the key. Two
    near-simultaneous POSTs (a double click) must not both win a claim."""
    now = time.monotonic()
    with _lock:
        entry = _fallback.get(key)
        if entry is not None and entry[1] >= now:
            return False
        _store(key, value, ttl, now)
        return True


def _forget(key: str) -> None:
    if key in _fallback:
        with _lock:
            _fallback.pop(key, None)


def _remembered(key: str, default: Any) -> Any:
    entry = _fallback.get(key)
    if entry is None:
        return default
    value, expires_at = entry
    if expires_at < time.monotonic():
        _forget(key)
        return default
    return value


def cache_get(key: str, default: Any = None, *, what: str = "cache") -> Any:
    if is_down():
        return _remembered(key, default)
    try:
        value = cache.get(key, default)
    except Exception:  # noqa: BLE001 - the backend's error, whatever it is
        failed(f"{what} unavailable; reading without it")
        return _remembered(key, default)
    ok()
    return value


def cache_set(key: str, value: Any, timeout: float | None, *, what: str = "cache",
              fallback_ttl: float | None = None) -> None:
    if is_down():
        _remember(key, value, _ttl(timeout, fallback_ttl))
        return
    try:
        cache.set(key, value, timeout)
    except Exception:  # noqa: BLE001
        failed(f"{what} unavailable; remembering the value in this process")
        _remember(key, value, _ttl(timeout, fallback_ttl))
        return
    ok()
    _forget(key)


def cache_add(key: str, value: Any, timeout: float | None, *, what: str = "cache") -> bool:
    """``cache.add``: True when this call stored the key.

    With the backend down the key is claimed in this process only, for the
    same ``timeout``: a repeat on this worker is refused, one on another
    worker is not. Degraded, and logged, but never blind. A claim made while
    the backend was down is honoured after it recovers (the backend never
    saw it), until the claim's own timeout.
    """
    if _remembered(key, None) is not None:
        return False
    if not is_down():
        try:
            added = cache.add(key, value, timeout)
        except Exception:  # noqa: BLE001
            failed(f"{what} unavailable; claiming in this process only")
        else:
            ok()
            _forget(key)  # the backend's answer wins over any stale local copy
            return bool(added)
    return _remember_if_absent(key, value, _ttl(timeout, timeout))


def cache_delete(key: str, *, what: str = "cache") -> None:
    cache_delete_many([key], what=what)


def cache_delete_many(keys: Iterable[str], *, what: str = "cache") -> None:
    keys = list(keys)
    if not is_down():
        try:
            cache.delete_many(keys)
        except Exception:  # noqa: BLE001
            failed(f"{what} unavailable; a stale entry stays cached until it expires")
        else:
            ok()
    for key in keys:
        _forget(key)


async def cache_aget(key: str, default: Any = None, *, what: str = "cache") -> Any:
    if is_down():
        return _remembered(key, default)
    try:
        value = await cache.aget(key, default)
    except Exception:  # noqa: BLE001
        failed(f"{what} unavailable; reading without it")
        return _remembered(key, default)
    ok()
    return value


async def cache_aset(key: str, value: Any, timeout: float | None, *, what: str = "cache",
                     fallback_ttl: float | None = None) -> None:
    if is_down():
        _remember(key, value, _ttl(timeout, fallback_ttl))
        return
    try:
        await cache.aset(key, value, timeout)
    except Exception:  # noqa: BLE001
        failed(f"{what} unavailable; remembering the value in this process")
        _remember(key, value, _ttl(timeout, fallback_ttl))
        return
    ok()
    _forget(key)


def _reset() -> None:
    """Tests only: close the breaker, forget warnings and remembered values."""
    global _down_until, _probe_started
    with _lock:
        _warned.clear()
        _fallback.clear()
        _down_until = 0.0
        _probe_started = 0.0
