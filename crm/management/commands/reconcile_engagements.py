"""``manage.py reconcile_engagements [--check | --fix]`` (spec 1.5, 1.6).

Two kinds of drift:

- **Next action.** ``Engagement.next_task`` / ``next_action_due_at`` are
  denormalised from the earliest open Task. ``Task.save`` and a task delete
  keep them in step; a write around ``save()`` (``QuerySet.update``,
  ``bulk_create``) leaves them stale. ``--fix`` recomputes them.
- **Quotes on threads.** Every quote belongs on an engagement, and every
  revision on its original's. ``Quote.save()`` ensures it; the step-5
  backfill leaves untouched legacy drafts for later, and a write around
  ``save()`` skips it. ``--fix`` runs ``ensure_for_quote`` on each quote
  without one (originals first, so revisions inherit). A revision on a
  different engagement than its original is reported, never moved: which
  thread is right needs a person.

- **Leads, activities and emails on threads** (step 6). A lead with a
  company and a request type other than ``support`` belongs on an engagement
  (intake failed, or a converted lead waited for its quote's engagement);
  every ActivityLog row has its Communication / Task copy; every non-PO
  EmailLog has its Communication, and a document email sits on its
  document's engagement once that exists. ``--fix`` runs the step-6
  backfills (idempotent) after the quotes, then moves those emails.

- **Reconstructed history written as observed** (R11). A legacy quote
  ensured lazily (its next save, or ``--fix`` here) is the deferred end of
  the step-5 backfill, so its creation history row is ``backfill``. Until
  2026-10-17 ``ensure_for_quote`` wrote it as ``system`` with no actor and
  a "Quote <number>" note, which read like an observed change and escaped
  the stage report's footnote. ``--fix`` relabels rows of exactly that
  shape: a creation row, ``system``, no actor, the note naming a quote on
  the engagement that was created more than an hour before the row.

``--check`` (the default) lists the drift and exits 1. The downstream
sequence after ``migrate`` is ``--fix`` then ``--check``: untouched draft
quotes, and the converted leads and emails waiting on them, are fixed there.
"""

from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F, OuterRef, Subquery

from communications.backfill import (
    SKIPPED_TYPES,
    backfill_email_communications,
    link_document_emails,
    unlinked_document_emails,
)
from communications.models import Communication, EmailLog
from core.plugins.dispatch import suppressed
from crm.backfill import backfill_activity_communications, backfill_lead_engagements
from crm.engagements import ensure_for_quote
from crm.intake import SUPPORT
from crm.models import OPEN_TASK_STATUSES, ActivityLog, Engagement, EngagementStageChange, Lead, Task
from crm.tasks import recompute_next_action
from orders.models import Quote

_SAMPLE = 20


def drift():
    """``(engagement_id, stored, expected)`` for every drifting engagement.

    One query: the expected pair is the same ordering as
    ``crm.tasks.next_open_task``, as correlated subqueries.
    """
    earliest = Task._base_manager.filter(
        engagement_id=OuterRef("pk"), status__in=OPEN_TASK_STATUSES
    ).order_by(F("due_at").asc(nulls_last=True), "created_at", "pk")
    rows = (
        Engagement._base_manager.order_by("pk")
        .annotate(expected_task=Subquery(earliest.values("pk")[:1]),
                  expected_due=Subquery(earliest.values("due_at")[:1]))
        .values_list("pk", "next_task_id", "next_action_due_at", "expected_task", "expected_due")
    )
    for pk, task_id, due, expected_task, expected_due in rows.iterator():
        if (task_id, due) != (expected_task, expected_due):
            yield pk, (task_id, due), (expected_task, expected_due)


def quotes_without_engagement():
    """Quote ids with no engagement, originals before their revisions."""
    return list(
        Quote._base_manager.filter(engagement__isnull=True)
        .order_by(F("original_quote_id").asc(nulls_first=True), "revision_number", "created_at", "pk")
        .values_list("pk", flat=True)
    )


def revisions_off_thread():
    """Revisions whose engagement differs from their original's."""
    return list(
        Quote._base_manager.filter(original_quote__isnull=False, engagement__isnull=False,
                                   original_quote__engagement__isnull=False)
        .exclude(engagement_id=F("original_quote__engagement_id"))
        .order_by("pk").values_list("pk", flat=True)
    )


def leads_without_engagement():
    """Leads the intake rule puts on an engagement that have none."""
    return list(
        Lead._base_manager.filter(engagement__isnull=True, company__isnull=False)
        .exclude(request_type=SUPPORT).order_by("created_at", "pk").values_list("pk", flat=True)
    )


def unmirrored_activities():
    """ActivityLog ids with no Communication or Task of the same id."""
    ids = set(ActivityLog._base_manager.filter(company__isnull=False).values_list("pk", flat=True))
    ids -= set(Communication._base_manager.filter(pk__in=ids).values_list("pk", flat=True))
    ids -= set(Task._base_manager.filter(pk__in=ids).values_list("pk", flat=True))
    return sorted(ids, key=str)


def unlinked_email_logs():
    return list(
        EmailLog._base_manager.filter(communication__isnull=True)
        .exclude(document_type__in=SKIPPED_TYPES).order_by("created_at", "pk")
        .values_list("pk", flat=True)
    )


#: A lazily ensured quote predates its engagement's creation row by at
#: least this much; a brand-new quote's row is written in the same second.
LAZY_ENSURE_GAP = datetime.timedelta(hours=1)


