"""Small in-memory TTL cache for near-immutable lookups.

Brands, categories, and warehouses change maybe once a month in the ERP,
but every `find_*` call round-trips to Django. This module backs those
lookups with a process-lifetime cache that expires entries after a short
TTL (default 5 minutes) so the happy path skips the network entirely
after the first call in a session.

Scope is deliberately narrow:
- Only used by lookup functions (list_* + find_*) for brands, categories,
  warehouses, price lists. Products, variants, orders, and stock are NOT
  cached because they change constantly.
- Cache is keyed on the full params dict, so different filter combinations
  each get their own entry.
- No background eviction — we just check the TTL on read and drop stale
  entries lazily.

The cache lives on a shared ERPClient instance (see mcp_adapter._get_client)
so it's reused across MCP tool calls for the life of the stdio process.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

DEFAULT_TTL_SECONDS = 300.0  # 5 minutes


@dataclass
class _Entry:
    value: Any
    expires_at: float


@dataclass
class TTLCache:
    """Async-safe TTL cache for list results.

    Not LRU — we don't expect enough distinct keys in a single process to
    need eviction. If that changes we can add a max-size with simple
    insertion-order trimming.
    """

    ttl: float = DEFAULT_TTL_SECONDS
    _entries: dict[tuple, _Entry] = field(default_factory=dict)

    def get(self, key: tuple) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if time.monotonic() >= entry.expires_at:
            # Lazy eviction — drop the stale entry and pretend it was never here
            del self._entries[key]
            return None
        return entry.value

    def set(self, key: tuple, value: Any) -> None:
        self._entries[key] = _Entry(
            value=value, expires_at=time.monotonic() + self.ttl
        )

    def invalidate(self, key: tuple | None = None) -> None:
        """Drop a specific key, or the whole cache if called with no argument."""
        if key is None:
            self._entries.clear()
        else:
            self._entries.pop(key, None)

    def __len__(self) -> int:
        return len(self._entries)


async def cached_fetch(
    cache: TTLCache,
    key: tuple,
    loader: Callable[[], Awaitable[Any]],
) -> Any:
    """Return cache[key], or call loader() and store the result.

    `loader` is a zero-arg async callable (usually a lambda). This keeps the
    caching concern out of the domain modules — they just wrap their list
    call in a lambda.
    """
    hit = cache.get(key)
    if hit is not None:
        return hit
    value = await loader()
    cache.set(key, value)
    return value


def make_lookup_key(resource: str, **params: Any) -> tuple:
    """Build a hashable cache key from a resource name and arbitrary params.

    Params dict values must themselves be hashable. Lists/dicts get coerced to
    sorted tuples so that `{"x": 1, "y": 2}` and `{"y": 2, "x": 1}` produce
    the same key.
    """
    def _normalise(v: Any) -> Any:
        if isinstance(v, dict):
            return tuple(sorted((k, _normalise(val)) for k, val in v.items()))
        if isinstance(v, (list, tuple, set)):
            return tuple(_normalise(x) for x in v)
        return v

    normalised = tuple(sorted((k, _normalise(v)) for k, v in params.items()))
    return (resource, normalised)
