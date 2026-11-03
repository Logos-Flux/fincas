"""The engagement timeline (docs/crm/phase3-engagement.md 6.3).

``build(engagement, before=None, kinds=None, limit=50)`` merges one bounded
query per source, newest first:

| Source | Item kind | Time |
|---|---|---|
| ``EngagementStageChange`` | ``stage`` | ``changed_at`` |
| ``Communication`` (not a future scheduled one) | ``communication`` | ``occurred_at`` |
| ``Task`` (done) | ``task`` | ``completed_at`` |
| ``Quote`` (sent) | ``document`` | ``sent_at`` |
| ``Order`` | ``document`` | ``order_date`` |
| ``SalesInvoice`` (of the engagement's orders) | ``document`` | ``created_at`` |

Ties break by item kind in that order (stage change, communication, task,
document), then by id, so the order is stable across reloads. Each source is
``ORDER BY time DESC LIMIT limit + 1`` under the cursor, so a page costs a
constant number of queries (six) and needs no database view.

The cursor is ``<ISO time>|<kind>|<id>`` of the last item served
(``next_before`` in the result). ``kinds`` narrows the sources: ``stage``,
``task``, ``document``, or communication kinds (``email``, ``call``, …); an
empty list means all. The first page (no cursor) also returns ``upcoming``:
open tasks due in the future and scheduled communications, soonest first.

Raw provider payloads never appear here; items carry subjects and
summaries, the same as the communication list.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import OPEN_TASK_STATUSES, EngagementStageChange, Task

KIND_ORDER = ("stage", "communication", "task", "document")
RANK = {kind: index for index, kind in enumerate(KIND_ORDER)}
NON_COMMUNICATION_KINDS = frozenset({"stage", "task", "document"})
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
UPCOMING_LIMIT = 20


class CursorError(ValueError):
    """``before`` is not ``<ISO time>|<kind>|<id>``."""


@dataclass(frozen=True)
class Cursor:
    at: datetime.datetime
    kind: str
    id: str

    @classmethod
    def parse(cls, raw: str) -> Cursor:
        parts = (raw or "").split("|")
        if len(parts) != 3:
            raise CursorError("before must be <ISO time>|<kind>|<id>.")
        try:
            at = parse_datetime(parts[0].replace(" ", "+"))
        except ValueError:  # date-shaped but impossible (month 13)
            at = None
        if at is None or parts[1] not in RANK:
            raise CursorError("before must be <ISO time>|<kind>|<id>.")
        try:
            uuid.UUID(parts[2])
        except ValueError as exc:
            raise CursorError("before: the id is not a UUID.") from exc
        if timezone.is_naive(at):
            at = timezone.make_aware(at, datetime.UTC)
        return cls(at, parts[1], parts[2])

    def encode(self) -> str:
        return f"{self.at.isoformat()}|{self.kind}|{self.id}"


@dataclass
class Item:
    kind: str
    at: datetime.datetime
    id: str
    data: dict[str, Any] = field(default_factory=dict)

    def sort_key(self):
        # Newest first, then kind order, then id.
        return (-self.at.timestamp(), RANK[self.kind], self.id)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "at": self.at.isoformat(), "id": self.id, **self.data}


@dataclass
class Page:
    items: list[Item]
    next_before: str | None
    upcoming: list[dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        out = {"items": [i.as_dict() for i in self.items], "next_before": self.next_before}
        if self.upcoming is not None:
            out["upcoming"] = self.upcoming
        return out


def _under_cursor(time_field: str, kind: str, cursor: Cursor | None) -> Q:
    """Rows of a source of ``kind`` that sort strictly after ``cursor``."""
    if cursor is None:
        return Q()
    rank, cursor_rank = RANK[kind], RANK[cursor.kind]
    older = Q(**{f"{time_field}__lt": cursor.at})
    if rank > cursor_rank:
        return older | Q(**{time_field: cursor.at})
    if rank < cursor_rank:
        return older
    return older | Q(**{time_field: cursor.at, "pk__gt": cursor.id})


def _wanted(kinds) -> tuple[bool, bool, bool, set[str] | None]:
    """``(stage, task, document, communication kinds or None for all)``."""
    if not kinds:
        return True, True, True, None
    kinds = set(kinds)
    comm = kinds - NON_COMMUNICATION_KINDS
    return "stage" in kinds, "task" in kinds, "document" in kinds, comm


def build(engagement, *, before: str | None = None, kinds=None, limit: int = DEFAULT_LIMIT,
          now=None) -> Page:
    """One page of ``engagement``'s timeline (module docstring)."""
    from communications.models import Communication
    from orders.models import Order, Quote, SalesInvoice

    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    cursor = Cursor.parse(before) if before else None
    now = now or timezone.now()
    want_stage, want_task, want_doc, comm_kinds = _wanted(kinds)
    take = limit + 1
    items: list[Item] = []

    if want_stage:
        rows = (
            EngagementStageChange._base_manager.filter(engagement=engagement)
            .filter(_under_cursor("changed_at", "stage", cursor))
            .select_related("from_stage", "to_stage", "changed_by")
            .order_by("-changed_at", "pk")[:take]
        )
        for r in rows:
            items.append(Item("stage", r.changed_at, str(r.pk), {
                "from_stage": r.from_stage.key if r.from_stage_id else None,
                "from_stage_name": r.from_stage.name if r.from_stage_id else None,
                "to_stage": r.to_stage.key if r.to_stage_id else None,
                "to_stage_name": r.to_stage.name if r.to_stage_id else None,
                "from_status": r.from_status, "to_status": r.to_status,
                "by": str(r.changed_by_id) if r.changed_by_id else None,
                "by_name": r.changed_by.display_name if r.changed_by_id else None,
                "source": r.source, "note": r.note,
            }))

    if comm_kinds is None or comm_kinds:
        qs = Communication._base_manager.filter(engagement=engagement).exclude(
            status=Communication.Status.SCHEDULED, occurred_at__gt=now)
        if comm_kinds:
            qs = qs.filter(kind__in=comm_kinds)
        rows = (
            qs.filter(_under_cursor("occurred_at", "communication", cursor))
            .select_related("performed_by").order_by("-occurred_at", "pk")[:take]
        )
        for r in rows:
            items.append(Item("communication", r.occurred_at, str(r.pk), {
                "communication_kind": r.kind, "direction": r.direction, "status": r.status,
                "subject": r.subject, "summary": r.summary, "thread_key": r.thread_key,
                "party": str(r.party_id) if r.party_id else None,
                "by": str(r.performed_by_id) if r.performed_by_id else None,
                "by_name": r.performed_by.display_name if r.performed_by_id else None,
            }))

    if want_task:
        rows = (
            Task._base_manager.filter(engagement=engagement, status=Task.Status.DONE,
                                      completed_at__isnull=False)
            .filter(_under_cursor("completed_at", "task", cursor))
            .select_related("completed_by").order_by("-completed_at", "pk")[:take]
        )
        for r in rows:
            items.append(Item("task", r.completed_at, str(r.pk), {
                "title": r.title, "task_kind": r.kind,
                "by": str(r.completed_by_id) if r.completed_by_id else None,
                "by_name": r.completed_by.display_name if r.completed_by_id else None,
            }))

    if want_doc:
        sources = (
            (Quote._base_manager.filter(engagement=engagement, sent_at__isnull=False), "sent_at",
             lambda q: {"document_type": "quote", "event": "sent", "number": q.quote_number,
                        "status": q.status, "total": str(q.grand_total), "currency": q.currency}),
            (Order._base_manager.filter(engagement=engagement), "order_date",
             lambda o: {"document_type": "sales_order", "event": "placed", "number": o.order_number,
                        "status": o.status, "total": str(o.grand_total)}),
            (SalesInvoice._base_manager.filter(order__engagement=engagement), "created_at",
             lambda i: {"document_type": "invoice", "event": "issued", "number": i.invoice_number,
                        "status": i.status, "total": str(i.grand_total),
                        "invoice_date": i.invoice_date.isoformat() if i.invoice_date else None}),
        )
        for qs, time_field, describe in sources:
            rows = qs.filter(_under_cursor(time_field, "document", cursor)).order_by(
                f"-{time_field}", "pk")[:take]
            for r in rows:
                items.append(Item("document", getattr(r, time_field), str(r.pk), describe(r)))

    items.sort(key=Item.sort_key)
    page = items[:limit]
    next_before = None
    if len(items) > limit:
        last = page[-1]
        next_before = Cursor(last.at, last.kind, last.id).encode()
    upcoming = None if cursor is not None else upcoming_for(engagement, now=now)
    return Page(page, next_before, upcoming)


