"""Recorded HubSpot files: the envelope, page sets and the error log.

Every JSON file the recorder (or an operator) writes is a versioned envelope::

    {"schema_version": 1, "provider": "hubspot",
     "request": {"method": "GET", "path": "/crm/v3/objects/contacts", "query": {...}, "body": null},
     "payload": <the HubSpot response body, verbatim>}

``request`` is optional for files that are not API responses (``export.json``).
The reader calls ``require_known_version`` on every envelope before reading
the payload, so a recorder that changed its format is refused, never misread.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from core.plugins.crm_export import AdapterError, require_known_version

from .values import DOCS

#: The newest recorder envelope version this adapter reads.
RECORDING_VERSION = 1
PROVIDER = "hubspot"

PAGE_RE = re.compile(r"^page-(\d{4})\.json$")

#: HubSpot object names (and their numeric object type ids) as they appear in API paths.
OBJECT_TYPE_IDS = {
    "0-1": "contacts", "0-2": "companies", "0-3": "deals", "0-5": "tickets", "0-27": "tasks",
    "0-46": "notes", "0-47": "meetings", "0-48": "calls", "0-49": "emails",
}
HS_OBJECTS = ("contacts", "companies", "deals", "tickets", "tasks", "notes", "meetings", "calls", "emails")


@dataclass
class Envelope:
    path: Path
    rel: str  # the path relative to the input directory, for raw_ref and messages
    request: dict
    payload: object

    @property
    def query(self) -> dict:
        return request_query(self.request)

    @property
    def is_search(self) -> bool:
        return str(self.request.get("path", "")).rstrip("/").endswith("/search")


def request_query(request: dict) -> dict:
    """The request's query as ``{name: value}`` (from ``query`` and the path)."""
    out = dict(parse_qsl(urlsplit(str(request.get("path", ""))).query))
    query = request.get("query") or {}
    if isinstance(query, dict):
        for key, value in query.items():
            out[str(key)] = ",".join(str(v) for v in value) if isinstance(value, list) else str(value)
    return out


def query_list(query: dict, name: str) -> set[str]:
    return {part.strip() for part in query.get(name, "").split(",") if part.strip()}


def read_envelope(path: Path, root: Path) -> Envelope:
    rel = path.relative_to(root).as_posix()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AdapterError(
            f"{rel} is not UTF-8 JSON ({exc}) — cause: a damaged or hand-edited recording. "
            "Fix: re-record the file. " + DOCS
        ) from exc
    if not isinstance(data, dict) or "payload" not in data:
        raise AdapterError(
            f"{rel} is not a recorder envelope — cause: the file holds a bare HubSpot response. "
            "Fix: wrap it as {schema_version, provider, request, payload}. " + DOCS
        )
    try:
        require_known_version(data, RECORDING_VERSION)
    except AdapterError as exc:
        raise AdapterError(
            f"{rel} has recorder schema_version {data.get('schema_version')!r}, newer than this adapter "
            f"reads (max {RECORDING_VERSION}) — cause: the recorder changed its format. Fix: upgrade "
            "the HubSpot adapter before converting this recording. " + DOCS
        ) from exc
    if data.get("provider") != PROVIDER:
        raise AdapterError(f"{rel} was recorded for provider {data.get('provider')!r}, not 'hubspot'. " + DOCS)
    request = data.get("request") or {}
    if not isinstance(request, dict):
        raise AdapterError(f"{rel}: request must be an object. " + DOCS)
    return Envelope(path=path, rel=rel, request=request, payload=data["payload"])


def results_of(env: Envelope) -> list:
    """The ``results`` list of a list/search response, shape-checked."""
    payload = env.payload
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise AdapterError(
            f"{env.rel} has no results list — cause: not a HubSpot list or search response "
            "(expected {results, paging}). Fix: re-record the page. " + DOCS
        )
    return payload["results"]


def next_cursor(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    nxt = (payload.get("paging") or {}).get("next") if isinstance(payload.get("paging"), dict) else None
    if isinstance(nxt, dict) and (nxt.get("after") or nxt.get("link")):
        return str(nxt.get("after") or nxt.get("link"))
    return None


@dataclass
class PageSet:
    """The ``page-NNNN.json`` files of one directory, in order."""

    directory: Path
    rel: str
    pages: list[Envelope] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)  # human-readable page_missing details

    @property
    def present(self) -> bool:
        return bool(self.pages)


