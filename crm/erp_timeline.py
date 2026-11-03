"""The timeline as the back office shows it (docs/crm/phase3-engagement.md 6.3).

``crm.timeline.build`` returns one ordered, cursor-paged page of items (the
API serves it as is). This module adds what only the screen needs:

- **Grouping by day** in the viewer's zone ("Today", "Yesterday", then dates).
- Inside a day, communications sharing a ``thread_key`` collapse into one row
  ("3 emails in 'Quote QT-00123'"), expandable in place; kinds with
  ``counts_as_activity=False`` (marketing sends) collapse into one muted row
  per day; stage changes and golden document events are compact lines.
- Participants as party links, the body behind "expand", and a "View source"
  link for users with ``communications.view_raw_communication``.

Every lookup is one query over the page's ids, so a page costs a constant
number of queries whatever its length: the six sources and the two upcoming
reads of ``build`` plus participants, bodies and (with the permission) raw
sidecars.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from django.urls import NoReverseMatch, reverse
from django.utils.dateparse import parse_datetime

from communications.kinds import kind_or_unknown

from .registries import TASK_KINDS
from .timeline import build

# Kind filter chips (spec 6.3): label, ``kinds`` value.
KIND_CHIPS = (
    ("All", ""), ("Emails", "email"), ("Calls", "call"), ("Meetings", "meeting"), ("Notes", "note"),
    ("Tasks", "task"), ("Stage", "stage"), ("Documents", "document"),
)
DOCUMENT_ROUTES = {"quote": "erp-quote-detail", "sales_order": "erp-order-edit", "invoice": "erp-invoice-detail"}
DOCUMENT_NOUNS = {"quote": "Quote", "sales_order": "Order", "invoice": "Invoice"}
DIRECTION_ICONS = {"inbound": "fas fa-arrow-down-long", "outbound": "fas fa-arrow-up-long"}
# The back office ships a 25-glyph Tabler subset plus Font Awesome, so the
# built-in kinds render with Font Awesome; a deployment's own kind keeps its
# registry icon (its ``ti ti-*`` glyph is the deployment's to ship).
KIND_ICONS = {
    "email": "fas fa-envelope", "sms": "fas fa-comment-sms", "call": "fas fa-phone",
    "meeting": "fas fa-calendar", "linkedin": "fab fa-linkedin", "note": "fas fa-note-sticky",
    "todo": "fas fa-square-check", "follow_up": "fas fa-reply", "unknown": "fas fa-comment",
}
GENERIC_COMMUNICATION_ICON = "fas fa-comment"
GENERIC_TASK_ICON = "fas fa-square-check"


def icon_for(name, registry_icon="", default=GENERIC_COMMUNICATION_ICON):
    return KIND_ICONS.get(name) or registry_icon or default
DIRECTION_WORDS = {"inbound": "Inbound", "outbound": "Outbound", "internal": "Internal"}


@dataclass
class Row:
    type: str  # communication / thread / muted / stage / task / document
    at: datetime.datetime
    icon: str = ""
    title: str = ""
    detail: str = ""
    link: str = ""
    by: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Day:
    label: str
    date: datetime.date
    rows: list[Row] = field(default_factory=list)


def day_label(day: datetime.date, today: datetime.date) -> str:
    if day == today:
        return "Today"
    if day == today - datetime.timedelta(days=1):
        return "Yesterday"
    if day.year == today.year:
        return f"{day:%a %b} {day.day}"
    return f"{day:%a %b} {day.day}, {day.year}"


def _doc_link(data) -> str:
    route = DOCUMENT_ROUTES.get(data.get("document_type"))
    if not route or not data.get("_pk"):
        return ""
    try:
        return reverse(route, args=[data["_pk"]])
    except NoReverseMatch:
        return ""


def _stage_title(data) -> str:
    to_name = data.get("to_stage_name") or ""
    from_name = data.get("from_stage_name") or ""
    before, after = data.get("from_status") or "", data.get("to_status") or ""
    if not before:
        return f"Created at {to_name}" if to_name else f"Created as {after}"
    if after == "won" and before != "won":
        return f"Won at {to_name}" if to_name else "Won"
    if after == "lost" and before != "lost":
        return f"Lost at {to_name}" if to_name else "Lost"
    if after == "open" and before != "open":
        return f"Reopened at {to_name}" if to_name else "Reopened"
    return f"Moved {from_name or '—'} → {to_name or '—'}"


def _extras(items, *, can_view_raw: bool):
    """Participants, bodies and raw-sidecar ids for the page's communications."""
    from communications.models import Communication, CommunicationParticipant, CommunicationRaw

    ids = [i.id for i in items if i.kind == "communication"]
    if not ids:
        return {}, {}, set()
    participants: dict[str, list] = {}
    for row in (CommunicationParticipant.objects.filter(communication_id__in=ids)
                .select_related("party").order_by("role", "display_name", "pk")):
        participants.setdefault(str(row.communication_id), []).append(row)
    bodies = {
        str(pk): body for pk, body in
        Communication._base_manager.filter(pk__in=ids).values_list("pk", "body_text")
    }
    raw = set()
    if can_view_raw:
        raw = {str(pk) for pk in CommunicationRaw.objects.filter(communication_id__in=ids)
               .values_list("communication_id", flat=True)}
    return participants, bodies, raw


