"""Keep a demo database's dates current: the demo clock.

A public demo is restored every night from a frozen snapshot, so without help
every date in it ages: tasks drift into "overdue", deals go stale, "this
month" reports empty out. The snapshot therefore carries an **anchor**, the
local date its data was written (or last rolled) for, and
``manage.py demo_roll_dates`` moves every demo date forward by
``today - anchor`` whole days, then records today as the new anchor.
Running it twice on one day shifts nothing.

Anchor storage: an ``AuditLog`` row (``action="demo_roll"``, a fixed
``object_id``), the latest of which holds the anchor in ``changes``. No
migration, and every roll leaves its own audit row (previous anchor, delta,
zone, row counts). AuditLog itself never moves.

What moves: every concrete ``DateField`` / ``DateTimeField`` of every managed
model, ``created_at`` / ``updated_at`` included (a deal created "three days
ago" must stay three days old, and history must stay before the stage it
led to), except what ``EXCLUDED`` lists: credentials and their expiry,
sessions, queue leases, sync watermarks, audit trails and legal records
(consent, suppression, erasure, merges), import ledgers, webhook
subscriptions (their age picks their payload version). Deployments add to
it with ``GOLDEN_DEMO_ROLL_EXCLUDE`` (``"app"``, ``"app.model"`` or
``"app.model.field"``).

Two kinds of rows get special handling instead of a plain shift:

- **All-day tasks** are re-anchored to the end of their local day in
  ``due_tz`` (``crm.tasks.end_of_local_day``): a plain UTC shift across a
  DST change would land them an hour off, i.e. on the wrong day. Engagement
  next actions are then recomputed where they drift.
- **The books.** Fiscal periods (and accounts' opening-balance dates) are
  the accounting calendar and never move.
  Journal entries in a closed or locked period never move (closed books are
  immutable; that is what the period guard is for). Every other entry moves
  and is re-stamped with the period its new date falls in, as a fresh
  posting would be (none when no period covers it). An entry whose new date
  would fall in a closed or locked period stays where it is.

Writes go through ``QuerySet.update()`` / ``bulk_update`` inside one
transaction and ``suppressed()``: no ``save()`` guard, no ``auto_now``, no
lifecycle signal, no outbox row. That is the point (it is a data step, not
business activity), and it is also why this must only ever run on a demo
database: the command refuses unless ``DEMO_USERNAMES`` is set (a public demo
login) or ``--force`` is passed.

Run it right after restoring the snapshot (a demo deployment's nightly
reset does): every row then dates from the anchor. Rows written since (visitors'
work on a live demo) would be pushed into the future.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from django.apps import apps
from django.conf import settings
from django.db import connection, models, transaction
from django.db.models import ExpressionWrapper, F

from core import tz
from core.plugins.dispatch import suppressed

ACTION = "demo_roll"
MODEL_NAME = "core.demo_clock"
#: The fixed ``AuditLog.object_id`` every anchor row carries.
ANCHOR_ID = uuid.uuid5(uuid.NAMESPACE_URL, "golden-erp:demo-clock")

#: What never moves, with the reason. Keys are ``app``, ``app.model`` or
#: ``app.model.field`` (lower case). Special-cased rows (all-day tasks, the
#: books) are listed too, so the generic pass leaves them alone.
EXCLUDED = {
    "admin": "admin action log: an audit trail",
    "sessions": "session expiry: a shifted session would outlive its cookie",
    "auth.user": "last_login / date_joined are account facts, not demo data",
    "core.auditlog": "audit trail; it also holds the anchor",
    "core.outboxevent": "queue leases and retry times: shifting would delay delivery",
    "core.apikey": "credential expiry, revocation and last use",
    "core.useridentity": "login facts",
    "core.integrationaccount": "provider sync state (pauses, last success)",
    "core.integrationcredential": "credential expiry",
    "core.integrationcheck": "audit record of connection tests",
    "core.externalref": "sync watermarks against a provider",
    "llmhub": "LLM settings, their audit and model-cache times",
    "parties.consentrecord": "legal record of consent",
    "parties.suppression": "compliance record",
    "parties.partymergelog": "merge ledger: undo restores exactly what it recorded",
    "parties.party.erased_at": "erasure is a legal record",
    "parties.party.merged_at": "merge ledger",
    "parties.party.marketable_changed_at": "consent change time",
    "orders.salesinvoice.payment_token_expires_at": "a payment link's expiry",
    "crm_import": "import ledger and source locks",
    "unify": "import batches and approvals",
    "webhooks": ("a subscription's created_at picks its payload version (WEBHOOKS_VERSIONING_CUTOFF); "
                 "delivery logs record real attempts"),
    "communications.communicationraw": "raw provider payload fetch time",
    "accounting.fiscalperiod": "the accounting calendar never moves (see the books)",
    "accounting.account.opening_balance_date": "where the books start: part of the calendar",
    "accounting.journalentry": "moved by roll_books, not the generic pass",
    "crm.task.due_at": "moved by roll_tasks (all-day tasks keep their local day)",
}


def excluded_reason(model, field_name=None):
    """Why ``model`` (or one of its fields) never moves, else None."""
    extra = {key.lower(): "GOLDEN_DEMO_ROLL_EXCLUDE"
             for key in getattr(settings, "GOLDEN_DEMO_ROLL_EXCLUDE", ())}
    table = {**EXCLUDED, **extra}
    label = model._meta.label_lower
    keys = [model._meta.app_label, label]
    if field_name:
        keys.append(f"{label}.{field_name}")
    for key in keys:
        if key in table:
            return table[key]
    return None


def date_fields(model):
    """The concrete date and datetime fields of ``model`` that move."""
    return [
        f for f in model._meta.concrete_fields
        if isinstance(f, models.DateField) and excluded_reason(model, f.name) is None
    ]


def rolled_models():
    """``[(model, [fields])]``: everything the generic pass shifts."""
    out = []
    for model in apps.get_models():
        meta = model._meta
        if meta.proxy or not meta.managed or excluded_reason(model):
            continue
        fields = date_fields(model)
        if fields:
            out.append((model, fields))
    return out


def demo_zone():
    """The zone "today" is read in: the deployment's ``TIME_ZONE``."""
    return tz.resolve()


