"""Communications data steps (docs/crm/phase3-engagement.md section 8).

Phase 3 step 6: EmailLog backfill (spec 2.5, E9)
------------------------------------------------
``backfill_email_communications`` (also ``manage.py backfill_crm --step 6``):
every historical EmailLog without a Communication gets one, oldest first,
through the same ``communications.email_link.link_email_log`` the four send
sites use, so a backfilled email is indistinguishable from a new one (kind
``email``, outbound, ``provider="golden"``, read-only, the document's
engagement and party, a ``to`` participant resolved against the party's
emails, ``failed`` for a failed or bounced log). Purchase-order emails are
skipped (supplier parties are Phase 4); a log whose document is gone is
counted and skipped. Idempotent: ``email_log`` is one-to-one.

**Historical models only.** The migration (``communications.0003``) passes
its ``apps``; every model this step reads or writes (the EmailLog, its
document, the customer, party, user profile, party contact points, the
Communication and its participant) comes from that registry, through the same
``communications.email_link.link_email_log`` the send sites use. Live models
grow columns in later migrations (``Party.owner_team``, ``Engagement.owner_team``
in 0.4), so a live model here broke a database upgrading across releases with
EmailLog rows (issue #88). The management command passes no ``apps`` and gets
the live registry.

``link_document_emails`` is the reconcile half: a communication of a quote,
order, invoice or shipping email whose document gained an engagement after
it was logged (``Quote.save()`` ensures one lazily, ``reconcile_engagements
--fix`` too) is put on that engagement.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from django.db import transaction

from core.plugins.dispatch import suppressed

SKIPPED_TYPES = ("purchase_order",)
STEP = 6
#: ``Communication.metadata`` key marking the rows this step created (its reverse deletes them).
BACKFILL_KEY = "backfill_step"


@dataclass
class EmailBackfillSummary:
    logs: int = 0
    linked: Counter = field(default_factory=Counter)  # by document type
    skipped_purchase_orders: int = 0
    missing_document: int = 0
    on_engagement: int = 0
    failed: int = 0
    dry_run: bool = False

    def lines(self) -> list[str]:
        by_type = ", ".join(f"{k} {v}" for k, v in sorted(self.linked.items())) or "none"
        return [
            f"email logs without a communication: {self.logs}{' [dry run]' if self.dry_run else ''}",
            f"communications created: {sum(self.linked.values())} ({by_type}); on an engagement: "
            f"{self.on_engagement}; failed/bounced: {self.failed}",
            f"purchase-order emails skipped: {self.skipped_purchase_orders}; document gone: "
            f"{self.missing_document}",
        ]


def _historical(apps):
    if apps is None:
        from django.apps import apps as live_apps

        return live_apps
    return apps


@suppressed()  # data steps announce nothing (spec 5.1, 7)
def backfill_email_communications(apps=None, *, dry_run=False, log=None) -> EmailBackfillSummary:
    summary = EmailBackfillSummary(dry_run=dry_run)
    registry = _historical(apps)
    EmailLog = registry.get_model("communications", "EmailLog")
    Communication = registry.get_model("communications", "Communication")
    pending = EmailLog._base_manager.filter(communication__isnull=True)
    summary.skipped_purchase_orders = pending.filter(document_type__in=SKIPPED_TYPES).count()
    ids = list(pending.exclude(document_type__in=SKIPPED_TYPES)
               .order_by("created_at", "pk").values_list("pk", flat=True))
    summary.logs = len(ids)
    if not ids:
        _log(log, summary)
        return summary

    from .email_link import document_refs, link_email_log
    from .models import Communication as LiveCommunication

    with transaction.atomic():
        for email_log in EmailLog._base_manager.filter(pk__in=ids).order_by("created_at", "pk"):
            if document_refs(email_log.document_type, email_log.document_id, apps=registry) is None:
                summary.missing_document += 1
                continue
            if dry_run:
                summary.linked[email_log.document_type] += 1
                continue
            comm = link_email_log(email_log, apps=registry)
            if comm is None:
                continue
            if comm.metadata.get(BACKFILL_KEY) is None:
                Communication._base_manager.filter(pk=comm.pk).update(
                    metadata={**comm.metadata, BACKFILL_KEY: STEP})
            summary.linked[email_log.document_type] += 1
            summary.on_engagement += comm.engagement_id is not None
            summary.failed += comm.status == LiveCommunication.Status.FAILED
    _log(log, summary)
    return summary


@suppressed()  # data steps announce nothing (spec 5.1, 7)
def reverse_email_communications(apps=None, *, log=None) -> int:
    """Step 6 (EmailLog) backward: delete the communications this step created
    (``metadata.backfill_step``); those the send sites made stay."""
    Communication = _historical(apps).get_model("communications", "Communication")
    with transaction.atomic():
        _total, per_model = Communication._base_manager.filter(
            provider="golden", kind="email", email_log__isnull=False,
            metadata__backfill_step=STEP,
        ).delete()
        removed = per_model.get(Communication._meta.label, 0)
    if log:
        log(f"email communications removed: {removed}")
    return removed


def unlinked_document_emails() -> list:
    """``[(communication, engagement)]``: golden email communications without
    an engagement whose document has one now (same tenant)."""
    from crm.models import Engagement

    from .email_link import document_refs
    from .models import Communication

    out = []
    rows = Communication._base_manager.filter(
        provider="golden", kind="email", engagement__isnull=True, email_log__isnull=False,
    ).select_related("email_log").order_by("occurred_at", "pk")
    for comm in rows:
        refs = document_refs(comm.email_log.document_type, comm.email_log.document_id)
        if refs and refs["engagement_id"] and refs["engagement_company_id"] == comm.company_id:
            out.append((comm, Engagement._base_manager.get(pk=refs["engagement_id"])))
    return out


@suppressed()  # data steps announce nothing (spec 5.1, 7)
def link_document_emails() -> int:
    """Put each ``unlinked_document_emails`` row on its engagement; the number moved."""
    from .models import Communication

    moved = 0
    for comm, engagement in unlinked_document_emails():
        moved += Communication._base_manager.filter(pk=comm.pk, engagement__isnull=True).update(
            engagement=engagement)
    return moved


def _log(log, summary):
    if log:
        for line in summary.lines():
            log(line)
