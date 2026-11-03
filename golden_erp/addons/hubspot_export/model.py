"""What the readers hand to the converter: HubSpot records and coverage facts.

Both readers (``reader_crawl``, ``reader_csv``) produce these, so the
conversion to canonical records and the coverage rules are written once.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: HubSpot object -> canonical object type.
CANONICAL = {
    "contacts": "contact", "companies": "company", "deals": "deal", "tickets": "relationship",
    "tasks": "task", "notes": "communication", "meetings": "communication", "calls": "communication",
    "emails": "communication",
}
#: HubSpot object -> its singular name (ExternalRef.object_type, association type names).
SINGULAR = {
    "contacts": "contact", "companies": "company", "deals": "deal", "tickets": "ticket", "tasks": "task",
    "notes": "note", "meetings": "meeting", "calls": "call", "emails": "email",
}
ACTIVITIES = ("tasks", "notes", "meetings", "calls", "emails")
COMMUNICATIONS = ("notes", "meetings", "calls", "emails")

#: The association pairs the importer needs, in canonical orientation (from, to).
#: The first five become associations.jsonl rows; activity pairs become target_refs.
CORE_PAIRS = (
    ("contacts", "companies"),
    ("deals", "contacts"),
    ("deals", "companies"),
    ("tickets", "contacts"),
    ("tickets", "companies"),
)
ACTIVITY_PAIRS = tuple((a, t) for a in ACTIVITIES for t in ("contacts", "companies", "deals", "tickets"))
PAIRS = CORE_PAIRS + ACTIVITY_PAIRS


def orient(a: str, b: str) -> tuple[str, str] | None:
    """The canonical orientation of a pair, or None if the importer does not use it."""
    if (a, b) in PAIRS:
        return (a, b)
    if (b, a) in PAIRS:
        return (b, a)
    return None


@dataclass
class Link:
    """One association between two records, with what the source says about it."""

    primary: bool = False
    labels: list[str] = field(default_factory=list)  # user-defined labels
    type_ids: list[str] = field(default_factory=list)  # HubSpot-defined type ids or v3 type names


@dataclass
class HsRecord:
    obj: str
    id: str
    properties: dict  # internal values; unset properties are absent
    archived: bool
    created_at: str | None
    updated_at: str | None
    raw_ref: str
    payload: dict | None = None  # the verbatim provider object (crawl) or CSV row
    history: dict[str, list[dict]] | None = None  # property -> [{value, timestamp, sourceType}]


@dataclass
class Status:
    """Coverage of one source feed (an object, owners, a pipeline file, …)."""

    enumerated: bool = True  # False: nothing was read ("unknown")
    reasons: list[tuple[str, str]] = field(default_factory=list)

    def add(self, code: str, detail: str) -> None:
        if (code, detail) not in self.reasons:
            self.reasons.append((code, detail))


@dataclass
class Feed:
    """One HubSpot object type as read from one input."""

    obj: str
    input: str  # "crawl" | "export"
    status: Status = field(default_factory=Status)
    records: dict[str, HsRecord] = field(default_factory=dict)
    archived: str = "not_enumerated"  # "enumerated" | "not_enumerated"
    declared_total: int | None = None
    history_props: set[str] = field(default_factory=set)
    history_exported: bool = False

    def add(self, record: HsRecord) -> None:
        current = self.records.get(record.id)
        if current is None or (current.archived and not record.archived):
            self.records[record.id] = record  # a live copy wins over an archived one

    @property
    def live_rows(self) -> int:
        return sum(1 for r in self.records.values() if not r.archived)


@dataclass
class PairFeed:
    a: str
    b: str
    links: dict[tuple[str, str], Link] = field(default_factory=dict)
    exported: bool = False
    status: Status = field(default_factory=Status)

    def link(self, a_id: str, b_id: str) -> Link:
        return self.links.setdefault((a_id, b_id), Link())


@dataclass
class Bundle:
    """Everything read from one input directory."""

    kinds: list[str] = field(default_factory=list)  # "crawl", "export"
    exported_at: list[str] = field(default_factory=list)
    account: dict = field(default_factory=dict)  # /account-info/v3/details, verbatim
    feeds: dict[str, Feed] = field(default_factory=dict)
    owners: list[dict] = field(default_factory=list)  # [(owner object, archived, raw_ref)]
    owner_status: Status = field(default_factory=lambda: Status(enumerated=False))
    owners_archived: str = "not_enumerated"
    pipelines: dict[str, list] = field(default_factory=dict)  # deals/tickets -> [(pipeline, raw_ref)]
    pipeline_status: dict[str, Status] = field(default_factory=dict)
    properties: dict[str, list] = field(default_factory=dict)  # HS object -> [property definition]
    property_status: dict[str, Status] = field(default_factory=dict)
    subscriptions: list = field(default_factory=list)  # [(recipient status object, raw_ref)]
    subscription_status: Status = field(default_factory=lambda: Status(enumerated=False))
    pairs: dict[tuple[str, str], PairFeed] = field(default_factory=dict)

    def pair(self, a: str, b: str) -> PairFeed:
        key = orient(a, b)
        assert key is not None, (a, b)
        return self.pairs.setdefault(key, PairFeed(*key))
