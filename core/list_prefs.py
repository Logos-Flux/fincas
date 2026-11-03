"""Per-user list preferences (docs/crm/phase4-fields.md 5.3).

``ERPUserSettings.list_preferences`` is ``{list_id: {"columns": [ids],
"page_size": n}}``. Ids are validated by the list view before they are
saved and filtered again on read (``core.list_columns.visible_ids``), so a
removed definition simply disappears from the user's choice.
"""

from __future__ import annotations

from django.http import QueryDict

#: Parameters never carried back to a list after a chooser or filter post.
_DROPPED = ("page", "export", "columns", "saved")
_MAX_QUERY = 2000


def get(user, list_id: str) -> dict:
    """The user's preferences for one list (``{}`` when none).

    Reads ``user.erp_settings``, which ``core.middleware`` has already loaded
    (and Django caches, a missing row included), so a list page pays no
    query for it."""
    if not getattr(user, "is_authenticated", False):
        return {}
    settings_obj = getattr(user, "erp_settings", None)
    stored = getattr(settings_obj, "list_preferences", None)
    entry = stored.get(list_id) if isinstance(stored, dict) else None
    return entry if isinstance(entry, dict) else {}


def save(user, list_id: str, *, columns=None, page_size=None) -> None:
    """Store the column choice (``None`` = back to defaults) and page size."""
    from django.db import transaction

    from .models import ERPUserSettings

    ERPUserSettings.objects.get_or_create(user=user)
    with transaction.atomic():
        # Locked, so two tabs saving different lists cannot drop each other.
        settings_obj = ERPUserSettings.objects.select_for_update().get(user=user)
        _store(settings_obj, list_id, columns, page_size)


def _store(settings_obj, list_id, columns, page_size) -> None:
    prefs = dict(settings_obj.list_preferences or {})
    entry = {}
    if columns:
        entry["columns"] = list(columns)
    if page_size:
        entry["page_size"] = page_size
    if entry:
        prefs[list_id] = entry
    else:
        prefs.pop(list_id, None)
    settings_obj.list_preferences = prefs
    settings_obj.save(update_fields=["list_preferences"])


def clean_query(raw: str) -> str:
    """The list query string a form was posted from, re-encoded without
    page / export parameters. It is only ever appended to the list's own
    path, so it cannot redirect elsewhere."""
    query = QueryDict(str(raw or "")[:_MAX_QUERY], mutable=True)
    for name in _DROPPED:
        query.pop(name, None)
    return query.urlencode()
