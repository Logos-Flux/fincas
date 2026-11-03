"""Read a recorded CRM v3 crawl (docs/crm/phase5-importer.md 2.1).

Layout (every file a recorder envelope, ``recording.py``)::

    crawl/recording.json                     recorded_at + /account-info/v3/details
    crawl/errors.json                        every non-2xx response (only 403s are allowed)
    crawl/<object>/page-NNNN.json            GET /crm/v3/objects/<object> (or POST …/search)
    crawl/<object>/archived/page-NNNN.json   the same with archived=true
    crawl/<object>/count.json                a count probe: POST …/search with limit=1
    crawl/history/<object>/page-NNNN.json    the list endpoint with propertiesWithHistory=…
    crawl/associations/<from>-<to>/page-NNNN.json   POST /crm/v4/associations/<from>/<to>/batch/read
    crawl/owners.json, crawl/owners-archived.json   GET /crm/v3/owners (archived=true)
    crawl/pipelines-deals.json, crawl/pipelines-tickets.json   GET /crm/v3/pipelines/<object>
    crawl/properties-<object>.json           GET /crm/v3/properties/<object>
    crawl/subscriptions/page-NNNN.json       communication-preferences status per recipient

The reader checks each response's shape (``results``, ``paging``) before
reading values and records every coverage fact it sees (spec 2.13).
"""

from __future__ import annotations

from pathlib import Path

from core.plugins.crm_export import AdapterError

from .model import CANONICAL, SINGULAR, Bundle, Feed, HsRecord, Status, orient
from .recording import (
    HS_OBJECTS,
    OBJECT_TYPE_IDS,
    ErrorFact,
    next_cursor,
    query_list,
    read_envelope,
    read_errors,
    read_pages,
    read_single,
    results_of,
)
from .values import DOCS

#: HubSpot's search endpoints return at most this many results per query.
SEARCH_CAP = 10_000


def _obj_name(name: str) -> str | None:
    name = OBJECT_TYPE_IDS.get(name, name)
    return name if name in CANONICAL else None


def api_record(obj: str, item, *, raw_ref: str, archived: bool, where: str) -> HsRecord:
    """One object from a v3 list/search response."""
    if not isinstance(item, dict) or item.get("id") in (None, "") or not isinstance(item.get("properties"), dict):
        raise AdapterError(
            f"{where} is not a HubSpot object (expected id and properties) — cause: the recorder "
            "saved another response shape. Fix: re-record the page. " + DOCS
        )
    props = {str(k): v for k, v in item["properties"].items() if v is not None and v != ""}
    return HsRecord(
        obj=obj,
        id=str(item["id"]),
        properties=props,
        archived=archived or item.get("archived") is True,
        created_at=item.get("createdAt") or props.get("createdate") or props.get("hs_createdate"),
        updated_at=item.get("updatedAt") or props.get("lastmodifieddate") or props.get("hs_lastmodifieddate"),
        raw_ref=raw_ref,
        payload=item,
    )


def _embedded(bundle: Bundle, record: HsRecord, item: dict) -> None:
    """Associations embedded by ``?associations=…`` on a v3 list page."""
    assoc = item.get("associations")
    if not isinstance(assoc, dict):
        return
    for key, block in sorted(assoc.items()):
        other = _obj_name(str(key))
        if other is None or orient(record.obj, other) is None or not isinstance(block, dict):
            continue
        pair = bundle.pair(record.obj, other)
        forward = (pair.a, pair.b) == (record.obj, other)
        for entry in block.get("results") or []:
            if not isinstance(entry, dict) or entry.get("id") in (None, ""):
                continue
            other_id = str(entry["id"])
            link = pair.link(record.id, other_id) if forward else pair.link(other_id, record.id)
            type_name = str(entry.get("type") or "")
            if type_name and type_name not in link.type_ids:
                link.type_ids.append(type_name)
            # v3 names the primary company association "<from>_to_company"; the
            # others carry an "_unlabeled" (or label) suffix.
            if other == "companies" and forward and type_name == f"{SINGULAR[record.obj]}_to_company":
                link.primary = True
        if next_cursor(block) is not None:
            # More associations than the object's page inlined: the rest were not recorded.
            pair.status.add("page_missing", f"{record.obj} {record.id} has more {other} associations than "
                                            "its page held (associations.paging.next)")


