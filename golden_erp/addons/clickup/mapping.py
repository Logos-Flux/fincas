"""ClickUp task <-> golden ``crm.Task`` values: pure functions, no database, no I/O.

The table these implement is docs/crm/phase6-adapters.md 3.1. Golden values
travel between the outbox and these functions as a plain dict
(:func:`golden_values`): ``title``, ``description``, ``status``,
``priority``, ``due_at`` (ISO 8601 UTC or None), ``all_day`` and
``assignee`` (a UserProfile pk as a string, or None). An outbound row pins
exactly the keys a golden save wrote.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import values

#: Task columns ClickUp owns, as ``task_updated`` names them -> the golden value keys they carry.
SYNCED_COLUMNS = {
    "title": ("title",),
    "description": ("description",),
    "status": ("status",),
    "priority": ("priority",),
    "due_at": ("due_at", "all_day"),
    "all_day": ("due_at", "all_day"),
    "assignee": ("assignee",),
}


# -- time -------------------------------------------------------------------------


def ms_to_dt(value: Any) -> dt.datetime | None:
    """ClickUp's millisecond timestamps (strings or ints) -> aware UTC datetime."""
    if value in (None, ""):
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC)


def dt_to_ms(value: dt.datetime | None) -> int | None:
    if value is None:
        return None
    return int(value.timestamp() * 1000)


def ms(value: Any) -> int:
    """A ClickUp timestamp as an int (0 when absent), for ordering."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def iso(value: dt.datetime | None) -> str | None:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z") if value is not None else None


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


# -- golden side -------------------------------------------------------------------


def golden_values(task, columns=None) -> dict:
    """The ClickUp-owned values of ``task``, restricted to what ``columns`` wrote (None = all)."""
    keys = set()
    for column in (SYNCED_COLUMNS if columns is None else columns):
        keys.update(SYNCED_COLUMNS.get(column, ()))
    out = {}
    for key in sorted(keys):
        if key == "due_at":
            out[key] = iso(task.due_at)
        elif key == "assignee":
            out[key] = str(task.assignee_id) if task.assignee_id else None
        else:
            out[key] = getattr(task, key)
    return out


# -- ClickUp side -----------------------------------------------------------------


def custom_field(task: dict, field_id: str) -> str:
    """The value of custom field ``field_id`` as a stripped string ("" when unset)."""
    if not field_id:
        return ""
    for field in task.get("custom_fields") or []:
        if isinstance(field, dict) and field.get("id") == field_id:
            value = field.get("value")
            if value is None or isinstance(value, (dict, list)):
                return ""
            return str(value).strip()
    return ""


def status_in(status: Any, list_cfg: dict) -> str | None:
    """ClickUp ``status`` -> golden status: the list's status_map, else by status type."""
    if not isinstance(status, dict):
        return None
    name = str(status.get("status") or "").strip().lower()
    mapped = {str(k).strip().lower(): v for k, v in (list_cfg.get("status_map") or {}).items()}
    if name in mapped:
        return mapped[name]
    return values.STATUS_BY_TYPE.get(str(status.get("type") or "").lower())


def status_out(status: str, list_cfg: dict) -> str | None:
    """Golden status -> ClickUp status name: ``push_status``, else the one status_map entry naming it."""
    explicit = (list_cfg.get("push_status") or {}).get(status)
    if explicit:
        return explicit
    names = sorted(k for k, v in (list_cfg.get("status_map") or {}).items() if v == status)
    return names[0] if len(names) == 1 else None


def priority_in(priority: Any) -> str:
    if isinstance(priority, dict):
        try:
            return values.PRIORITY_IN.get(int(priority.get("id")), "normal")
        except (TypeError, ValueError):
            return "normal"
    return "normal"


def priority_out(priority: str) -> int:
    return values.PRIORITY_OUT.get(priority, values.PRIORITY_OUT["normal"])


def due_in(task: dict, zone) -> dt.date | dt.datetime | None:
    """ClickUp due -> what ``crm.tasks.due_instant`` takes.

    With a time (``due_date_time``) it is the instant. Without, it is an
    all-day task: the calendar day of that instant in ``zone`` (the
    assignee's), due at the end of that day.
    """
    instant = ms_to_dt(task.get("due_date"))
    if instant is None:
        return None
    if task.get("due_date_time"):
        return instant
    return instant.astimezone(zone).date()


def assignees(task: dict) -> list[dict]:
    """``[{id, email}]`` in ClickUp's order (ids as strings)."""
    out = []
    for person in task.get("assignees") or []:
        if isinstance(person, dict) and person.get("id") is not None:
            out.append({"id": str(person["id"]), "email": str(person.get("email") or "").strip().lower()})
    return out


def title_in(name: Any) -> tuple[str, bool]:
    """``(title, truncated)``: golden's title is 255 characters; longer is cut, never refused."""
    text = str(name or "").strip()
    if len(text) > values.TITLE_MAX:
        return text[: values.TITLE_MAX], True
    return text, False


def description_in(task: dict) -> str:
    text = task.get("markdown_description") or task.get("description") or task.get("text_content") or ""
    return str(text)


def done_at(task: dict) -> dt.datetime | None:
    return ms_to_dt(task.get("date_done")) or ms_to_dt(task.get("date_closed"))


# -- request bodies ---------------------------------------------------------------


def _due_body(due_at_iso: str | None, all_day: bool) -> dict:
    due = parse_iso(due_at_iso)
    if due is None:
        return {"due_date": None, "due_date_time": False}
    return {"due_date": dt_to_ms(due), "due_date_time": not all_day}


def create_body(golden: dict, *, status_name: str | None, golden_id: str, golden_id_field: str,
                assignee_id: str | None) -> dict:
    """POST /api/v2/list/{list_id}/task: every ClickUp-owned value plus golden's id field."""
    body = {
        "name": golden["title"],
        "description": golden.get("description") or "",
        "priority": priority_out(golden.get("priority") or "normal"),
        "assignees": [int(assignee_id)] if assignee_id and assignee_id.isdigit() else [],
        "custom_fields": [{"id": golden_id_field, "value": golden_id}],
        **_due_body(golden.get("due_at"), bool(golden.get("all_day", True))),
    }
    if status_name:
        body["status"] = status_name
    return body


def update_body(golden: dict, *, status_name: str | None, add_assignee: str | None,
                remove_assignees: list[str]) -> dict:
    """PUT /api/v2/task/{id}: only the pinned values (a golden change, not state)."""
    body: dict = {}
    if "title" in golden:
        body["name"] = golden["title"]
    if "description" in golden:
        body["description"] = golden["description"] or ""
    if "priority" in golden:
        body["priority"] = priority_out(golden["priority"])
    if "status" in golden and status_name:
        body["status"] = status_name
    if "due_at" in golden:
        body.update(_due_body(golden["due_at"], bool(golden.get("all_day", True))))
    if "assignee" in golden:
        add = [int(add_assignee)] if add_assignee and add_assignee.isdigit() else []
        rem = sorted(int(x) for x in remove_assignees if x.isdigit() and x != add_assignee)
        body["assignees"] = {"add": add, "rem": rem}
    return body


def erase_body() -> dict:
    """What the erase handler writes: the work item stays, golden's copy is scrubbed (4.1)."""
    return {"name": values.ERASED_NAME, "description": ""}
