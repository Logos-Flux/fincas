"""Segment lists (docs/crm/phase6-adapters.md 3.2 "Segment lists", step 3b).

``settings["segment_lists"]`` maps a golden segment to a Klaviyo list id::

    {"saved_filter:<SavedFilter uuid>": "<Klaviyo list id>"}

The spec ties segments to Phase 5 step 11 (``GOLDEN_CRM_SEGMENTS``), which is
not built yet. Until it is, a segment is a **tenant-visible saved filter on
the parties list** (``core.SavedFilter``, Phase 4 step 5): the people that
list shows for the filter's stored parameters, re-validated on every read
(``core.saved_filters.clean_params``) exactly as the list re-validates them,
so a parameter the list no longer accepts is dropped, never applied. The key
carries its kind (``saved_filter:``) so ``segment:<name>`` keys for
``GOLDEN_CRM_SEGMENTS`` can be added beside it without a settings migration.

Membership never grants anything: the profile handler subscribes a member to
a segment list only while ``can_contact(..., "marketing", ...)`` allows, and
takes them off it when they leave the segment.
"""

from __future__ import annotations

import uuid

SAVED_FILTER = "saved_filter"
PARTY_LIST_ID = "parties.party"


def parse_key(key) -> tuple[str, uuid.UUID] | None:
    """``("saved_filter", uuid)`` for a valid key, else None."""
    if not isinstance(key, str) or ":" not in key:
        return None
    kind, _, raw = key.partition(":")
    if kind != SAVED_FILTER:
        return None
    try:
        return kind, uuid.UUID(raw)
    except ValueError:
        return None


def validate(mapping, main_list: str = "") -> list[str]:
    """Errors (problem + cause + fix) for ``settings["segment_lists"]``.

    A segment may not map to the main ``list_id``: leaving the segment would
    take the person off the marketing list itself.
    """
    if not isinstance(mapping, dict):
        return ["segment_lists must be an object of segment -> Klaviyo list id — cause: another JSON "
                "value. Fix: enter {\"saved_filter:<uuid>\": \"<list id>\"} or remove it."]
    errors = []
    for key, list_id in mapping.items():
        if parse_key(key) is None:
            errors.append(f"segment_lists key {str(key)[:60]!r} is not saved_filter:<uuid> — cause: an "
                          "unknown segment kind or a malformed id. Fix: copy the saved filter's id from "
                          "the parties list.")
        if not isinstance(list_id, str) or not list_id.strip():
            errors.append(f"segment_lists[{str(key)[:60]!r}] must be a Klaviyo list id — cause: an empty "
                          "or non-string value. Fix: paste the list id from Klaviyo.")
        elif main_list and list_id == main_list:
            errors.append(f"segment_lists[{str(key)[:60]!r}] is the main list_id — cause: a segment mapped "
                          "onto the marketing list. Fix: give the segment its own Klaviyo list.")
    return errors


def _saved_filter(company, filter_id):
    from core.models import SavedFilter

    # Tenant filters only: a private filter is its owner's working set, not a
    # marketing audience the whole company agreed on.
    return SavedFilter.objects.select_related("owner").filter(
        company=company, pk=filter_id, list_id=PARTY_LIST_ID, visibility=SavedFilter.Visibility.TENANT,
    ).first()


def members(company, key):
    """A queryset of the live people in segment ``key`` of ``company``, or None when it does not resolve.

    Evaluated through the parties list's own filter code, as the filter's
    owner sees it, then pinned to ``company`` (a superuser owner sees every
    tenant).
    """
    parsed = parse_key(key)
    if parsed is None:
        return None
    saved = _saved_filter(company, parsed[1])
    if saved is None or saved.owner is None:
        return None
    from django.http import HttpRequest, QueryDict

    from core.saved_filters import clean_params
    from parties.erp_views import PartyListView
    from parties.models import Party

    request = HttpRequest()
    request.method = "GET"
    request.user = saved.owner
    view = PartyListView()
    view.kwargs = {}
    view.request = request  # the list's allow-lists can depend on the viewer (definition columns)
    kept, _dropped = clean_params(view, saved.params)
    kept.pop("show", None)  # never the erased and merged rows
    kept.pop("sort", None)
    kept.pop("dir", None)
    query = QueryDict(mutable=True)
    query.update(kept)
    request.GET = query
    return view.get_queryset().filter(
        company=company, kind=Party.Kind.PERSON, merged_into__isnull=True, erased_at__isnull=True,
    ).order_by()


def lists_for(company, party, mapping) -> set[str]:
    """The segment list ids ``party`` belongs to now (one query per mapped segment)."""
    wanted = set()
    for key, list_id in sorted((mapping or {}).items()):
        qs = members(company, key)
        if qs is not None and qs.filter(pk=party.pk).exists():
            wanted.add(list_id)
    return wanted
