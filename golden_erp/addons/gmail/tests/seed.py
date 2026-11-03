"""Case seeds the generic runner does not build (``case.json`` "seed").

- ``suppressions``: ``[{"address", "reason"}]``; reason ``erasure`` is what
  ``parties.erasure.erase_party`` leaves behind for an erased address.
- ``engagements``: ``[{"key", "party"}]``: open engagements (fixture key ``engagement:<key>``).
- ``thread_communications``: ``[{"key", "thread_root", "engagement", "party", "subject"}]``:
  a communication already on a thread (a manual log), so a reply can take its engagement.
- ``email_logs``: ``[{"id", "recipient"}]``: golden document emails already sent and logged
  (the EmailLog and its communication, as ``log_document_email`` leaves them).

Grants are polled in ``created_at`` order; under the case's pinned clock
they would tie, so the seed spaces them a second apart in seed order.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from core.plugins.dispatch import suppressed


def _by_key(tenant, key):
    return next(pk for pk, value in tenant.keys.by_id.items() if value == key)


def seed(tenant, case: dict) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from communications.ingest import record
    from communications.models import EmailLog
    from core.models import IntegrationCredential
    from crm import engagements
    from parties.consent import address_hash, normalize_address
    from parties.models import Party, Suppression

    spec = case.get("seed") or {}
    company = tenant.company
    grants = spec.get("grants", [])
    for index, row in enumerate(grants):
        IntegrationCredential.objects.filter(account=tenant.account, owner_id=_by_key(tenant, row["owner"])).update(
            created_at=timezone.now() - timedelta(seconds=len(grants) - index))
    with suppressed():
        for row in spec.get("suppressions", []):
            Suppression.objects.create(
                company=company, channel="email",
                address_hash=address_hash(normalize_address("email", row["address"])),
                reason=row.get("reason", "erasure"), source="erp",
                source_detail="erasure" if row.get("reason", "erasure") == "erasure" else "",
            )
        for row in spec.get("engagements", []):
            party = Party.objects.get(pk=_by_key(tenant, f"party:{row['party']}"))
            eng = engagements.create(company=company, party=party, title=row.get("title", "Fixture deal"))
            tenant.keys.add(f"engagement:{row['key']}", eng)
        for row in spec.get("thread_communications", []):
            from communications.models import Communication

            root = row["thread_root"].strip("<>").lower()
            result = record(
                company, "email", direction="outbound",
                occurred_at=datetime.fromisoformat(row.get("occurred_at", "2026-03-01T10:00:00+00:00")),
                subject=row.get("subject", ""), provider="manual",
                thread_key="mid:" + hashlib.sha256(root.encode()).hexdigest()[:40],
                engagement=_engagement(tenant, row.get("engagement")),
                party=Party.objects.get(pk=_by_key(tenant, f"party:{row['party']}")),
            )
            tenant.keys.add(f"communication:{row['key']}", result.communication)
            assert Communication.objects.filter(pk=result.communication.pk).exists()
        for row in spec.get("email_logs", []):
            from communications.models import Communication

            log = EmailLog.objects.create(
                id=uuid.UUID(row["id"]), document_type="quote", document_id=uuid.uuid4(),
                document_number="Q-0001", recipient_email=row["recipient"], subject="Quote Q-0001",
                template_name="quote_email", status="sent", triggered_by="manual",
            )
            comm = Communication.objects.create(
                company=company, kind="email", direction="outbound", provider="golden", is_editable=False,
                occurred_at=datetime.fromisoformat("2026-03-02T13:59:00+00:00"), subject=log.subject,
                thread_key=f"golden:quote:{log.document_id}", email_log=log,
            )
            tenant.keys.add(f"communication:{row.get('key', 'golden_send')}", comm)

def _engagement(tenant, key):
    from crm.models import Engagement

    if not key:
        return None
    return Engagement.objects.get(pk=_by_key(tenant, f"engagement:{key}"))