def _read_object(bundle: Bundle, crawl: Path, root: Path, obj: str, errors: list[ErrorFact]) -> Feed:
    feed = Feed(obj=obj, input="crawl")
    mine = [e for e in errors if e.target == obj]
    pages = read_pages(crawl / obj, root)
    if not pages.present:
        feed.status.enumerated = False
        forbidden = [e for e in mine if e.scope == "object"]
        if forbidden:
            for e in forbidden:
                feed.status.add("forbidden", e.detail)
        else:
            feed.status.add("file_absent", f"crawl/{obj}/ holds no pages")
        return feed

    embedded_sets: list[set[str]] = []
    search_totals: list[int] = []
    search_rows = 0
    searched = False
    for env in pages.pages:
        embedded_sets.append({o for o in (_obj_name(x) for x in query_list(env.query, "associations")) if o})
        results = results_of(env)
        if env.is_search:
            searched = True
            total = env.payload.get("total") if isinstance(env.payload, dict) else None
            if isinstance(total, int):
                search_totals.append(total)
            search_rows += len(results)
        for i, item in enumerate(results):
            rec = api_record(obj, item, raw_ref=f"{env.rel}#/payload/results/{i}", archived=False,
                             where=f"{env.rel} results[{i}]")
            feed.add(rec)
            _embedded(bundle, rec, item)
    for gap in pages.gaps:
        feed.status.add("page_missing", gap)
    for e in mine:
        if e.scope == "object":
            feed.status.add("forbidden", e.detail)
    if searched and (max(search_totals, default=0) >= SEARCH_CAP or search_rows == SEARCH_CAP):
        feed.status.add(
            "search_cap",
            f"crawl/{obj}/ was recorded from the search endpoint, which stops at {SEARCH_CAP:,} results "
            f"({max(search_totals + [search_rows]):,} results). Fix: re-crawl with the list endpoint (cursor paging has no "
            "cap) or use a file export",
        )
    # Association types embedded on every page count as exported for that pair.
    embedded = set.intersection(*embedded_sets) if embedded_sets else set()
    for other in sorted(embedded):
        if orient(obj, other) is not None:
            bundle.pair(obj, other).exported = True

    # Archived records are a separate enumeration.
    archived_pages = read_pages(crawl / obj / "archived", root)
    archived_forbidden = [e for e in mine if e.scope == "archived"]
    if archived_forbidden:
        for e in archived_forbidden:
            feed.status.add("forbidden", e.detail + " (archived records unknown)")
    elif archived_pages.present:
        feed.archived = "enumerated"
        for env in archived_pages.pages:
            for i, item in enumerate(results_of(env)):
                rec = api_record(obj, item, raw_ref=f"{env.rel}#/payload/results/{i}", archived=True,
                                 where=f"{env.rel} results[{i}]")
                feed.add(rec)
        for gap in archived_pages.gaps:
            feed.status.add("page_missing", gap)
    else:
        feed.status.add("archived_not_enumerated",
                        f"crawl/{obj}/archived/ is absent: archived {obj} were not enumerated")

    # A count probe states the source's own count of live records.
    probe = read_single(crawl / obj / "count.json", root)
    if probe is not None:
        total = probe.payload.get("total") if isinstance(probe.payload, dict) else None
        if not isinstance(total, int) or isinstance(total, bool):
            raise AdapterError(f"{probe.rel} has no integer total — not a search response. " + DOCS)
        feed.declared_total = total
        if total != feed.live_rows:
            feed.status.add("count_mismatch",
                            f"the count probe says {total} live {obj}; {feed.live_rows} were enumerated")

    # Property history is a third enumeration. It counts only when it covers
    # every live record: "no history row" must never stand in for "not read".
    # A 403 on it is treated like absent history pages (spec 2.14).
    history = read_pages(crawl / "history" / obj, root)
    if history.present and not history.gaps and not [e for e in mine if e.scope == "history"]:
        props_sets = [query_list(env.query, "propertiesWithHistory") for env in history.pages]
        props = set.intersection(*props_sets) if props_sets else set()
        found: dict[str, dict] = {}
        for env in history.pages:
            for item in results_of(env):
                if isinstance(item, dict) and isinstance(item.get("propertiesWithHistory"), dict):
                    found.setdefault(str(item.get("id")), {}).update(item["propertiesWithHistory"])
        live = {r.id for r in feed.records.values() if not r.archived}
        if props and live <= set(found):
            feed.history_exported = True
            feed.history_props = props
            for rid in live:
                rec = feed.records[rid]
                rec.history = {prop: [e for e in (found[rid].get(prop) or []) if isinstance(e, dict)]
                               for prop in props}
    return feed


