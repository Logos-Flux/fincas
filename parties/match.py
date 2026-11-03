"""The matcher (docs/crm/phase2-party.md 3.2).

``find(company, email=..., phone=...)`` returns the live person parties of
one tenant whose normalised email (or, without an email, E.164 phone) equals
the input exactly. No fuzzy logic: names, postcodes and domains are dedupe
evidence (3.3), never match keys. A shared phone is never a link on its own
when an email is known.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import PERSON
from .mirror import phone_parts, safe_email

MATCH_0 = "match-0"
MATCH_1 = "match-1"
MATCH_N = "match-N"


@dataclass
class MatchResult:
    state: str
    candidates: list = field(default_factory=list)  # Party rows, oldest first

    @property
    def party(self):
        return self.candidates[0] if self.state == MATCH_1 else None


def live_parties(company, kind=PERSON):
    from .models import Party

    company_id = getattr(company, "pk", company)
    return Party.objects.filter(
        company_id=company_id, kind=kind, merged_into__isnull=True, erased_at__isnull=True,
    )


def find(company, *, email: str | None = None, phone: str | None = None, region: str | None = None,
         kind: str = PERSON) -> MatchResult:
    """Exact-key lookup inside one tenant (3.1: never across tenants)."""
    key_email = safe_email(email)
    qs = live_parties(company, kind)
    if key_email:
        qs = qs.filter(emails__email_normalized=key_email)
    else:
        _raw, e164, _ext = phone_parts(phone, region)
        if not e164:
            return MatchResult(MATCH_0)
        qs = qs.filter(phones__number_e164=e164)
    candidates = list(qs.distinct().order_by("created_at", "pk"))
    state = MATCH_0 if not candidates else MATCH_1 if len(candidates) == 1 else MATCH_N
    return MatchResult(state, candidates)