def local_today(zone=None, now=None):
    from django.utils import timezone

    return (now or timezone.now()).astimezone(zone or demo_zone()).date()


# --- the anchor ----------------------------------------------------------------------


def anchor_row():
    """The latest anchor record (its ``changes``), or None.

    Latest by ``created_at``, then ``seq``: two writes inside one clock tick
    (or under a test's frozen clock) share a timestamp.
    """
    from core.models import AuditLog

    rows = AuditLog.objects.filter(action=ACTION, object_id=ANCHOR_ID).values_list("created_at", "changes")
    latest = max(rows, key=lambda r: (r[0], int((r[1] or {}).get("seq", 0))), default=None)
    return latest[1] if latest and (latest[1] or {}).get("anchor") else None


def get_anchor():
    """The stored anchor date, or None when the data has none yet."""
    row = anchor_row()
    return datetime.date.fromisoformat(row["anchor"]) if row else None


def set_anchor(day, **details):
    from core.models import AuditLog

    row = anchor_row()
    seq = int(row.get("seq", 0)) + 1 if row else 1
    AuditLog.objects.create(action=ACTION, model_name=MODEL_NAME, object_id=ANCHOR_ID,
                            changes={"anchor": day.isoformat(), "seq": seq, **details})


def ensure_anchor(day=None, stdout=None):
    """Record ``day`` (default: today) as the anchor unless one exists.

    Seeds call this after writing data relative to now. When the data is
    already anchored to another day the seed's new rows and the old ones
    disagree, so it says so: roll first (``demo_roll_dates``), then seed.
    """
    day = day or local_today()
    current = get_anchor()
    if current is None:
        set_anchor(day, source="seed")
    elif current != day and stdout is not None:
        stdout.write(f"  Demo data is anchored to {current}, not {day}: run demo_roll_dates "
                     "before seeding, or the new rows are out of step.")
    return current or day


# --- the roll ------------------------------------------------------------------------


@dataclass
class RollResult:
    anchor: datetime.date | None
    today: datetime.date
    delta_days: int
    zone: str
    initialised: bool = False
    anchor_ignored: datetime.date | None = None  # --anchor given, but the data's anchor is known
    rows: dict[str, int] = field(default_factory=dict)
    books_kept: int = 0  # journal entries left in place (closed books)


def _shift(field_obj, delta):
    return ExpressionWrapper(F(field_obj.attname) + delta, output_field=field_obj)


def _shift_dates_in_python(model, fields, delta):
    """Date (not datetime) columns, row by row: ``date + interval`` comes
    back as a datetime string on SQLite, which a DateField cannot read."""
    names = [f.attname for f in fields]
    batch, total = [], 0
    for row in model._base_manager.values_list("pk", *names).iterator(chunk_size=1000):
        if all(v is None for v in row[1:]):
            continue
        obj = model(pk=row[0])
        for name, value in zip(names, row[1:], strict=True):
            setattr(obj, name, value + delta if value is not None else None)
        batch.append(obj)
        if len(batch) >= 500:
            model._base_manager.bulk_update(batch, names)
            total += len(batch)
            batch = []
    if batch:
        model._base_manager.bulk_update(batch, names)
        total += len(batch)
    return total


def shift_model(model, fields, delta):
    """Shift ``fields`` of every row of ``model``; returns rows touched."""
    datetimes = [f for f in fields if isinstance(f, models.DateTimeField)]
    dates = [f for f in fields if not isinstance(f, models.DateTimeField)]
    touched = 0
    if datetimes:
        touched = model._base_manager.update(**{f.attname: _shift(f, delta) for f in datetimes})
    if dates:
        touched = max(touched, _shift_dates_in_python(model, dates, delta))
    return touched


