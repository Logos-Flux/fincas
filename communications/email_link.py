"""EmailLog -> Communication (docs/crm/phase3-engagement.md 2.5, E9).

``EmailLog`` stays the delivery log (status, provider id, template). Every
golden-sent document email also becomes one ``Communication(kind="email",
direction="outbound", provider="golden", is_editable=False)`` on the thread,
created in the same transaction as the log by ``log_document_email`` (the
four send sites in ``communications.services`` call it). ``link_email_log``
is idempotent on the EmailLog: the one-to-one ``email_log`` column holds at
most one communication per log, and a second call returns the first.

Purchase-order emails go to suppliers; supplier parties are Phase 4, so they
get no communication yet.

``link_email_log`` also serves the EmailLog backfill (Phase 3 step 6), which
runs inside ``communications.0003``: there it is handed the migration's
historical ``apps`` and historical instances, and touches no live model, so
a database upgrading over several releases never selects a column that a
later migration adds. Enum members (``EmailLog.Status`` ...) are read from the
live classes; they are constants, not database access.
"""

from __future__ import annotations

import logging

from django.db import IntegrityError, transaction

from .models import Communication, CommunicationParticipant, EmailLog
from .participants import Participant, build

logger = logging.getLogger(__name__)

GOLDEN = "golden"

#: Header on every document email golden sends, naming its EmailLog. A provider
#: adapter that reads a staff mailbox (Gmail, docs/crm/phase6-adapters.md 3.3)
#: skips a message carrying it: that email is logged here already.
EMAIL_LOG_HEADER = "X-Golden-Email-Log"


def golden_headers(log_id) -> dict:
    """The extra headers of a golden document email whose EmailLog will be ``log_id``."""
    return {EMAIL_LOG_HEADER: str(log_id)}

_FAILED = (EmailLog.Status.FAILED, EmailLog.Status.BOUNCED)


def status_for(log) -> str:
    """``failed`` for a failed or bounced log, else ``completed``."""
    return Communication.Status.FAILED if log.status in _FAILED else Communication.Status.COMPLETED


#: EmailLog.document_type -> (``orders`` model, path from it to the order
#: carrying the customer and engagement: "" for the document itself, or "order__").
_DOCUMENTS = {
    EmailLog.DocumentType.QUOTE: ("Quote", ""),
    EmailLog.DocumentType.SALES_ORDER: ("Order", ""),
    EmailLog.DocumentType.INVOICE: ("SalesInvoice", "order__"),
    EmailLog.DocumentType.SHIPPING_NOTIFICATION: ("SubOrder", "order__"),
}


def _registry(apps):
    if apps is None:
        from django.apps import apps as live_apps

        return live_apps
    return apps


def document_refs(document_type, document_id, *, apps=None):
    """``{company_id, party_id, engagement_id, engagement_company_id}`` of the
    document a log is about, or None (a purchase order, or the document is gone).

    The company is the document's own (a shipping notification's is its
    order's); the party is the order's or quote's customer's; the engagement is
    ``Quote.engagement`` / ``Order.engagement`` (Phase 3 step 5), None for
    storefront orders. Read as one ``values()`` query, so only these columns
    are selected whatever registry ``apps`` is.
    """
    entry = _DOCUMENTS.get(document_type)
    if entry is None:
        return None
    name, via = entry
    model = _registry(apps).get_model("orders", name)
    company = "order__company_id" if name == "SubOrder" else "company_id"
    row = model._base_manager.filter(pk=document_id).values(
        company, f"{via}customer__party_id", f"{via}engagement_id", f"{via}engagement__company_id",
    ).first()
    if row is None:
        return None
    return {"company_id": row[company], "party_id": row[f"{via}customer__party_id"],
            "engagement_id": row[f"{via}engagement_id"],
            "engagement_company_id": row[f"{via}engagement__company_id"]}


def _performed_by_id(user_id, company_id, *, apps=None):
    """The sender's UserProfile id when it belongs to ``company_id``, else None."""
    if user_id is None:
        return None
    UserProfile = _registry(apps).get_model("core", "UserProfile")
    return UserProfile._base_manager.filter(user_id=user_id, company_id=company_id).values_list(
        "pk", flat=True).first()


def link_email_log(log, document=None, *, apps=None):
    """The Communication for ``log`` (an EmailLog about ``document``), creating it once.

    Returns None for logs that get no communication (purchase orders, or a
    document that is gone). ``document`` is only its primary key (the log's
    ``document_id`` when omitted). ``apps``: a migration's historical
    registry; ``log`` is then an instance of that state's EmailLog (see the
    module docstring).
    """
    if log.document_type not in _DOCUMENTS:
        return None
    Comm = _registry(apps).get_model("communications", "Communication")
    existing = Comm._base_manager.filter(email_log=log).first()
    if existing is not None:
        return existing
    refs = document_refs(log.document_type, document.pk if document is not None else log.document_id,
                         apps=apps)
    if refs is None:
        return None
    company_id, party_id = refs["company_id"], refs["party_id"]
    comm = Comm(
        company_id=company_id, kind="email", direction=Communication.Direction.OUTBOUND,
        status=status_for(log), subject=(log.subject or "")[:255],
        occurred_at=log.created_at, provider=GOLDEN,
        thread_key=f"golden:{log.document_type}:{log.document_id}",
        engagement_id=refs["engagement_id"], party_id=party_id,
        performed_by_id=_performed_by_id(log.sent_by_id, company_id, apps=apps),
        email_log=log, is_editable=False,
        metadata={"document_type": log.document_type, "document_number": log.document_number,
                  "template_name": log.template_name},
    )
    try:
        with transaction.atomic():
            comm.save()
            row = build(comm, Participant(role=CommunicationParticipant.Role.TO,
                                          address=log.recipient_email),
                        prefer_party_id=party_id, apps=apps)
            row.save()
    except IntegrityError:
        # A concurrent call linked this log first (email_log is one-to-one).
        existing = Comm._base_manager.filter(email_log=log).first()
        if existing is None:
            raise
        return existing
    return comm


def log_document_email(document, **fields):
    """Create the EmailLog for ``document`` and its Communication, in one transaction.

    The mail has already gone out when this runs, so the delivery log must
    survive a failure to build the communication: the link runs in its own
    savepoint and a failure is logged, never raised. The EmailLog backfill
    (Phase 3 step 6, idempotent on ``email_log``) links any log left behind.
    """
    with transaction.atomic():
        log = EmailLog.objects.create(**fields)
        try:
            with transaction.atomic():
                link_email_log(log, document)
        except Exception:
            logger.exception("Could not log EmailLog %s as a communication", log.pk)
    return log


def email_log_saved(sender, instance, created=False, raw=False, **kwargs):
    """A delivery update (``bounced``, ``failed``) flips the communication's status.

    ``post_save`` only: a delivery webhook must update the log through
    ``save()``, not ``QuerySet.update()``, or the thread keeps the old status.
    """
    if raw or created:
        return
    target = status_for(instance)
    Communication._base_manager.filter(email_log=instance).exclude(status=target).update(status=target)