def _read_pairs(bundle: Bundle, crawl: Path, root: Path, errors: list[ErrorFact]) -> None:
    base = crawl / "associations"
    forbidden = {e.target: e for e in errors if e.scope == "pair"}
    for target, e in sorted(forbidden.items()):
        a, b = target.split("-", 1)
        if orient(a, b):
            bundle.pair(a, b).status.add("forbidden", e.detail)
    if not base.is_dir():
        return
    for directory in sorted(p for p in base.iterdir() if p.is_dir()):
        if "-" not in directory.name:
            continue
        a, b = (_obj_name(x) for x in directory.name.split("-", 1))
        if not a or not b or orient(a, b) is None:
            continue
        pair = bundle.pair(a, b)
        forward = (pair.a, pair.b) == (a, b)
        pages = read_pages(directory, root)
        if not pages.present:
            continue
        pair.exported = not pages.gaps
        for gap in pages.gaps:
            pair.status.add("page_missing", gap)
        inputs: set[str] = set()
        for env in pages.pages:
            body = env.request.get("body")
            for entry in (body or {}).get("inputs") or [] if isinstance(body, dict) else []:
                if isinstance(entry, dict) and entry.get("id") not in (None, ""):
                    inputs.add(str(entry["id"]))
        feed = bundle.feeds.get(a)
        if inputs and feed is not None:
            unread = {r.id for r in feed.records.values() if not r.archived} - inputs
            if unread:
                pair.exported = False
                pair.status.add("pair_not_exported",
                                f"{directory.name}: {len(unread)} live {a} were not in the batch read inputs")
        for env in pages.pages:
            for i, item in enumerate(results_of(env)):
                frm = (item or {}).get("from") if isinstance(item, dict) else None
                if not isinstance(frm, dict) or frm.get("id") in (None, "") or not isinstance(item.get("to"), list):
                    raise AdapterError(f"{env.rel} results[{i}] is not a v4 association result "
                                       "(expected from.id and a to list). " + DOCS)
                for to in item["to"]:
                    if not isinstance(to, dict) or to.get("toObjectId") in (None, ""):
                        continue
                    x, y = str(frm["id"]), str(to["toObjectId"])
                    link = pair.link(x, y) if forward else pair.link(y, x)
                    for t in to.get("associationTypes") or []:
                        if not isinstance(t, dict):
                            continue
                        label = t.get("label")
                        if isinstance(label, str) and label.strip().lower() == "primary":
                            link.primary = True
                        elif t.get("category") == "USER_DEFINED" and isinstance(label, str) and label:
                            if label not in link.labels:
                                link.labels.append(label)
                        elif t.get("typeId") is not None and str(t["typeId"]) not in link.type_ids:
                            link.type_ids.append(str(t["typeId"]))