def upcoming_for(engagement, *, now=None) -> list[dict[str, Any]]:
    """Open tasks due in the future and scheduled communications, soonest first."""
    from communications.models import Communication

    now = now or timezone.now()
    tasks = (
        Task._base_manager.filter(engagement=engagement, status__in=OPEN_TASK_STATUSES,
                                  due_at__gt=now)
        .order_by("due_at", "pk")[:UPCOMING_LIMIT]
    )
    comms = (
        Communication._base_manager.filter(engagement=engagement,
                                           status=Communication.Status.SCHEDULED,
                                           occurred_at__gt=now)
        .order_by("occurred_at", "pk")[:UPCOMING_LIMIT]
    )
    out = [{"kind": "task", "at": t.due_at.isoformat(), "id": str(t.pk), "title": t.title,
            "task_kind": t.kind, "assignee": str(t.assignee_id) if t.assignee_id else None,
            "all_day": t.all_day, "due_tz": t.due_tz} for t in tasks]
    out += [{"kind": "communication", "at": c.occurred_at.isoformat(), "id": str(c.pk),
             "communication_kind": c.kind, "subject": c.subject,
             "party": str(c.party_id) if c.party_id else None} for c in comms]
    out.sort(key=lambda row: (row["at"], row["id"]))
    return out[:UPCOMING_LIMIT]
