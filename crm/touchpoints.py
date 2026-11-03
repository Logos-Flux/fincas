"""Touchpoints: the one write path and the attribution rules (CRM Phase 6 step 6).

docs/crm/phase6-adapters.md 3.5; the rules come from docs/crm/phase3-engagement.md 1.8.

**Writing.** :func:`record` is the only code that creates a ``Touchpoint``:
lead intake (``crm.intake``), ``POST /api/v1/crm/touchpoints/`` and the
backfills that predate it. It validates the channel and the click-id kind,
and when no click id is given it parses one from ``landing_url``
(``gclid`` > ``gbraid`` > ``wbraid`` > ``msclkid`` > ``fbclid``), plus the
UTM tags the caller did not pass. A click with no channel gets the paid
channel its network implies (``paid_search`` / ``paid_social``) when that
channel is registered.

A visit posted before the visitor is known is stored party-less with
``metadata.visitor_id``; :func:`link_visitor` attaches those rows to the
party once an identification (a lead, a later post with an email) names
it. ``metadata.event_id`` makes a retried post a no-op.

**Attribution** (one function each; the reports and the Google Ads
adapter call these, never their own copy):

- :func:`first_and_last_touch`: first = the party's earliest touchpoint
  within ``GOLDEN_CRM["attribution_lookback_days"]`` before ``at``, last =
  the latest. Fixed on the engagement at creation (``first_touch`` /
  ``last_touch``); the funnel's ``first_touch`` attribution reads that FK.
- :func:`attribution_for`: an engagement's first, last and Google click.
- :func:`google_click`: the engagement's ``last_touch`` when it carries a
  Google click id, else the party's latest touchpoint with one at or before
  the engagement's creation and within the lookback.
- :func:`path_for`: the touches a multi-touch model credits.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Q

from .conf import crm_setting

#: Click-id query parameters, in precedence order when a URL carries several.
CLICK_PARAMS = ("gclid", "gbraid", "wbraid", "msclkid", "fbclid")
#: Google Ads click ids: the ones a conversion upload can carry.
GOOGLE_CLICK_KINDS = ("gclid", "gbraid", "wbraid")
UTM_KEYS = ("source", "medium", "campaign", "term", "content")
#: The channel a click implies when the caller gave none.
CLICK_CHANNEL = {"gclid": "paid_search", "gbraid": "paid_search", "wbraid": "paid_search",
                 "msclkid": "paid_search", "fbclid": "paid_social"}
#: ``Touchpoint.source`` of a visit posted to the API.
API_SOURCE = "api"
#: ``Touchpoint.metadata`` keys this module writes (registered in core.sidecar).
VISITOR_KEY = "visitor_id"
EVENT_KEY = "event_id"
MAX_ID = 255
MAX_URL = 2048

_url = URLValidator(schemes=("http", "https"))


def parse_landing_url(url: str) -> dict:
    """``{"click_id_kind", "click_id", "utm": {...}}`` read from a landing URL's
    query string. Unparseable or empty: empty values, never an error."""
    found = {"click_id_kind": "", "click_id": "", "utm": {}}
    if not url:
        return found
    try:
        query = parse_qs(urlsplit(url).query, keep_blank_values=False)
    except ValueError:
        return found
    for kind in CLICK_PARAMS:
        value = (query.get(kind) or [""])[0].strip()
        if value:
            found["click_id_kind"], found["click_id"] = kind, value[:MAX_ID]
            break
    for key in UTM_KEYS:
        value = (query.get(f"utm_{key}") or [""])[0].strip()
        if value:
            found["utm"][key] = value[:255]
    return found


def _check_url(name: str, value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) > MAX_URL:
        raise ValidationError({name: f"At most {MAX_URL} characters."})
    try:
        _url(value)
    except ValidationError:
        raise ValidationError({name: "An absolute http(s) URL."}) from None
    return value


def _clean(*, channel, utm, referrer_url, landing_url, click_id_kind, click_id):
    from .models import Touchpoint
    from .registries import ENGAGEMENT_SOURCES

    if channel and channel not in ENGAGEMENT_SOURCES:
        raise ValidationError({"channel": f"Unknown channel {channel!r} (GOLDEN_ENGAGEMENT_SOURCES)."})
    unknown = sorted(set(utm or {}) - set(UTM_KEYS))
    if unknown:
        raise ValidationError({"utm": f"Unknown UTM key(s) {unknown}; known: {list(UTM_KEYS)}."})
    referrer_url = _check_url("referrer_url", referrer_url)
    landing_url = _check_url("landing_url", landing_url)
    click_id = (click_id or "").strip()
    if click_id_kind and click_id_kind not in Touchpoint.ClickIdKind.values:
        raise ValidationError({"click_id_kind": f"Unknown click id kind {click_id_kind!r}; "
                                                f"known: {list(Touchpoint.ClickIdKind.values)}."})
    if bool(click_id_kind) != bool(click_id):
        raise ValidationError({"click_id": "click_id and click_id_kind go together."})
    if len(click_id) > MAX_ID:
        raise ValidationError({"click_id": f"At most {MAX_ID} characters."})
    utm = {k: str(v).strip()[:255] for k, v in (utm or {}).items() if v}
    parsed = parse_landing_url(landing_url)
    if not click_id:
        click_id_kind, click_id = parsed["click_id_kind"], parsed["click_id"]
    for key, value in parsed["utm"].items():
        utm.setdefault(key, value)
    if not channel and click_id_kind in CLICK_CHANNEL and CLICK_CHANNEL[click_id_kind] in ENGAGEMENT_SOURCES:
        channel = CLICK_CHANNEL[click_id_kind]
    return channel, utm, referrer_url, landing_url, click_id_kind, click_id


def record(company, *, occurred_at, source, party=None, engagement=None, channel="", utm=None,
           referrer_url="", landing_url="", click_id_kind="", click_id="", visitor_id="",
           event_id="", metadata=None):
    """Write one touchpoint (module docstring). Returns ``(touchpoint, created)``.

    ``utm``: ``{"source", "medium", "campaign", "term", "content"}`` (any
    subset). A party or engagement of another company is refused. With
    ``event_id``, a second call for the same company and id returns the
    first row unchanged.
    """
    from .models import Touchpoint

    company_id = getattr(company, "pk", company)
    for name, row in (("party", party), ("engagement", engagement)):
        if row is not None and row.company_id != company_id:
            raise ValidationError({name: "Belongs to another company."})
    if occurred_at is None:
        raise ValidationError({"occurred_at": "Required."})
    visitor_id = (visitor_id or "").strip()
    event_id = (event_id or "").strip()
    if len(visitor_id) > MAX_ID or len(event_id) > MAX_ID:
        raise ValidationError({"visitor_id": f"visitor_id and event_id: at most {MAX_ID} characters."})
    channel, utm, referrer_url, landing_url, click_id_kind, click_id = _clean(
        channel=channel, utm=utm, referrer_url=referrer_url, landing_url=landing_url,
        click_id_kind=click_id_kind, click_id=click_id,
    )
    meta = dict(metadata or {})
    if visitor_id:
        meta[VISITOR_KEY] = visitor_id
    if event_id:
        meta[EVENT_KEY] = event_id
        _lock_event(company_id, event_id)
        existing = Touchpoint._base_manager.filter(company_id=company_id, metadata__event_id=event_id).first()
        if existing is not None:
            return existing, False
    touch = Touchpoint.objects.create(
        company_id=company_id, party=party, engagement=engagement, occurred_at=occurred_at,
        channel=channel, utm_source=utm.get("source", ""), utm_medium=utm.get("medium", ""),
        utm_campaign=utm.get("campaign", ""), utm_term=utm.get("term", ""),
        utm_content=utm.get("content", ""), referrer_url=referrer_url, landing_url=landing_url,
        click_id_kind=click_id_kind, click_id=click_id, source=source, metadata=meta,
    )
    return touch, True


def _lock_event(company_id, event_id) -> None:
    """Serialise two concurrent posts of one event id (a tag manager's fast
    retry) for the rest of the transaction: a Postgres advisory lock, since
    there is no unique index on a sidecar key (a column and an index would
    need a migration; this step has none). No-op on other databases."""
    from django.db import connection

    if connection.vendor != "postgresql" or connection.get_autocommit():
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                       [f"crm.touchpoint:{company_id}:{event_id}"])


def link_visitor(company, visitor_id, party) -> int:
    """Attach the party-less touchpoints of ``visitor_id`` to ``party``.
    Returns how many were linked. A blank id or no party links nothing."""
    from .models import Touchpoint

    if not visitor_id or party is None:
        return 0
    company_id = getattr(company, "pk", company)
    if party.company_id != company_id:
        raise ValidationError({"party": "Belongs to another company."})
    with transaction.atomic():
        return Touchpoint._base_manager.filter(
            company_id=company_id, party__isnull=True, metadata__visitor_id=visitor_id,
        ).update(party=party)


#: The fields a posted visit may carry (``POST touchpoints/``, an adapter's
#: recorded visit envelope).
VISIT_FIELDS = ("party", "email", "visitor_id", "event_id", "occurred_at", "channel", "utm_source",
                "utm_medium", "utm_campaign", "utm_term", "utm_content", "referrer_url", "landing_url",
                "click_id_kind", "click_id")
SKIPPED = "skipped"


def record_visit(company, visit: dict, *, source=API_SOURCE):
    """One visit from a site or a tag manager: ``(touchpoint, created)``, or
    ``(None, "skipped")`` when the address belongs to an erased person.

    The person is ``visit["party"]`` (a live party of ``company``) or the
    party ``visit["email"]`` names exactly (match-1, never a guess, A7); an
    unknown email stores the visit party-less under ``visitor_id`` and is
    never stored itself. A named party links the visitor's earlier
    party-less visits. ``occurred_at`` defaults to now.
    """
    from django.utils import timezone

    from parties.erasure import erased_address
    from parties.match import find, live_parties

    unknown = sorted(set(visit) - set(VISIT_FIELDS))
    if unknown:
        raise ValidationError({"visit": f"Unknown field(s) {unknown}."})
    if visit.get("party") and visit.get("email"):
        raise ValidationError({"email": "Name the person with party or email, not both."})
    party = None
    if visit.get("party"):
        party = live_parties(company).filter(pk=visit["party"]).first()
        if party is None:
            raise ValidationError({"party": "No such party in this company."})
    elif visit.get("email"):
        company_id = getattr(company, "pk", company)
        if erased_address(company_id, visit["email"]):
            return None, SKIPPED
        party = find(company, email=visit["email"]).party
    occurred_at = visit.get("occurred_at") or timezone.now()
    if isinstance(occurred_at, str):
        from django.utils.dateparse import parse_datetime

        parsed = parse_datetime(occurred_at)
        if parsed is None:
            raise ValidationError({"occurred_at": "An ISO 8601 instant."})
        occurred_at = parsed
    visitor_id = visit.get("visitor_id") or ""
    with transaction.atomic():
        touch, created = record(
            company, party=party, source=source, occurred_at=occurred_at,
            channel=visit.get("channel") or "",
            utm={k: visit.get(f"utm_{k}") or "" for k in UTM_KEYS},
            referrer_url=visit.get("referrer_url") or "", landing_url=visit.get("landing_url") or "",
            click_id_kind=visit.get("click_id_kind") or "", click_id=visit.get("click_id") or "",
            visitor_id=visitor_id, event_id=visit.get("event_id") or "",
        )
        if party is not None and visitor_id:
            link_visitor(company, visitor_id, party)
    return touch, created


# --- attribution -------------------------------------------------------------------


def lookback() -> datetime.timedelta:
    return datetime.timedelta(days=crm_setting("attribution_lookback_days"))


def _party_touches(company_id, party_id, at):
    from .models import Touchpoint

    return Touchpoint._base_manager.filter(
        company_id=company_id, party_id=party_id, occurred_at__lte=at, occurred_at__gte=at - lookback(),
    )


def first_and_last_touch(company_id, party, touch, at):
    """First = the party's earliest touchpoint within the lookback, last =
    the latest, both at or before ``at`` (Phase 3 1.8). ``touch`` (the one
    just written) stands in when the party has none or is unknown."""
    if party is None:
        return touch, touch
    touches = _party_touches(company_id, party.pk, at)
    first = touches.order_by("occurred_at", "created_at").first()
    last = touches.order_by("-occurred_at", "-created_at").first()
    return first or touch, last or touch


def google_click(engagement):
    """The touchpoint whose Google click a conversion for ``engagement``
    carries, or None (spec 3.5 rule 1)."""
    last = engagement.last_touch
    if last is not None and last.click_id_kind in GOOGLE_CLICK_KINDS and last.click_id:
        return last
    if engagement.party_id is None:
        return None
    return (
        _party_touches(engagement.company_id, engagement.party_id, engagement.created_at)
        .filter(click_id_kind__in=GOOGLE_CLICK_KINDS).exclude(click_id="")
        .order_by("-occurred_at", "-created_at").first()
    )


@dataclass(frozen=True)
class Attribution:
    first: object
    last: object
    click: object

    @property
    def first_channel(self) -> str:
        return self.first.channel if self.first is not None else ""


def attribution_for(engagement) -> Attribution:
    """The engagement's first and last touch (fixed at creation) and its Google click."""
    return Attribution(engagement.first_touch, engagement.last_touch, google_click(engagement))