def _single_list(path: Path, root: Path, status: Status, missing_code: str, missing_detail: str,
                 forbidden: list[ErrorFact]) -> list:
    """A one-file listing (owners, pipelines, properties) as [(item, raw_ref)]."""
    env = read_single(path, root)
    if env is None:
        if forbidden:
            for e in forbidden:
                status.add("forbidden", e.detail)
        else:
            status.add(missing_code, missing_detail)
        status.enumerated = False
        return []
    status.enumerated = True
    if next_cursor(env.payload) is not None:
        status.add("page_missing", f"{env.rel} has paging.next; the rest of the listing was not recorded")
    return [(item, f"{env.rel}#/payload/results/{i}") for i, item in enumerate(results_of(env))]


def read_crawl(bundle: Bundle, input_dir: Path) -> None:
    crawl = input_dir / "crawl"
    root = input_dir
    errors = read_errors(crawl / "errors.json", root)  # refuses a failed crawl before anything else
    info_path = crawl / "recording.json"
    if not info_path.is_file():
        raise AdapterError(
            "crawl/recording.json is missing — cause: the recorder writes it with the recording time and "
            "the account details (portal id, company currency). Fix: re-run the recorder, or write it by "
            "hand from GET /account-info/v3/details. " + DOCS
        )
    info = read_envelope(info_path, root).payload
    if not isinstance(info, dict) or not isinstance(info.get("recorded_at"), str):
        raise AdapterError("crawl/recording.json needs payload.recorded_at (a UTC instant). " + DOCS)
    bundle.kinds.append("crawl")
    bundle.exported_at.append(info["recorded_at"])
    if isinstance(info.get("account"), dict):
        bundle.account = {**info["account"], **bundle.account}

    for obj in HS_OBJECTS:
        bundle.feeds[obj] = _read_object(bundle, crawl, root, obj, errors)
    _read_pairs(bundle, crawl, root, errors)

    owners = _single_list(crawl / "owners.json", root, bundle.owner_status, "file_absent",
                          "crawl/owners.json is absent", [e for e in errors if e.scope == "owners"])
    bundle.owners = [(item, False, ref) for item, ref in owners]
    if bundle.owner_status.enumerated:
        archived_forbidden = [e for e in errors if e.scope == "owners_archived"]
        archived_status = Status()
        archived = _single_list(crawl / "owners-archived.json", root, archived_status, "archived_not_enumerated",
                                "crawl/owners-archived.json is absent: archived owners were not enumerated",
                                archived_forbidden)
        for code, detail in archived_status.reasons:
            bundle.owner_status.add(code, detail)
        if archived_status.enumerated and not archived_status.reasons:
            bundle.owners_archived = "enumerated"
        bundle.owners += [(item, True, ref) for item, ref in archived]

    for obj in ("deals", "tickets"):
        status = Status()
        bundle.pipelines[obj] = _single_list(
            crawl / f"pipelines-{obj}.json", root, status, "file_absent", f"crawl/pipelines-{obj}.json is absent",
            [e for e in errors if e.scope == "pipelines" and e.target == obj])
        bundle.pipeline_status[obj] = status
    for obj in HS_OBJECTS:
        status = Status()
        bundle.properties[obj] = _single_list(
            crawl / f"properties-{obj}.json", root, status, "file_absent", f"crawl/properties-{obj}.json is absent",
            [e for e in errors if e.scope == "properties" and e.target == obj])
        bundle.property_status[obj] = status

    subs = read_pages(crawl / "subscriptions", root)
    sub_forbidden = [e for e in errors if e.scope == "subscriptions"]
    if sub_forbidden:
        for e in sub_forbidden:
            bundle.subscription_status.add("forbidden", e.detail)
    elif subs.present:
        bundle.subscription_status.enumerated = True
        for gap in subs.gaps:
            bundle.subscription_status.add("page_missing", gap)
        for env in subs.pages:
            for i, item in enumerate(results_of(env)):
                bundle.subscriptions.append((item, f"{env.rel}#/payload/results/{i}"))
    else:
        bundle.subscription_status.add(
            "file_absent", "crawl/subscriptions/ is absent: per-subscription grants and withdrawals are unknown")
