"""Fiscal-period close/reopen service (Step 1b, ED-6).

Close preconditions: the period must be OPEN, and no DRAFT (unposted) entry
may overlap its date range — closing over drafts would strand them in a
period that rejects writes. In-flight drain is structural: posters resolve
the period fresh from the DB on every ``save()`` and the model guard
re-reads status live, so a post racing the close either lands before it
(committed, included) or is rejected after it. The close itself runs inside
``transaction.atomic()`` with the period row locked.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models import FiscalPeriod, JournalEntry


class PeriodCloseError(Exception):
    """A close/reopen was refused; nothing was changed."""


def _locked(period: FiscalPeriod) -> FiscalPeriod:
    return FiscalPeriod.objects.select_for_update().get(pk=period.pk)


def draft_entries(period: FiscalPeriod):
    """Unposted entries overlapping the period — the close blockers."""
    return JournalEntry.objects.filter(
        company=period.company,
        status=JournalEntry.Status.DRAFT,
        entry_date__gte=period.start_date,
        entry_date__lte=period.end_date,
    )


def close_period(period: FiscalPeriod) -> FiscalPeriod:
    """Close an OPEN period after preconditions. Returns the refreshed row."""
    with transaction.atomic():
        period = _locked(period)
        if period.status != FiscalPeriod.Status.OPEN:
            raise PeriodCloseError(
                f"Cannot close {period}: status is {period.status}, "
                "only OPEN periods can be closed."
            )
        blockers = list(
            draft_entries(period).values_list(
                "entry_number", "source_reference"
            )[:25]
        )
        blocker_names = [number or ref for number, ref in blockers]
        if blocker_names:
            raise PeriodCloseError(
                f"Cannot close {period}: {len(blocker_names)}+ draft entries "
                f"overlap it (e.g. {blocker_names[0]}). Post or void them "
                "first — closing would strand them in a period that rejects "
                "writes."
            )
        period.status = FiscalPeriod.Status.CLOSED
        period.closed_at = timezone.now()
        period.save(update_fields=["status", "closed_at", "updated_at"])
        return period


def reopen_period(period: FiscalPeriod) -> FiscalPeriod:
    """Reopen a CLOSED period. LOCKED periods never reopen through the API."""
    with transaction.atomic():
        period = _locked(period)
        if period.status == FiscalPeriod.Status.LOCKED:
            raise PeriodCloseError(
                f"Cannot reopen {period}: LOCKED periods are year-end final. "
                "A correction belongs in the next open period."
            )
        if period.status != FiscalPeriod.Status.CLOSED:
            raise PeriodCloseError(
                f"Cannot reopen {period}: status is {period.status}, "
                "only CLOSED periods can be reopened."
            )
        period.status = FiscalPeriod.Status.OPEN
        period.closed_at = None
        period.save(update_fields=["status", "closed_at", "updated_at"])
        return period
