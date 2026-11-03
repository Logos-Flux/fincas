"""The transactional outbox (docs/crm/phase6-adapters.md section 1).

Work that must not be lost is a ``core.OutboxEvent`` row written inside the
transaction that made the fact: a rollback drops it, a crash after commit
loses nothing. ``manage.py process_outbox`` (:func:`process`) claims due rows
with a conditional UPDATE and a lease, runs the consumer's handler with **no
transaction open**, retries with capped exponential backoff and jitter, and
dead-letters after ``max_attempts``. Delivery is at-least-once, so every
consumer is idempotent (its idempotency keys name the fact).

Rows come from two places:

- **Signals.** ``core.plugins.dispatch.emit`` calls :func:`write_for_signal`
  before it defers the in-process receivers: every consumer listening to the
  signal ``build``s its specs, and the rows are inserted in a savepoint of the
  emitting transaction. A raising ``build`` is logged and skipped; the outbox
  never fails the save that emitted.
- **Explicit** :func:`enqueue` (an erasure request, an inbound webhook, a push
  asked for from a view).

``core.plugins.dispatch.suppressed()`` silences both (an explicit enqueue
passes ``ignore_suppression=True`` to write anyway). :func:`origin` marks
writes that came *from* a provider: that provider's own consumers skip the
resulting events, everyone else still hears them.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import logging
import os
import random
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, IntegrityError, transaction
from django.db.models import Count, F, Min, Q
from django.utils import timezone

logger = logging.getLogger("golden_erp.outbox")

#: Backoff: ``min(BASE × 2^(attempts−1), CAP)`` seconds, ±``JITTER``.
BACKOFF_BASE_S = 30
BACKOFF_CAP_S = 6 * 3600
BACKOFF_JITTER = 0.2
DEFAULT_MAX_ATTEMPTS = 10
DEFAULT_LEASE_S = 60
LAST_ERROR_MAX = 500


# --- outcomes ----------------------------------------------------------------


@dataclass(frozen=True)
class Done:
    """The work is finished (state ``done``)."""


@dataclass(frozen=True)
class Retry:
    """Try again later: after ``after`` (seconds or a timedelta, e.g. a
    provider's ``Retry-After``), else after the backoff for this attempt.
    At ``max_attempts`` a Retry becomes ``dead``."""

    after: Any = None
    error: str = ""


@dataclass(frozen=True)
class Dead:
    """Give up now (state ``dead``): a 4xx, a payload the provider refuses."""

    error: str = ""


@dataclass(frozen=True)
class Cancel:
    """Nothing to do any more (state ``cancelled``): the target was removed
    or switched off after the row was written. Not an alarm."""

    error: str = ""


# --- consumers ---------------------------------------------------------------


@dataclass(frozen=True)
class EventSpec:
    """One row a consumer wants written."""

    idempotency_key: str
    payload: dict
    event: str = ""
    company_id: Any = None
    object_type: str = ""
    object_id: Any = None
    party_id: Any = None
    coalesce_key: str = ""
    occurred_at: Any = None


@dataclass(frozen=True)
class OutboxConsumer:
    """A durable consumer. Registered in code (:func:`register`, or a plugin's
    ``get_outbox_consumers()``), never in settings.

    ``build(signal_name, sender, obj, kwargs) -> list[EventSpec]`` runs inside
    the business transaction: no network, at most a query or two.
    ``handle(event) -> Done | Retry | Dead | Cancel`` runs with no transaction
    open and never sleeps (waiting is ``next_attempt_at``).
    """

    name: str
    handle: Callable
    signals: tuple[str, ...] = ()
    build: Callable | None = None
    # ``handle_batch(events) -> [outcome, ...]`` (one per event, same order):
    # up to ``batch_size`` claimed rows sharing ``batch_key(event)`` go to one
    # call (the Google Ads upload, docs/crm/phase6-adapters.md 3.5). A raising
    # batch handler retries every row of the batch.
    handle_batch: Callable | None = None
    batch_key: Callable | None = None
    batch_size: int = 100
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    lease_seconds: int = DEFAULT_LEASE_S
    provider: str = ""  # adapter consumers: the echo rule (origin())
    # ``() -> bool``, checked before ``build`` opens its savepoint: a consumer
    # that is switched off (webhooks without WEBHOOKS_DELIVER_ON_SIGNALS, an
    # adapter with no enabled account) then costs a save no query at all.
    enabled: Callable | None = None


_builtin: dict[str, tuple[OutboxConsumer, str]] = {}


def register(consumer: OutboxConsumer, owner: str) -> None:
    """Register a core / first-party consumer (from an ``AppConfig.ready()``).
    Re-registering the same name by the same owner replaces it (autoreload);
    another owner fails startup naming both."""
    if not isinstance(consumer, OutboxConsumer):
        raise ImproperlyConfigured(f"{owner} registers {consumer!r}; expected a core.outbox.OutboxConsumer.")
    current = _builtin.get(consumer.name)
    if current is not None and current[1] != owner:
        raise ImproperlyConfigured(
            f"Duplicate outbox consumer {consumer.name!r}: {current[1]} and {owner} both claim it. "
            "Rename one: a row must never silently reach another owner's handler."
        )
    _builtin[consumer.name] = (consumer, owner)


_cache: dict[str, Any] = {"key": None, "value": None}


def consumers() -> dict[str, OutboxConsumer]:
    """Every registered consumer: core's, then each plugin's. A name claimed
    twice raises ``ImproperlyConfigured`` naming both owners (also reported
    by the ``core.E050`` system check at startup).

    Cached until the builtin registrations change or the plugin registry
    reloads (``emit`` calls this on every save). Builtins are keyed by the
    ids of their consumers, which the cached value keeps alive, so an id
    cannot be reused while it is in the key. Plugins are keyed by
    ``plugin_registry.generation``, never by ``id()`` of the instances: a
    reload (``override_settings(GOLDEN_PLUGINS=...)`` in tests) frees the old
    instances, CPython hands their ids to the new ones, and an id-keyed cache
    then served the previous plugin set's consumers (the full suite's
    "No outbox consumer named 'gmail.inbound'")."""
    from core.plugins.registry import plugin_registry

    plugins = plugin_registry.plugins
    key = (tuple((name, id(pair[0])) for name, pair in _builtin.items()), plugin_registry.generation)
    if _cache["key"] == key:
        return _cache["value"]
    value = _collect(plugins)
    _cache.update(key=key, value=value)
    return value


def _collect(plugins) -> dict[str, OutboxConsumer]:
    found = {name: pair for name, pair in _builtin.items()}
    for plugin in plugins.values():
        for consumer in plugin.get_outbox_consumers() or []:
            owner = f"plugin {plugin.name!r}"
            if not isinstance(consumer, OutboxConsumer):
                raise ImproperlyConfigured(
                    f"{owner} returns {consumer!r} from get_outbox_consumers(); expected an OutboxConsumer."
                )
            if consumer.name in found:
                raise ImproperlyConfigured(
                    f"Duplicate outbox consumer {consumer.name!r}: {found[consumer.name][1]} and {owner} "
                    "both claim it. Rename one."
                )
            found[consumer.name] = (consumer, owner)
    return {name: pair[0] for name, pair in found.items()}


def get_consumer(name: str) -> OutboxConsumer | None:
    return consumers().get(name)


# --- context: origin ---------------------------------------------------------

_origin = contextvars.ContextVar("golden_outbox_origin", default="")


@contextlib.contextmanager
def origin(provider: str) -> Iterator[None]:
    """Writes inside this block came from ``provider`` (an inbound sync):
    consumers whose ``provider`` is the same skip the resulting signals, so
    golden never echoes a change back to where it came from. Not
    ``suppressed()``: webhooks and other providers still hear them."""
    token = _origin.set(provider or "")
    try:
        yield
    finally:
        _origin.reset(token)


def current_origin() -> str:
    return _origin.get()


# --- writing -----------------------------------------------------------------


def _uuid_or_none(value) -> uuid.UUID | None:
    value = getattr(value, "pk", value)
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _row(consumer: OutboxConsumer, spec: EventSpec, now):
    from core.models import OutboxEvent

    if not isinstance(spec, EventSpec):
        raise TypeError(f"outbox consumer {consumer.name!r} built {spec!r}; expected an EventSpec")
    return OutboxEvent(
        company_id=_uuid_or_none(spec.company_id),
        consumer=consumer.name,
        event=(spec.event or "")[:64],
        idempotency_key=spec.idempotency_key,
        coalesce_key=spec.coalesce_key or "",
        object_type=(spec.object_type or "")[:64],
        object_id=_uuid_or_none(spec.object_id),
        party_id=_uuid_or_none(spec.party_id),
        payload=spec.payload or {},
        max_attempts=consumer.max_attempts,
        next_attempt_at=now,
        occurred_at=spec.occurred_at or now,
        created_at=now,
    )


def _insert(rows: list, using: str) -> None:
    from core.models import OutboxEvent

    if rows:
        OutboxEvent.objects.using(using).bulk_create(rows, ignore_conflicts=True)


def _suppressed() -> bool:
    from core.plugins.dispatch import is_suppressed

    return is_suppressed()


def write_for_signal(signal_name: str, sender: Any, obj: Any, kwargs: dict, using: str = DEFAULT_DB_ALIAS) -> int:
    """Write the durable rows for one emit (``dispatch.emit`` step 2–3).

    Never raises: a raising ``build`` or a failed insert is logged at ERROR
    (a lost row is a bug to fix, not a design state) and the savepoint keeps
    the emitting transaction usable. Returns the number of rows offered.
    """
    if _suppressed():
        return 0
    try:
        listening = [c for c in consumers().values() if signal_name in c.signals and c.build is not None]
    except Exception:
        logger.exception("Outbox consumer registry failed: signal=%s", signal_name)
        return 0
    if not listening:
        return 0
    here = current_origin()
    now = timezone.now()
    pk = getattr(obj, "pk", None)
    written = 0
    for consumer in listening:
        if consumer.provider and consumer.provider == here:
            continue  # the echo rule (A10)
        try:
            if consumer.enabled is not None and not consumer.enabled():
                continue
        except Exception:
            logger.exception("Outbox enabled() failed: signal=%s consumer=%s", signal_name, consumer.name)
            continue
        # One savepoint per consumer, around build AND insert: a database
        # error inside a build (a lazy FK load, a lookup) would otherwise
        # leave a Postgres transaction aborted even though Python caught it,
        # and one consumer's bad spec must not lose another's rows.
        try:
            with transaction.atomic(using=using):
                specs = consumer.build(signal_name, sender, obj, kwargs) or []
                rows = [_row(consumer, spec, now) for spec in specs]
                _insert(rows, using)
            written += len(rows)
        except Exception:
            logger.exception(
                "Outbox build failed; no row written: signal=%s consumer=%s object_pk=%s",
                signal_name, consumer.name, pk,
                extra={"signal": signal_name, "consumer": consumer.name, "object_pk": str(pk)},
            )
    return written


def enqueue(consumer: str | OutboxConsumer, spec: EventSpec, *, ignore_suppression: bool = False,
            using: str = DEFAULT_DB_ALIAS):
    """Write one row for ``consumer`` in the caller's transaction (or
    autocommit). Respects ``suppressed()`` unless ``ignore_suppression``
    (erasure and inbound webhook receipt only). A duplicate idempotency key
    (or a pending row with the same coalesce key) is a no-op. Returns the
    row, or None when nothing was written."""
    from core.models import OutboxEvent

    if not ignore_suppression and _suppressed():
        return None
    if isinstance(consumer, str):
        found = get_consumer(consumer)
        if found is None:
            raise ImproperlyConfigured(f"No outbox consumer named {consumer!r} is registered.")
        consumer = found
    row = _row(consumer, spec, timezone.now())
    with transaction.atomic(using=using):
        _insert([row], using)
    return OutboxEvent.objects.using(using).filter(consumer=consumer.name, idempotency_key=spec.idempotency_key,
                                                   pk=row.pk).first()


def enqueue_many(consumer: str | OutboxConsumer, specs: Iterable[EventSpec], *, ignore_suppression: bool = False,
                 using: str = DEFAULT_DB_ALIAS) -> int:
    """:func:`enqueue` for several specs in one insert. Returns how many were offered."""
    if not ignore_suppression and _suppressed():
        return 0
    if isinstance(consumer, str):
        found = get_consumer(consumer)
        if found is None:
            raise ImproperlyConfigured(f"No outbox consumer named {consumer!r} is registered.")
        consumer = found
    now = timezone.now()
    rows = [_row(consumer, spec, now) for spec in specs]
    with transaction.atomic(using=using):
        _insert(rows, using)
    return len(rows)


# --- processing --------------------------------------------------------------


def backoff(attempts: int, *, rng: Callable[[], float] | None = None) -> dt.timedelta:
    """The wait after a failed attempt number ``attempts`` (1-based)."""
    rng = rng or random.random
    base = min(BACKOFF_BASE_S * (2 ** max(attempts - 1, 0)), BACKOFF_CAP_S)
    factor = 1 + BACKOFF_JITTER * (2 * rng() - 1)
    return dt.timedelta(seconds=base * factor)


def worker_id() -> str:
    """``host:pid:token``: one per processing run."""
    return f"{socket.gethostname()[:80]}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


@dataclass
class RunResult:
    claimed: int = 0
    done: int = 0
    retried: int = 0
    dead: int = 0
    cancelled: int = 0
    lost_lease: int = 0  # finalize refused: the lease expired and another worker took the row
    expired_dead: int = 0  # running rows whose lease expired at max_attempts
    skipped: int = 0  # candidates another worker claimed first
    by_consumer: dict = field(default_factory=dict)

    def merge(self, other: "RunResult") -> None:
        for name in ("claimed", "done", "retried", "dead", "cancelled", "lost_lease", "expired_dead", "skipped"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for key, value in other.by_consumer.items():
            self.by_consumer[key] = self.by_consumer.get(key, 0) + value


def _due_q(now) -> Q:
    return Q(state="pending", next_attempt_at__lte=now) | Q(state="running", locked_until__lt=now)


def candidates(now, *, names: Iterable[str], limit: int | None = None) -> list[tuple]:
    """``(pk, consumer)`` of due rows, oldest first."""
    from core.models import OutboxEvent

    qs = (OutboxEvent.objects.filter(_due_q(now), consumer__in=list(names))
          .order_by("next_attempt_at", "pk").values_list("pk", "consumer"))
    return list(qs[:limit] if limit else qs)


def expire(pk, now) -> bool:
    """A ``running`` row whose lease expired at ``max_attempts`` goes dead."""
    from core.models import OutboxEvent

    return bool(OutboxEvent.objects.filter(
        pk=pk, state="running", locked_until__lt=now, attempts__gte=F("max_attempts"),
    ).update(state="dead", last_error="lease_expired", completed_at=now, locked_until=None, locked_by=""))


def claim(pk, now, *, worker: str, lease_seconds: int = DEFAULT_LEASE_S) -> bool:
    """Take the row iff it is still due and unleased; True when this caller won.

    One conditional UPDATE (the ``crm.due.claim`` pattern): two workers that
    both saw the row as a candidate race on it, and only the one whose
    UPDATE touched it handles it. On Postgres the loser's UPDATE waits for
    the winner's commit, re-reads the row and matches nothing.
    """
    from core.models import OutboxEvent

    return bool(OutboxEvent.objects.filter(_due_q(now), pk=pk, attempts__lt=F("max_attempts")).update(
        state="running", locked_until=now + dt.timedelta(seconds=lease_seconds), locked_by=worker,
        attempts=F("attempts") + 1,
    ))


def _as_outcome(value) -> Any:
    if isinstance(value, (Done, Retry, Dead, Cancel)):
        return value
    if value is None or value is True:
        return Done()
    return Retry(error=f"bad_outcome:{type(value).__name__}")


def _delay(after) -> dt.timedelta | None:
    if after is None:
        return None
    if isinstance(after, dt.timedelta):
        return after
    return dt.timedelta(seconds=float(after))


def finalize(event, outcome, *, worker: str, now=None) -> str | None:
    """Record ``outcome`` for a row this worker holds. Returns the new state,
    or None when the lease was lost (the row was reclaimed by another worker
    after this one's lease expired; the slow worker writes nothing)."""
    from core.models import OutboxEvent

    now = now or timezone.now()
    error = (getattr(outcome, "error", "") or "")[:LAST_ERROR_MAX]
    fields: dict[str, Any] = {"locked_until": None, "locked_by": ""}
    if isinstance(outcome, Done):
        fields.update(state="done", completed_at=now, last_error="")
    elif isinstance(outcome, Dead):
        fields.update(state="dead", completed_at=now, last_error=error or "dead")
    elif isinstance(outcome, Cancel):
        fields.update(state="cancelled", completed_at=now, last_error=error)
    elif event.attempts >= event.max_attempts:
        fields.update(state="dead", completed_at=now, last_error=error or "max_attempts")
    else:
        wait = _delay(outcome.after) or backoff(event.attempts)
        fields.update(state="pending", next_attempt_at=now + wait, last_error=error)
    mine = OutboxEvent.objects.filter(pk=event.pk, state="running", locked_by=worker)
    try:
        with transaction.atomic():
            touched = mine.update(**fields)
    except IntegrityError:
        # Back to pending while a newer pending row holds the same coalesce
        # key (outbox_coalesce_uniq): that row supersedes this one.
        fields.update(state="cancelled", completed_at=now, last_error="coalesced",
                      next_attempt_at=event.next_attempt_at)
        with transaction.atomic():
            touched = mine.update(**fields)
    return fields["state"] if touched else None


def handle_one(event, consumer: OutboxConsumer, *, worker: str, require_autocommit: bool = True):
    """Run ``consumer.handle`` on a claimed row and finalize it."""
    if require_autocommit and not transaction.get_autocommit():
        raise RuntimeError(
            "core.outbox: handlers must run with no transaction open (docs/crm/phase6-adapters.md 1.5)"
        )
    try:
        outcome = _as_outcome(consumer.handle(event))
    except Exception as exc:  # noqa: BLE001 - a raising handler is a Retry, never a crash
        logger.warning("Outbox handler raised: consumer=%s event=%s row=%s error=%s",
                       consumer.name, event.event, event.pk, type(exc).__name__)
        outcome = Retry(error=type(exc).__name__)
    return finalize(event, outcome, worker=worker)


def handle_batch(events: list, consumer: OutboxConsumer, *, worker: str, require_autocommit: bool = True) -> list:
    """Run ``consumer.handle_batch`` on claimed rows sharing a batch key and
    finalize each with its own outcome."""
    if require_autocommit and not transaction.get_autocommit():
        raise RuntimeError(
            "core.outbox: handlers must run with no transaction open (docs/crm/phase6-adapters.md 1.5)"
        )
    try:
        outcomes = list(consumer.handle_batch(list(events)))
        if len(outcomes) != len(events):
            raise ValueError("batch outcome count")
        outcomes = [_as_outcome(o) for o in outcomes]
    except Exception as exc:  # noqa: BLE001 - a raising batch is a Retry for each row
        logger.warning("Outbox batch handler raised: consumer=%s rows=%s error=%s",
                       consumer.name, len(events), type(exc).__name__)
        outcomes = [Retry(error=type(exc).__name__)] * len(events)
    return [finalize(event, outcome, worker=worker) for event, outcome in zip(events, outcomes)]


def _count(result: RunResult, state) -> None:
    if state is None:
        result.lost_lease += 1
    else:
        key = {"done": "done", "pending": "retried", "dead": "dead", "cancelled": "cancelled"}[state]
        setattr(result, key, getattr(result, key) + 1)


def process(*, names: Iterable[str] | None = None, limit: int | None = 100, worker: str | None = None,
            now=None, require_autocommit: bool = True) -> RunResult:
    """One pass: claim and handle up to ``limit`` due rows. Rows of a batching
    consumer are held per batch key and handled together when the batch is
    full or the pass ends, after their leases are renewed."""
    from core.models import OutboxEvent

    registered = consumers()
    wanted = [n for n in (names or registered) if n in registered]
    unknown = sorted(set(names or ()) - set(registered))
    if unknown:
        logger.warning("process_outbox: no consumer registered for %s; their rows wait", unknown)
    worker = worker or worker_id()
    pinned = now  # tests pin the clock; otherwise every row reads it afresh
    result = RunResult()
    batches: dict[tuple, list] = {}

    def flush(key):
        events = batches.pop(key, [])
        if not events:
            return
        consumer = registered[key[0]]
        # Rows waited in the batch while the pass handled others: renew the
        # lease of every row this worker still holds, and send only those (a
        # row whose lease ran out may already be another worker's).
        flush_now = pinned or timezone.now()
        held = set(OutboxEvent.objects.filter(
            pk__in=[e.pk for e in events], state="running", locked_by=worker,
        ).values_list("pk", flat=True))
        OutboxEvent.objects.filter(pk__in=held, locked_by=worker).update(
            locked_until=flush_now + dt.timedelta(seconds=consumer.lease_seconds))
        lost = [e for e in events if e.pk not in held]
        result.lost_lease += len(lost)
        events = [e for e in events if e.pk in held]
        if events:
            for state in handle_batch(events, consumer, worker=worker, require_autocommit=require_autocommit):
                _count(result, state)

    for pk, name in candidates(pinned or timezone.now(), names=wanted, limit=limit):
        consumer = registered[name]
        # A fresh time per row: a pass can outlast a lease (100 rows × a 10 s
        # timeout), and a lease stamped from the pass's start would already
        # be expired when a late row is claimed.
        row_now = pinned or timezone.now()
        if expire(pk, row_now):
            result.expired_dead += 1
            continue
        if not claim(pk, row_now, worker=worker, lease_seconds=consumer.lease_seconds):
            result.skipped += 1
            continue
        result.claimed += 1
        result.by_consumer[name] = result.by_consumer.get(name, 0) + 1
        event = OutboxEvent.objects.get(pk=pk)
        if consumer.handle_batch is not None:
            try:
                key = (name, consumer.batch_key(event) if consumer.batch_key else None)
            except Exception as exc:  # noqa: BLE001 - a bad batch key retries the row, never crashes the pass
                logger.warning("Outbox batch_key raised: consumer=%s row=%s error=%s",
                               name, event.pk, type(exc).__name__)
                _count(result, finalize(event, Retry(error=type(exc).__name__), worker=worker))
                continue
            batches.setdefault(key, []).append(event)
            if len(batches[key]) >= max(consumer.batch_size, 1):
                flush(key)
            continue
        _count(result, handle_one(event, consumer, worker=worker, require_autocommit=require_autocommit))
    for key in list(batches):
        flush(key)
    return result


def loop(*, names=None, limit: int = 100, max_seconds: float | None = None, idle_sleep: float = 2.0,
         once: bool = False, stop: Callable[[], bool] | None = None) -> RunResult:
    """Repeat :func:`process` until no row is due (``once``), ``max_seconds``
    passed, or ``stop()`` says so; sleep ``idle_sleep`` when a pass claimed
    nothing (``--loop``)."""
    started = time.monotonic()
    total = RunResult()
    while True:
        result = process(names=names, limit=limit)
        total.merge(result)
        if stop is not None and stop():
            break
        if max_seconds is not None and time.monotonic() - started >= max_seconds:
            break
        busy = result.claimed or result.expired_dead
        if not busy:
            if once:
                break
            time.sleep(idle_sleep)
    return total


# --- operations --------------------------------------------------------------


def _settings() -> dict:
    return {"retain_done_days": 7, "retain_inbound_days": 1, "retain_cancelled_days": 7,
            **(getattr(settings, "GOLDEN_OUTBOX", None) or {})}


def prune(now=None) -> dict:
    """Delete finished rows past their retention (``GOLDEN_OUTBOX``). ``dead``
    rows stay until requeued or cancelled."""
    from core.models import OutboxEvent

    now = now or timezone.now()
    conf = _settings()
    inbound = Q(consumer__endswith=".inbound")
    counts = {
        "inbound": OutboxEvent.objects.filter(
            inbound, state="done", completed_at__lt=now - dt.timedelta(days=conf["retain_inbound_days"]),
        ).delete()[0],
        "done": OutboxEvent.objects.filter(
            ~inbound, state="done", completed_at__lt=now - dt.timedelta(days=conf["retain_done_days"]),
        ).delete()[0],
        "cancelled": OutboxEvent.objects.filter(
            state="cancelled", completed_at__lt=now - dt.timedelta(days=conf["retain_cancelled_days"]),
        ).delete()[0],
    }
    return counts


def requeue(*, pks: Iterable[Any] = (), consumer: str | None = None, now=None) -> int:
    """Put ``dead`` rows back to ``pending`` with a fresh attempt budget."""
    from core.models import OutboxEvent

    qs = OutboxEvent.objects.filter(state="dead")
    if pks:
        qs = qs.filter(pk__in=list(pks))
    elif consumer:
        qs = qs.filter(consumer=consumer)
    else:
        return 0
    now = now or timezone.now()
    count = 0
    for pk in list(qs.values_list("pk", flat=True)):
        try:
            with transaction.atomic():
                count += OutboxEvent.objects.filter(pk=pk, state="dead").update(
                    state="pending", attempts=0, next_attempt_at=now, completed_at=None,
                    locked_until=None, locked_by="")
        except IntegrityError:
            # A newer pending row with the same coalesce key supersedes it.
            OutboxEvent.objects.filter(pk=pk, state="dead").update(state="cancelled", last_error="coalesced")
    return count


def cancel(*, pks: Iterable[Any]) -> int:
    """Give up on ``pending`` or ``dead`` rows for good (state ``cancelled``)."""
    from core.models import OutboxEvent

    now = timezone.now()
    return OutboxEvent.objects.filter(pk__in=list(pks), state__in=["pending", "dead"]).update(
        state="cancelled", completed_at=now, last_error="cancelled_by_operator")


def stats(now=None) -> dict:
    """Counts per consumer and state, plus the oldest due pending row."""
    from core.models import OutboxEvent

    now = now or timezone.now()
    counts: dict[str, dict[str, int]] = {}
    for row in OutboxEvent.objects.values("consumer", "state").annotate(n=Count("id")).order_by("consumer"):
        counts.setdefault(row["consumer"], {})[row["state"]] = row["n"]
    # Due rows nobody picked up: their wait starts at next_attempt_at, so a
    # row in normal backoff is not "late".
    oldest_due = OutboxEvent.objects.filter(state="pending", next_attempt_at__lte=now).aggregate(
        oldest=Min("next_attempt_at"))["oldest"]
    stuck = OutboxEvent.objects.filter(state="running", locked_until__lt=now - dt.timedelta(minutes=5)).count()
    try:
        known = set(consumers())
    except ImproperlyConfigured:
        known = set()
    orphaned = sorted(set(counts) - known)
    return {"counts": counts, "oldest_due": oldest_due, "stuck_running": stuck,
            "dead": OutboxEvent.objects.filter(state="dead").count(), "orphaned": orphaned, "now": now}