def paths_for(engagements) -> dict:
    """``{engagement pk: [touch, ...]}``, oldest first, for many engagements in
    one query: the party's touches from the lookback before creation to the
    close (``closed_at``, else now), plus touches linked to the engagement.
    The query is bounded by the cohort's earliest window start and latest
    close, and touches are grouped by party and engagement once."""
    from collections import defaultdict

    from django.utils import timezone

    from .models import Touchpoint

    engagements = list(engagements)
    if not engagements:
        return {}
    now = timezone.now()
    back = lookback()
    start = min(e.created_at for e in engagements) - back
    end = max((e.closed_at or now) for e in engagements)
    q = Q(engagement_id__in=[e.pk for e in engagements])
    party_ids = {e.party_id for e in engagements if e.party_id is not None}
    if party_ids:
        q |= Q(party_id__in=party_ids)
    by_party, by_engagement = defaultdict(list), defaultdict(list)
    for t in (Touchpoint._base_manager
              .filter(q, company_id__in={e.company_id for e in engagements},
                      occurred_at__gte=start, occurred_at__lte=end)
              .order_by("occurred_at", "created_at", "pk")):
        if t.party_id is not None:
            by_party[(t.company_id, t.party_id)].append(t)
        if t.engagement_id is not None:
            by_engagement[t.engagement_id].append(t)
    paths = {}
    for eng in engagements:
        until = eng.closed_at or now
        found = {t.pk: t for t in by_engagement.get(eng.pk, []) if t.occurred_at <= until}
        if eng.party_id is not None:
            window = eng.created_at - back
            for t in by_party.get((eng.company_id, eng.party_id), []):
                if window <= t.occurred_at <= until:
                    found[t.pk] = t
        paths[eng.pk] = sorted(found.values(), key=lambda t: (t.occurred_at, t.created_at, str(t.pk)))
    return paths


def path_for(engagement) -> list:
    """:func:`paths_for` for one engagement."""
    return paths_for([engagement])[engagement.pk]