def mislabelled_lazy_history():
    """Creation history rows ``ensure_for_quote`` wrote as ``system`` for a
    legacy quote (the shape in the module docstring): ``[pk, ...]``."""
    Source = EngagementStageChange.Source
    rows = (
        EngagementStageChange._base_manager
        .filter(source=Source.SYSTEM, changed_by__isnull=True, from_stage__isnull=True,
                from_status="", note__startswith="Quote ", engagement__origin=Engagement.Origin.QUOTE)
        .values_list("pk", "engagement_id", "note", "changed_at")
    )
    out = []
    for pk, engagement_id, note, changed_at in rows:
        number = note[len("Quote "):].strip()
        created = (Quote._base_manager.filter(engagement_id=engagement_id, quote_number=number)
                   .values_list("created_at", flat=True).first())
        if created is not None and created + LAZY_ENSURE_GAP < changed_at:
            out.append(pk)
    return out


def fix_lazy_history(pks) -> int:
    """Relabel the rows as ``backfill`` (a repair: QuerySet.update, no signals)."""
    if not pks:
        return 0
    return EngagementStageChange._base_manager.filter(pk__in=pks).update(
        source=EngagementStageChange.Source.BACKFILL)


def fix_quotes(pks) -> int:
    fixed = 0
    for pk in pks:
        quote = Quote._base_manager.select_related("company", "customer").get(pk=pk)
        if quote.engagement_id is not None:  # a revision fixed via its original
            continue
        with transaction.atomic():
            eng = ensure_for_quote(quote)
            Quote._base_manager.filter(pk=pk, engagement__isnull=True).update(engagement=eng)
        fixed += 1
    return fixed


class Command(BaseCommand):
    help = ("Report (and with --fix repair) stale engagement next actions, and quotes, leads, "
            "activities and emails not on their thread.")

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--check", action="store_true", help="Report drift; exit 1 if any (default)")
        mode.add_argument("--fix", action="store_true",
                          help="Recompute every drifting engagement; ensure quotes have one")

    def handle(self, *args, **opts):
        rows = list(drift())
        unlinked = quotes_without_engagement()
        if opts["fix"]:
            # A repair, like a backfill: it announces nothing (spec 7,
            # "signal noise at cutover"), even where it writes through save().
            with suppressed():
                self._fix(unlinked)
            rows, unlinked = list(drift()), quotes_without_engagement()
        self._report(rows, unlinked)

    def _fix(self, unlinked):
        fixed_quotes = fix_quotes(unlinked)
        relabelled = fix_lazy_history(mislabelled_lazy_history())
        # Quotes first: ensuring one can create an engagement a converted
        # lead and a quote email then join. Then the step-6 backfills.
        leads = backfill_lead_engagements()
        activities = backfill_activity_communications()
        emails = backfill_email_communications()
        moved = link_document_emails()
        rows = list(drift())
        with transaction.atomic():
            for pk, _, _ in rows:
                recompute_next_action(pk)
        linked = leads.attached_to_quote + leads.attached_repeat + sum(leads.created.values())
        self.stdout.write(
            f"Fixed: {len(rows)} engagement(s), {fixed_quotes} quote(s), {linked} lead(s), "
            f"{sum(activities.communications.values()) + activities.tasks} activity row(s), "
            f"{sum(emails.linked.values())} email log(s), {moved} email(s) moved onto "
            f"their document's engagement, {relabelled} reconstructed history row(s) relabelled"
        )

    def _report(self, rows, unlinked):
        for pk, stored, expected in rows[:_SAMPLE]:
            self.stdout.write(f"  {pk}: next_task {stored[0]} due {stored[1]} -> {expected[0]} due {expected[1]}")
        if len(rows) > _SAMPLE:
            self.stdout.write(f"  ... {len(rows) - _SAMPLE} more")
        self.stdout.write(f"drift: {len(rows)} engagement(s)")
        self._sample("quote without an engagement", unlinked)
        off_thread = revisions_off_thread()
        self._sample("revision on another engagement than its original (not auto-fixed)", off_thread)
        leads = leads_without_engagement()
        self._sample("lead without an engagement (not a support request)", leads)
        activities = unmirrored_activities()
        self._sample("ActivityLog row without its Communication / Task copy", activities)
        emails = unlinked_email_logs()
        self._sample("EmailLog without a communication", emails)
        off_emails = [comm.pk for comm, _eng in unlinked_document_emails()]
        self._sample("document email off its document's engagement", off_emails)
        lazy_rows = mislabelled_lazy_history()
        self._sample("reconstructed history row written as system (lazy ensure)", lazy_rows)
        problems = []
        if rows:
            problems.append(f"{len(rows)} engagement(s) with a stale next action")
        if unlinked:
            problems.append(f"{len(unlinked)} quote(s) without an engagement")
        if off_thread:
            problems.append(f"{len(off_thread)} revision(s) off their chain's engagement")
        if leads:
            problems.append(f"{len(leads)} lead(s) without an engagement")
        if activities:
            problems.append(f"{len(activities)} ActivityLog row(s) not mirrored")
        if emails:
            problems.append(f"{len(emails)} EmailLog(s) without a communication")
        if off_emails:
            problems.append(f"{len(off_emails)} document email(s) off their engagement")
        if lazy_rows:
            problems.append(f"{len(lazy_rows)} reconstructed history row(s) written as system")
        if problems:
            raise CommandError(
                "; ".join(problems) + ". Run `manage.py reconcile_engagements --fix` "
                "(revisions off their chain need a person).", returncode=1,
            )

    def _sample(self, what, pks):
        for pk in pks[:_SAMPLE]:
            self.stdout.write(f"  {pk}")
        if len(pks) > _SAMPLE:
            self.stdout.write(f"  ... {len(pks) - _SAMPLE} more")
        self.stdout.write(f"{what}: {len(pks)}")
