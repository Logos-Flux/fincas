"""Isolated delivery of the lifecycle signals in ``core.plugins.signals``.

Domain code calls :func:`emit` at the one choke point per transition (the
model ``save()`` that persists it). Delivery is deferred with
``transaction.on_commit`` — chosen over a savepoint per receiver because:

- a receiver only ever hears about a fact that committed (a savepoint
  dispatch would announce an order whose transaction later rolls back);
- a receiver runs outside the emitting transaction, so it cannot poison or
  abort it — in Postgres even a *caught* error inside ``atomic`` breaks the
  transaction, which is the failure this module exists to prevent;
- a receiver does not hold the emitter's row locks.

Each receiver still runs in its own ``transaction.atomic()`` so its own
writes are all-or-nothing and one failing receiver leaves the connection
usable for the next one. A raising receiver is logged with
``logger.exception`` (signal, receiver qualname, emitting object's pk) and
never propagates; per-receiver duration is logged at DEBUG.

Calling ``Signal.send()`` directly keeps Django's plain semantics; only
``emit`` adds the isolation.

Data steps that write through live ``save()`` (a backfill reusing a live
helper, ``reconcile_engagements --fix``) run inside :func:`suppressed`, so a
cutover announces nothing: the rule "backfills emit nothing" holds whether a
step uses ``bulk_create`` or not.

Work that does I/O (golden's webhooks, every Phase 6 adapter) is not a
receiver: it is a durable ``core.outbox`` consumer, whose rows ``emit``
writes inside the emitting transaction and ``manage.py process_outbox``
delivers outside any transaction.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import logging
import time
from typing import Any, Iterator

from asgiref.sync import async_to_sync
from django.db import DEFAULT_DB_ALIAS, transaction
from django.dispatch import Signal

from . import signals as _signals

logger = logging.getLogger("golden_erp.plugins.signals")

_suppressed = contextvars.ContextVar("golden_plugin_signals_suppressed", default=False)

_SIGNAL_NAMES = {
    value: name for name, value in vars(_signals).items() if isinstance(value, Signal)
}


def signal_name(signal: Signal) -> str:
    return _SIGNAL_NAMES.get(signal, repr(signal))


def _receiver_name(receiver: Any) -> str:
    target = getattr(receiver, "__func__", receiver)
    module = getattr(target, "__module__", None) or type(target).__module__
    qualname = getattr(target, "__qualname__", None) or type(target).__qualname__
    return f"{module}.{qualname}"


def deliver(signal: Signal, sender: Any, obj: Any, kwargs: dict, using: str) -> None:
    """Call every live receiver now, each isolated (see module docstring)."""
    if not signal.has_listeners(sender):
        return
    name = signal_name(signal)
    pk = getattr(obj, "pk", None)
    sync_receivers, async_receivers = signal._live_receivers(sender)
    receivers = list(sync_receivers) + [async_to_sync(r) for r in async_receivers]
    for receiver in receivers:
        receiver_name = _receiver_name(receiver)
        started = time.perf_counter()
        try:
            with transaction.atomic(using=using):
                receiver(signal=signal, sender=sender, **kwargs)
        except Exception:
            logger.exception(
                "Plugin signal receiver failed: signal=%s receiver=%s object_pk=%s",
                name, receiver_name, pk,
                extra={"signal": name, "receiver": receiver_name, "object_pk": str(pk)},
            )
        finally:
            logger.debug(
                "Plugin signal receiver ran: signal=%s receiver=%s object_pk=%s duration_ms=%.2f",
                name, receiver_name, pk, (time.perf_counter() - started) * 1000,
            )


@contextlib.contextmanager
def suppressed() -> Iterator[None]:
    """Inside this block :func:`emit` sends nothing (backfills, repairs).

    Context-local (``contextvars``), so it never silences another thread or
    request. A signal emitted inside the block is dropped, not deferred.
    """
    token = _suppressed.set(True)
    try:
        yield
    finally:
        _suppressed.reset(token)


def is_suppressed() -> bool:
    return _suppressed.get()


def emit(signal: Signal, sender: Any, obj: Any, **kwargs: Any) -> None:
    """Send ``signal`` once the current transaction commits (immediately in
    autocommit). Dropped if the transaction rolls back, or when called inside
    :func:`suppressed`. ``obj`` is the emitting row: its pk goes into failure
    logs, its DB alias picks the transaction.

    Before deferring the in-process receivers it writes the ``core.outbox``
    rows of every durable consumer listening to ``signal``, inside the
    emitting transaction (docs/crm/phase6-adapters.md 1.3). ``suppressed()``
    silences those too."""
    if _suppressed.get():
        return
    using = getattr(getattr(obj, "_state", None), "db", None) or DEFAULT_DB_ALIAS
    # Durable consumers first: their rows join the emitting transaction
    # (a savepoint; never raises), so a crash after commit loses nothing.
    from core import outbox

    outbox.write_for_signal(signal_name(signal), sender, obj, kwargs, using=using)
    transaction.on_commit(
        functools.partial(deliver, signal, sender, obj, kwargs, using),
        using=using,
        robust=True,
    )
