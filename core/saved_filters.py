"""Saved filters on back-office lists (docs/crm/phase4-fields.md 5.4).

A ``SavedFilter`` stores a list's filter, search and sort parameters. Its
``params`` are untrusted on the way in **and** on the way out: they are
validated against the list's allow-lists when saved, when the chips render
and when one is loaded (``?saved=<id>``). A parameter the list no longer
accepts (a removed definition filter, a choice that went away, a sort on a
column that is no longer sortable) is dropped, never applied, and the chip
says "Some conditions no longer apply".

Visibility is tenant-bound: a user sees their own filters and the ``tenant``
filters of their own company, never another company's, superusers included.
Editing or deleting a tenant filter needs ownership or
``core.change_savedfilter``.
"""

from __future__ import annotations

import uuid

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponseRedirect, QueryDict

#: At most this many filters per owner per list.
MAX_PER_LIST = 50
MAX_VALUE = 200
STALE_TEXT = "Some conditions no longer apply"


def clean_params(view, params) -> tuple[dict, list]:
    """``(kept, dropped)``: ``params`` reduced to what ``view`` accepts now.

    Single string values only. ``sort`` must be a sortable id, ``dir`` is
    ``asc``/``desc`` and only kept with a sort; every other name must be in
    ``view.get_filter_params()``, and a value must be one of its allowed
    values when the list declares them."""
    allowed = view.get_filter_params()
    sortable = view.get_sortable_fields()
    kept, dropped = {}, []
    if not isinstance(params, dict):
        return {}, ["params"]
    for name, value in params.items():
        if not isinstance(name, str) or not isinstance(value, str) or len(value) > MAX_VALUE:
            dropped.append(str(name))
            continue
        if value == "":
            continue
        if name == "sort":
            value = getattr(view, "sort_aliases", {}).get(value, value)
            if value in sortable:
                kept[name] = value
            else:
                dropped.append(name)
        elif name == "dir":
            if value in ("asc", "desc"):
                kept[name] = value
            else:
                dropped.append(name)
        elif name in allowed:
            choices = allowed[name]
            if choices is None or value in choices:
                kept[name] = value
            else:
                dropped.append(name)
        else:
            dropped.append(name)
    if "dir" in kept and "sort" not in kept:
        kept.pop("dir")
    return kept, dropped


def _company_id(user):
    return getattr(getattr(user, "userprofile", None), "company_id", None)


def visible(view):
    """The saved filters the viewer may see on this list (one query)."""
    from .models import SavedFilter

    user = view.request.user
    scope = Q(owner=user)
    company_id = _company_id(user)
    if company_id is not None:
        scope |= Q(visibility=SavedFilter.Visibility.TENANT, company_id=company_id)
        scope &= Q(company_id=company_id)
    return SavedFilter.objects.filter(scope, list_id=view.get_list_id())


def can_edit(user, saved) -> bool:
    if saved.owner_id == user.pk:
        return True
    return (saved.visibility == "tenant" and saved.company_id == _company_id(user)
            and user.has_perm("core.change_savedfilter"))


def _current(view) -> dict:
    return {k: v for k, v in view.request.GET.items() if k not in ("page", "export", "columns", "saved")}


def chips(view) -> dict:
    """Context for ``erp/_saved_filters.html``: the chips (re-validated) and
    whether the viewer may save the current view."""
    current, _dropped = clean_params(view, _current(view))
    items = []
    for saved in visible(view):
        kept, dropped = clean_params(view, saved.params)
        items.append({
            "id": saved.pk, "name": saved.name, "shared": saved.visibility == "tenant",
            "stale": bool(dropped), "stale_text": STALE_TEXT,
            "active": kept == current and bool(kept),
            "can_edit": can_edit(view.request.user, saved),
        })
    return {
        "items": items,
        "can_save": _company_id(view.request.user) is not None and bool(current),
    }


def _encode(params: dict) -> str:
    query = QueryDict(mutable=True)
    for k, v in params.items():
        query[k] = v
    return query.urlencode()


def load(view, raw_id):
    """``?saved=<id>``: re-validate the filter and redirect to its query.
    An unknown or invisible id is a notice, not a 404 (a stale bookmark)."""
    try:
        pk = uuid.UUID(str(raw_id))
    except ValueError:
        pk = None
    saved = visible(view).filter(pk=pk).first() if pk else None
    if saved is None:
        messages.warning(view.request, "That saved filter no longer exists.")
        return HttpResponseRedirect(view.request.path)
    kept, dropped = clean_params(view, saved.params)
    if dropped:
        messages.warning(view.request, f"{saved.name}: {STALE_TEXT.lower()}, so they were left out.")
    query = _encode(kept)
    return HttpResponseRedirect(view.request.path + (f"?{query}" if query else ""))


def save(view) -> None:
    from .models import SavedFilter

    request = view.request
    company_id = _company_id(request.user)
    if company_id is None:
        messages.error(request, "Saving a filter needs a staff profile with a company.")
        return
    name = (request.POST.get("name") or "").strip()[:80]
    if not name:
        messages.error(request, "Give the filter a name.")
        return
    raw = QueryDict(str(request.POST.get("query", ""))[:2000])
    params, dropped = clean_params(view, {k: raw.get(k) for k in raw})
    if not params:
        messages.error(request, "There is nothing to save: set a search, filter or sort first.")
        return
    visibility = request.POST.get("visibility", SavedFilter.Visibility.PRIVATE)
    if visibility not in SavedFilter.Visibility.values:
        visibility = SavedFilter.Visibility.PRIVATE
    list_id = view.get_list_id()
    existing = SavedFilter.objects.filter(owner=request.user, list_id=list_id, name=name,
                                          company_id=company_id).first()
    if existing is None and SavedFilter.objects.filter(owner=request.user, list_id=list_id).count() >= MAX_PER_LIST:
        messages.error(request, f"You already have {MAX_PER_LIST} saved filters on this list.")
        return
    try:
        with transaction.atomic():
            if existing is not None:
                existing.params, existing.visibility = params, visibility
                existing.save(update_fields=["params", "visibility", "updated_at"])
            else:
                SavedFilter.objects.create(company_id=company_id, owner=request.user, list_id=list_id,
                                           name=name, params=params, visibility=visibility)
    except IntegrityError:
        messages.error(request, "A filter with that name already exists.")
        return
    messages.success(request, f"Saved “{name}”." + (f" {STALE_TEXT}." if dropped else ""))


def delete(view, raw_id) -> None:
    request = view.request
    try:
        pk = uuid.UUID(str(raw_id))
    except ValueError:
        messages.error(request, "That saved filter no longer exists.")
        return
    saved = visible(view).filter(pk=pk).first()
    if saved is None:
        messages.error(request, "That saved filter no longer exists.")
        return
    if not can_edit(request.user, saved):
        messages.error(request, "Only its owner can delete that filter.")
        return
    name = saved.name
    saved.delete()
    messages.success(request, f"Deleted “{name}”.")
