"""Run the outbox explicitly in tests and fixture runners.

There is no eager mode: a test that forgets to drain sees the rows, which is
the point. ``drain`` skips the autocommit assertion because a ``TestCase``
wraps every test in a transaction; the assertion itself is tested in a
``TransactionTestCase`` (``core/tests/test_outbox.py``).
"""

from __future__ import annotations

from . import RunResult, process


def drain(consumer: str | list[str] | None = None, *, now=None, max_passes: int = 50) -> RunResult:
    """Claim and handle every due row (``consumer``: one name or a list),
    repeating while handlers enqueue more due rows (the ``webhooks`` row
    expands into ``webhooks.deliver`` rows). Retried rows are not due again
    until their ``next_attempt_at``, so this terminates."""
    names = [consumer] if isinstance(consumer, str) else consumer
    total = RunResult()
    for _ in range(max_passes):
        result = process(names=names, limit=None, now=now, require_autocommit=False)
        total.merge(result)
        if not (result.claimed or result.expired_dead):
            break
    return total