def roll_tasks(delta):
    """``Task.due_at``: timed tasks shift; all-day ones stay end-of-local-day."""
    from crm.models import Task
    from crm.tasks import end_of_local_day

    timed = Task._base_manager.filter(due_at__isnull=False, all_day=False).update(
        due_at=_shift(Task._meta.get_field("due_at"), delta))
    batch = []
    fallback = demo_zone()
    for pk, due_at, due_tz in (Task._base_manager.filter(due_at__isnull=False, all_day=True)
                               .values_list("pk", "due_at", "due_tz").iterator(chunk_size=1000)):
        zone = ZoneInfo(due_tz) if tz.is_valid_timezone(due_tz) else fallback
        day = due_at.astimezone(zone).date() + delta
        batch.append(Task(pk=pk, due_at=end_of_local_day(day, zone)))
    Task._base_manager.bulk_update(batch, ["due_at"], batch_size=500)
    return timed + len(batch)


def fix_next_actions():
    """Recompute engagement next actions that drifted (all-day re-anchoring
    can reorder a task against a timed one within the DST hour)."""
    from crm.management.commands.reconcile_engagements import drift
    from crm.tasks import recompute_next_action

    pks = [pk for pk, _stored, _expected in drift()]
    for pk in pks:
        recompute_next_action(pk)
    return len(pks)


def roll_books(delta):
    """Move open-book journal entries; returns ``(moved, kept)``."""
    from accounting.models import FiscalPeriod, JournalEntry

    closed = (FiscalPeriod.Status.CLOSED, FiscalPeriod.Status.LOCKED)
    periods = {}
    for period in FiscalPeriod._base_manager.order_by("-start_date").only(
            "pk", "company_id", "start_date", "end_date", "status"):
        periods.setdefault(period.company_id, []).append(period)

    def period_for(company_id, day):
        # Same rule as accounting.posting._period_for: latest start wins.
        return next((p for p in periods.get(company_id, ()) if p.start_date <= day <= p.end_date), None)

    kept, batch = 0, []
    rows = (JournalEntry._base_manager.exclude(fiscal_period__status__in=closed)
            .values_list("pk", "company_id", "entry_date", "created_at", "updated_at"))
    for pk, company_id, entry_date, created_at, updated_at in rows.iterator(chunk_size=1000):
        day = entry_date + delta
        target = period_for(company_id, day)
        if target is not None and target.status in closed:
            kept += 1
            continue
        batch.append(JournalEntry(pk=pk, entry_date=day, fiscal_period_id=target.pk if target else None,
                                  created_at=created_at + delta, updated_at=updated_at + delta))
    JournalEntry._base_manager.bulk_update(
        batch, ["entry_date", "fiscal_period", "created_at", "updated_at"], batch_size=500)
    kept += JournalEntry._base_manager.filter(fiscal_period__status__in=closed).count()
    return len(batch), kept


def _lock():
    """One roll at a time (two concurrent rolls would both read the old
    anchor and shift twice). A no-op on SQLite, which has one writer."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ["golden.demo_roll"])


def roll(*, today=None, anchor=None, dry_run=False):
    """Shift the demo data so its anchor becomes ``today``.

    With no anchor stored (a baseline taken before the demo clock existed),
    today becomes the anchor and nothing moves; that anchor is a guess
    (``source="init"``), so ``anchor`` may correct it until the first real
    roll. A known anchor (from a seed or a roll) is never overridden:
    ``anchor`` is then ignored (``RollResult.anchor_ignored``), so a stale
    ``--anchor`` left in a script cannot shift the data twice. ``dry_run``
    rolls everything back at the end.
    """
    zone = demo_zone()
    today = today or local_today(zone)

    class _DryRun(Exception):
        pass

    result = None
    try:
        with transaction.atomic(), suppressed():
            _lock()
            row = anchor_row()
            stored = get_anchor()
            ignored = None
            if anchor is not None and row is not None and row.get("source") != "init":
                ignored, anchor = anchor, None
            start = anchor or stored
            if start is None:
                result = RollResult(anchor=None, today=today, delta_days=0, zone=zone.key, initialised=True)
                set_anchor(today, previous_anchor=None, delta_days=0, zone=zone.key, source="init")
            else:
                days = (today - start).days
                if days < 0:
                    raise ValueError(f"The demo data is anchored to {start}, after {today}: "
                                     "refusing to move it backwards.")
                result = RollResult(anchor=start, today=today, delta_days=days, zone=zone.key,
                                    anchor_ignored=ignored)
                if days:
                    delta = datetime.timedelta(days=days)
                    for model, fields in rolled_models():
                        result.rows[model._meta.label] = shift_model(model, fields, delta)
                    result.rows["crm.Task (due_at)"] = roll_tasks(delta)
                    result.rows["crm.Engagement (next action recomputed)"] = fix_next_actions()
                    moved, result.books_kept = roll_books(delta)
                    result.rows["accounting.JournalEntry"] = moved
                if days or start != stored:
                    set_anchor(today, previous_anchor=start.isoformat(), delta_days=days, zone=zone.key,
                               books_kept=result.books_kept, source="roll")
            if dry_run:
                raise _DryRun
    except _DryRun:
        pass
    return result
