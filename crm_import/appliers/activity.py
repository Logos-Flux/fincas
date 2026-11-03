"""What the task and communication appliers share (docs/crm/phase5-importer.md
2.9, 2.10; hand-off sections 4-6).

**Targets, explicitly.** An activity's ``target_refs`` name the deals,
tickets, contacts and companies it was associated with in the source. Its
engagement is the first deal (or, with ``tickets_as_engagements``, ticket)
that resolves; its party the first contact or company that resolves (merges
followed, an erased party never). Nothing is guessed: no target, no link
(``record(..., link_engagement=False)``). A target the export lacks, that
was not imported (N/A) or whose person was erased is dropped and named in
the sidecar (``import_unresolved_targets``); one still waiting for review or
apply is ``dependency_missing`` (retried once it is applied, 4.2).

**Engagement bookkeeping, logged.** ``Task.save()`` recomputes the
engagement's next action and an imported completed communication (or done
task) moves ``last_activity_at`` forward (``crm.engagements.touch_activity``,
which ``record`` skips inside ``suppressed()``). Both are written per row,
inside the row's transaction, as a final ``update`` entry on the engagement
(``WriteSession.checkpoint``), so undo restores the pointer before it
deletes the task. The hand-off's "once per batch" is a performance note: the
unify apply loop has no end-of-batch hook, and per row keeps the write log
exact and the engagement right after every chunk.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.utils import timezone

from .base import Row
from .deals import live_party, resolve_optional

ENGAGEMENT_TARGETS = ("deal", "relationship")
PARTY_TARGETS = ("contact", "company")


@dataclass
class Targets:
    engagement: object = None
    party: object = None
    unresolved: list = field(default_factory=list)  # "<type>:<id>" the import could not link
    others: list = field(default_factory=list)  # resolved, but not the first of their kind


def targets(row: Row) -> Targets:
    from crm.models import Engagement
    from parties.models import Party

    out = Targets()
    tickets_as_engagements = bool((row.job.options or {}).get("tickets_as_engagements"))
    for ref in row.data.get("target_refs") or []:
        if not ref:
            continue
        otype, label = ref.get("object_type"), f"{ref.get('object_type')}:{ref.get('source_id')}"
        if otype in ENGAGEMENT_TARGETS:
            if otype == "relationship" and not tickets_as_engagements:
                out.unresolved.append(label)  # a partner relationship has no tasks or thread
                continue
            found, _absent = resolve_optional(row, ref, Engagement)
            if found is None:
                out.unresolved.append(label)
            elif out.engagement is None:
                out.engagement = found
            elif found.pk != out.engagement.pk:
                out.others.append(label)
        elif otype in PARTY_TARGETS:
            found, _absent = resolve_optional(row, ref, Party)
            found = live_party(found)
            if found is None:
                out.unresolved.append(label)
            elif out.party is None:
                out.party = found
            elif found.pk != out.party.pk:
                out.others.append(label)
    return out


def owner_of(row: Row):
    """The owner's ``UserProfile``; one the export lacks is NULL (sidecar flag)."""
    from .deals import owner_of as deal_owner

    return deal_owner(row)


class EngagementCheckpoint:
    """Snapshot an engagement before a helper changes it behind the session,
    then log the change as the row's last entry on it (see the module docstring)."""

    def __init__(self, row: Row, engagement_id):
        from crm.models import Engagement

        from .. import writelog

        self.row, self.engagement_id = row, engagement_id
        self.before = writelog.snapshot(Engagement, engagement_id) if engagement_id else None

    def done(self) -> None:
        from crm.models import Engagement

        if self.before is not None:
            self.row.session.checkpoint(Engagement, self.engagement_id, self.before)


def touch_activity(row: Row, engagement_id, at) -> None:
    """``last_activity_at`` forward to ``at`` (never back, never the future), logged."""
    from crm.engagements import touch_activity as touch

    if engagement_id is None or at is None or at > timezone.now():
        return
    mark = EngagementCheckpoint(row, engagement_id)
    if touch(engagement_id, at):
        mark.done()