def _communication(item, participants, bodies, raw) -> dict[str, Any]:
    data = item.data
    entry = kind_or_unknown(data.get("communication_kind") or "")
    people = []
    for p in participants.get(item.id, []):
        name = p.party.display_name if p.party_id else (p.display_name or p.address_normalized or p.address_raw)
        people.append({"name": name, "party_id": p.party_id, "role": p.role})
    return {
        "id": item.id, "at": item.at, "kind": data.get("communication_kind"),
        "kind_label": entry.label, "icon": icon_for(entry.name, entry.icon),
        "direction": data.get("direction"),
        "direction_icon": DIRECTION_ICONS.get(data.get("direction"), ""),
        "direction_word": DIRECTION_WORDS.get(data.get("direction"), ""),
        "status": data.get("status"), "subject": data.get("subject") or "",
        "summary": data.get("summary") or "", "body": bodies.get(item.id, ""),
        "participants": people, "by": data.get("by_name") or "",
        "has_raw": item.id in raw, "counts_as_activity": entry.counts_as_activity,
    }


def group(items, *, zone, today, participants=None, bodies=None, raw=None, doc_pks=None) -> list[Day]:
    """Bucket ``items`` (already newest first) into days and collapse rows."""
    participants, bodies, raw = participants or {}, bodies or {}, raw or set()
    days: list[Day] = []
    for item in items:
        local = item.at.astimezone(zone)
        if not days or days[-1].date != local.date():
            days.append(Day(day_label(local.date(), today), local.date()))
        day = days[-1]
        data = item.data
        if item.kind == "communication":
            comm = _communication(item, participants, bodies, raw)
            if not comm["counts_as_activity"]:
                muted = next((r for r in day.rows if r.type == "muted"), None)
                if muted is None:
                    muted = Row("muted", item.at, icon="fas fa-bullhorn")
                    day.rows.append(muted)
                muted.items.append(comm)
                muted.title = f"{len(muted.items)} marketing message{'s' if len(muted.items) != 1 else ''}"
                continue
            key = data.get("thread_key") or ""
            thread = next((r for r in day.rows if r.type == "thread" and r.extra.get("key") == key), None) \
                if key else None
            if thread is not None:
                thread.items.append(comm)
                continue
            day.rows.append(Row("thread" if key else "communication", item.at, icon=comm["icon"],
                                items=[comm], extra={"key": key}))
        elif item.kind == "stage":
            day.rows.append(Row("stage", item.at, icon="fas fa-code-commit", title=_stage_title(data),
                                detail=data.get("note") or "", by=data.get("by_name") or ""))
        elif item.kind == "task":
            entry = TASK_KINDS.get(data.get("task_kind") or "")
            day.rows.append(Row("task", item.at, icon=icon_for(data.get("task_kind"), entry.icon if entry else "",
                                                                GENERIC_TASK_ICON),
                                title=f"Completed: {data.get('title') or ''}", by=data.get("by_name") or ""))
        elif item.kind == "document":
            noun = DOCUMENT_NOUNS.get(data.get("document_type"), "Document")
            data = {**data, "_pk": item.id}
            day.rows.append(Row("document", item.at, icon="fas fa-file-lines",
                                title=f"{noun} {data.get('number') or ''} {data.get('event') or ''}".strip(),
                                detail=f"{data.get('status') or ''} · {data.get('total') or ''}".strip(" ·"),
                                link=_doc_link(data)))
    for day in days:
        for row in day.rows:
            if row.type == "thread":
                first = row.items[-1]  # oldest in the day names the thread
                count = len(row.items)
                noun = first["kind_label"].lower()
                row.title = (f"{count} {noun}{'s' if count != 1 and not noun.endswith('s') else ''} in "
                             f"'{first['subject'] or 'no subject'}'") if count > 1 else ""
    return days


def upcoming_rows(page, zone) -> list[dict[str, Any]]:
    """The pinned "Upcoming" group: future open tasks and scheduled meetings."""
    rows = []
    for row in page.upcoming or []:
        at = parse_datetime(row["at"])
        if row["kind"] == "task":
            entry = TASK_KINDS.get(row.get("task_kind") or "")
            rows.append({"at": at, "title": row.get("title") or "", "all_day": row.get("all_day"),
                         "icon": icon_for(row.get("task_kind"), entry.icon if entry else "", GENERIC_TASK_ICON),
                         "noun": entry.label if entry else "Task"})
        else:
            entry = kind_or_unknown(row.get("communication_kind") or "")
            rows.append({"at": at, "title": row.get("subject") or entry.label, "all_day": False,
                         "icon": icon_for(entry.name, entry.icon), "noun": entry.label})
    return rows


def page_for(engagement, *, zone, today, before=None, kinds=None, limit=50, can_view_raw=False):
    """``(page, days, upcoming)`` for one engagement (``CursorError`` on a bad cursor)."""
    page = build(engagement, before=before, kinds=kinds or None, limit=limit)
    participants, bodies, raw = _extras(page.items, can_view_raw=can_view_raw)
    days = group(page.items, zone=zone, today=today, participants=participants, bodies=bodies, raw=raw)
    return page, days, upcoming_rows(page, zone)