def read_pages(directory: Path, root: Path) -> PageSet:
    rel = directory.relative_to(root).as_posix() if directory.exists() else str(directory)
    pageset = PageSet(directory=directory, rel=rel)
    if not directory.is_dir():
        return pageset
    numbered = []
    for path in sorted(directory.iterdir()):
        match = PAGE_RE.match(path.name)
        if match and path.is_file():
            numbered.append((int(match.group(1)), path))
    for _number, path in numbered:
        env = read_envelope(path, root)
        results_of(env)
        pageset.pages.append(env)
    numbers = {number for number, _ in numbered}
    for missing in sorted(set(range(1, max(numbers, default=0) + 1)) - numbers):
        pageset.gaps.append(f"{rel}/page-{missing:04d}.json is missing (the pages are not consecutive)")
    if pageset.pages and next_cursor(pageset.pages[-1].payload) is not None:
        last = PAGE_RE.match(pageset.pages[-1].path.name).group(1)
        pageset.gaps.append(
            f"{rel}/page-{int(last) + 1:04d}.json is missing (page-{last}.json has paging.next)"
        )
    return pageset


def read_single(path: Path, root: Path) -> Envelope | None:
    """A one-file response (owners, pipelines, properties): None when absent."""
    if not path.is_file():
        return None
    env = read_envelope(path, root)
    results_of(env)
    return env


@dataclass(frozen=True)
class ErrorFact:
    """A 403 the recorder got, classified to what it hides."""

    scope: str  # "object" | "archived" | "history" | "pair" | "owners" | "owners_archived" |
    #             "pipelines" | "properties" | "subscriptions"
    target: str  # an HS object name, "a-b" for a pair, or "" for owners/subscriptions
    detail: str


def _object_name(segment: str) -> str | None:
    segment = OBJECT_TYPE_IDS.get(segment, segment)
    return segment if segment in HS_OBJECTS else None


def classify_error(entry: dict, rel: str) -> ErrorFact:
    method = str(entry.get("method", "GET")).upper()
    request = {"path": entry.get("path", ""), "query": entry.get("query") or {}}
    path = urlsplit(str(entry.get("path", ""))).path.rstrip("/")
    query = request_query(request)
    parts = [p for p in path.split("/") if p]
    detail = f"{entry.get('status')} on {method} {path}" + (
        "?" + "&".join(f"{k}={v}" for k, v in sorted(query.items())) if query else ""
    )
    if len(parts) >= 4 and parts[0] == "crm" and parts[2] == "objects":
        obj = _object_name(parts[3])
        if obj:
            if "propertiesWithHistory" in query:
                return ErrorFact("history", obj, detail)
            if query.get("archived", "").lower() == "true":
                return ErrorFact("archived", obj, detail)
            return ErrorFact("object", obj, detail)
    if len(parts) >= 5 and parts[0] == "crm" and parts[2] == "associations":
        a, b = _object_name(parts[3]), _object_name(parts[4])
        if a and b:
            return ErrorFact("pair", f"{a}-{b}", detail)
    if len(parts) >= 3 and parts[0] == "crm" and parts[2] == "owners":
        scope = "owners_archived" if query.get("archived", "").lower() == "true" else "owners"
        return ErrorFact(scope, "", detail)
    if len(parts) >= 4 and parts[0] == "crm" and parts[2] in ("pipelines", "properties"):
        obj = _object_name(parts[3])
        if obj:
            return ErrorFact(parts[2], obj, detail)
    if parts and parts[0] == "communication-preferences":
        # The path and query of a status read name the recipient's address: never copy them.
        return ErrorFact("subscriptions", "", f"{entry.get('status')} on {method} the communication-preferences "
                                              "status endpoint")
    raise AdapterError(
        f"{rel}: a 403 on {method} {path} is not an endpoint the adapter knows — cause: the recorder "
        "called an endpoint this adapter does not read, so it cannot tell what the 403 hides. Fix: "
        "remove the entry if the endpoint is irrelevant, or extend the adapter. " + DOCS
    )


def read_errors(path: Path, root: Path) -> list[ErrorFact]:
    """``errors.json``: every non-2xx response. A 403 is a coverage fact; any
    other failure (401, 429, 5xx, …) means the crawl failed, and the adapter
    refuses before writing anything."""
    env = read_single(path, root)
    if env is None:
        return []
    facts: list[ErrorFact] = []
    for i, entry in enumerate(results_of(env)):
        if not isinstance(entry, dict) or not isinstance(entry.get("status"), int):
            raise AdapterError(f"{env.rel}: results[{i}] needs an integer status. " + DOCS)
        status = entry["status"]
        if status == 403:
            facts.append(classify_error(entry, env.rel))
            continue
        raise AdapterError(
            f"{env.rel}: the recording holds a {status} on {entry.get('method', 'GET')} {entry.get('path')} — "
            "cause: the crawl failed (only a 403 is a coverage fact; 401, 429 and 5xx mean the "
            "recorder did not finish). Fix: re-run the recorder until errors.json holds only 403s. "
            "No canonical files were written. " + DOCS
        )
    return facts
