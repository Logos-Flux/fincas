"""Quote revisions: a new version of a quote on the same deal (R2.06).

A revision is a copy of the quote (``duplicate_quote``) that stays in the
chain: ``original_quote`` points at the root of the chain,
``revision_number`` counts up, the source is marked ``superseded_by`` the
new quote and, unless it was accepted (it has an order), its status becomes
``superseded``. The revision keeps the source's engagement
(``crm.engagements.ensure_for_quote``: a revision inherits its original's),
and the deal's history records the new quote.

The model and the API carried these fields since the start; nothing in the
back office wrote them, so a lost or sent quote could not be revised from
the quote editor (release harness, 2026-10-17).
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction

from .duplicate import duplicate_quote
from .models import Quote


def revise_quote(source: Quote, *, reason: str = "", by=None) -> Quote:
    """Create and return the next revision of ``source``.

    Refuses a quote that already has a revision (``superseded_by`` set):
    revise the head of the chain instead. ``reason`` is a
    ``Quote.RevisionReason`` value or blank; ``by`` is the staff profile for
    the engagement's history row.
    """
    if source.superseded_by_id is not None:
        raise ValidationError(
            f"{source.quote_number} already has a revision "
            f"({source.superseded_by.quote_number}); revise that one."
        )
    if reason and reason not in Quote.RevisionReason.values:
        raise ValidationError({"reason": f"Unknown revision reason {reason!r}."})
    with transaction.atomic():
        source = Quote.objects.select_for_update().get(pk=source.pk)
        if source.superseded_by_id is not None:
            raise ValidationError(f"{source.quote_number} was revised by someone else just now.")
        engagement = source.engagement if source.engagement_id else None
        revision = duplicate_quote(source, engagement=engagement)
        revision.original_quote = source.original_quote if source.original_quote_id else source
        revision.revision_number = (source.revision_number or 1) + 1
        revision.revision_reason = reason or ""
        revision.amended_from = None  # a revision is the chain's next version, not a duplicate
        revision.save(update_fields=["original_quote", "revision_number", "revision_reason",
                                     "amended_from", "updated_at"])
        source.superseded_by = revision
        update = ["superseded_by", "updated_at"]
        if source.status != Quote.Status.ACCEPTED:
            source.status = Quote.Status.SUPERSEDED
            update.append("status")
        source.save(update_fields=update)
        if engagement is not None:
            from crm.engagements import record_quote_added

            record_quote_added(revision, by=by)
    return revision
