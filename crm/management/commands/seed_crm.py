"""Seed demo engagements with history (CRM Phase 3 step 8).

    manage.py seed_crm                    # top up to 60 seeded engagements
    manage.py seed_crm --count 5000       # performance rehearsal
    manage.py seed_crm --company <uuid>

Each engagement gets what a worked deal has: a creation row and stage moves
in ``EngagementStageChange``, an outcome (about 60% open, 25% lost, 15% won,
closed ones keeping the stage they closed at), communications (an email
thread, calls with durations, meetings, notes; some meetings scheduled ahead),
done tasks with their ``TaskEvent``s, and, on most open engagements, one open
task as the next action (some overdue, some due today, most ahead). One in
five open engagements has no next action and one in nine no owner, so the
exception queue has work in it. Engagements with a paid or social source get
a ``Touchpoint`` as first and last touch; an organisation's engagement gets
its first employee as the primary contact.

Idempotent like ``seed_parties``: it counts the engagements it seeded before
(``metadata.seed == "seed_crm"``) and creates the difference, continuing
after the highest ``seed_index``. Parties come from ``seed_parties`` (topped
up first). Owners are the company's staff profiles plus four seeded sales
reps: profiles without a login, which then appear in the tenant's owner and
assignee choices (fine for a demo; do not seed a real tenant). Values are fixed per index (no randomness).

Rows go in with ``bulk_create`` (no ``save()``, so no lifecycle signals and
no per-row next-action recompute): the values the services would derive
(``next_task``, ``next_action_due_at``, ``stage_entered_at``,
``last_activity_at``, numbers from the shared ``DocumentSequence``) are
computed here, so ``check_engagements`` and ``reconcile_engagements --check``
stay clean.
"""

from __future__ import annotations

import contextlib
import datetime
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from communications.models import Communication, CommunicationParticipant
from core import tz
from core.models import Company, DocumentSequence, UserProfile
from core.plugins.dispatch import suppressed
from crm.models import (
    Engagement,
    EngagementContact,
    EngagementStageChange,
    Pipeline,
    Task,
    TaskEvent,
    Touchpoint,
)
from crm.pipelines import seed_default_pipelines
from crm.tasks import end_of_local_day
from parties.models import Party

SEED_TAG = "seed_crm"
BATCH = 500
REPS = ("Ana Reyes", "Ben Okafor", "Chloe Martin", "Dev Patel")
SOURCES = ("website_form", "referral", "paid_search", "phone", "trade_show", "email",
           "paid_social", "outbound", "partner", "social", "marketplace", "")
TOUCH_SOURCES = {"paid_search": ("google", "cpc"), "paid_social": ("facebook", "paid_social"),
                 "social": ("linkedin", "social"), "website_form": ("newsletter", "email")}
LOSS = ("price", "competitor", "timing", "budget", "no_response", "scope")
NEXT_ACTIONS = (("Follow up on the proposal", "follow_up"), ("Call to confirm requirements", "call"),
                ("Send the revised quote", "email"), ("Book a site visit", "meeting"),
                ("Check budget approval", "todo"))
DONE_TASKS = (("Send the product sheet", "email"), ("Qualify the requirements", "call"),
              ("Prepare the first proposal", "todo"))
SUBJECTS = ("Pricing for the next order", "Lead times and delivery", "Specification questions",
            "Proposal follow-up", "Installation schedule", "Volume discount request")


@contextlib.contextmanager
def explicit_timestamps(*models):
    """Let ``bulk_create`` keep the ``created_at`` / ``updated_at`` we set.

    Both are ``auto_now_add`` / ``auto_now``; seeded history needs them in
    the past. Restored on exit (this process only).
    """
    saved = []
    for model in models:
        for name in ("created_at", "updated_at"):
            field = model._meta.get_field(name)
            saved.append((field, field.auto_now, field.auto_now_add))
            field.auto_now = field.auto_now_add = False
    try:
        yield
    finally:
        for field, auto_now, auto_now_add in saved:
            field.auto_now, field.auto_now_add = auto_now, auto_now_add


def reserve_numbers(company, n):
    """``n`` free ``EN-`` numbers from the company's shared document sequence."""
    with transaction.atomic():
        seq, _ = DocumentSequence.objects.select_for_update().get_or_create(company=company)
        num = seq.next_number
        taken = set(Engagement._base_manager.filter(number__startswith="EN-").values_list("number", flat=True))
        out = []
        while len(out) < n:
            candidate = f"EN-{num:05d}"
            num += 1
            if candidate not in taken:
                out.append(candidate)
        DocumentSequence.objects.filter(pk=seq.pk).update(next_number=num)
    return out


class Command(BaseCommand):
    help = "Seed demo engagements with history, tasks, communications and touchpoints; idempotent top-up."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=60,
                            help="Total seeded engagements wanted for the company (default 60).")
        parser.add_argument("--company", help="Company pk (default: the only company).")

    def handle(self, *args, **options):
        count = options["count"]
        if count < 0:
            raise CommandError("--count must be zero or more.")
        company = self._company(options.get("company"))
        seeded = Engagement._base_manager.filter(company=company, metadata__seed=SEED_TAG)
        existing = seeded.count()
        wanted = max(0, count - existing)
        if not wanted:
            self.stdout.write(f"  Engagements: {existing} seeded already for '{company.name}'; nothing to add.")
            return
        call_command("seed_parties", count=max(40, (count * 3) // 5), company=str(company.pk),
                     stdout=self.stdout)
        seed_default_pipelines(company)
        pipeline = Pipeline.objects.filter(company=company, is_default=True).first()
        stages = list(pipeline.stages.filter(is_active=True).order_by("sort_order", "key"))
        if not stages:
            raise CommandError("The default pipeline has no active stage.")
        owners = self._owners(company)
        parties = list(
            Party.objects.filter(company=company, metadata__seed="seed_parties", merged_into__isnull=True,
                                 erased_at__isnull=True)
            .only("pk", "kind", "display_name", "primary_email_normalized", "primary_phone_e164", "metadata")
        )
        parties.sort(key=lambda p: int(p.metadata.get("seed_index", 0)))
        by_index = {int(p.metadata.get("seed_index", -1)): p for p in parties}
        indexes = seeded.values_list("metadata__seed_index", flat=True)
        start = max((int(i) for i in indexes if i is not None), default=-1) + 1
        numbers = reserve_numbers(company, wanted)
        zone = tz.resolve(None, company)
        now = timezone.now()
        done = 0
        with suppressed(), explicit_timestamps(Engagement, EngagementStageChange, Task, TaskEvent,
                                                Communication, CommunicationParticipant, Touchpoint,
                                                EngagementContact):
            while done < wanted:
                size = min(BATCH, wanted - done)
                with transaction.atomic():
                    self._batch(company, pipeline, stages, owners, parties, by_index, zone, now,
                                start + done, size, numbers[done:done + size])
                done += size
        self.stdout.write(self.style.SUCCESS(
            f"  Engagements: created {wanted} for '{company.name}' ({existing + wanted} seeded)."))

    @staticmethod
    def _company(pk):
        if pk:
            try:
                return Company.objects.get(pk=pk)
            except (Company.DoesNotExist, ValueError) as exc:
                raise CommandError(f"No company {pk!r}.") from exc
        companies = list(Company.objects.order_by("created_at")[:2])
        if len(companies) != 1:
            raise CommandError("Pass --company: there is not exactly one company.")
        return companies[0]

    @staticmethod
    def _owners(company):
        """The company's staff profiles, then four seeded reps (no login)."""
        staff = list(UserProfile.objects.filter(company=company, user__is_active=True, user__is_staff=True)
                     .order_by("created_at"))
        reps = []
        for name in REPS:
            rep = UserProfile.objects.filter(company=company, user__isnull=True, display_name=name).first()
            reps.append(rep or UserProfile.objects.create(company=company, display_name=name))
        return staff + reps

    def _batch(self, company, pipeline, stages, owners, parties, by_index, zone, now, offset, size, numbers):
        engagements, changes, tasks, events, comms, participants, touches, contacts = ([] for _ in range(8))
        links = {}  # engagement -> (next task, touchpoint)
        last = len(stages) - 1
        # An open deal never sits in a stage that proposes closing (Closed/Signed).
        open_last = max((k for k, stage in enumerate(stages) if not stage.outcome_hint), default=last)
        for n, i in enumerate(range(offset, offset + size)):
            party = None if i % 25 == 24 else (parties[i % len(parties)] if parties else None)
            owner = None if i % 9 == 8 else owners[i % len(owners)]
            created = now - datetime.timedelta(days=(i * 7) % 360 + 2, hours=i % 24)
            outcome = i % 20
            status = "open" if outcome < 12 else ("lost" if outcome < 17 else "won")
            target = min(last if status != "open" else open_last, (i * 3) % len(stages))
            first = 0 if i % 3 else min(1, last)
            target = max(target, first)
            end = now - datetime.timedelta(hours=1 + i % 48)
            closed_at = None
            if status != "open":
                closed_at = min(end, created + datetime.timedelta(days=10 + i % 60))
                end = closed_at
            span = max(end - created, datetime.timedelta(hours=1))
            source = SOURCES[i % len(SOURCES)]
            value = None if i % 7 == 3 else Decimal(500 + (i * 137) % 48000)
            who = party.display_name if party else "No counterparty"
            eng = Engagement(
                company=company, number=numbers[n], pipeline=pipeline, stage=stages[target], status=status,
                closed_at=closed_at, loss_reason=LOSS[i % len(LOSS)] if status == "lost" else "",
                loss_competitor="Northwind Supply" if status == "lost" and i % 3 == 0 else "",
                owner=owner, party=party, expected_value=value, currency="USD",
                probability=(i * 13) % 100 if i % 5 == 1 else None,
                expected_close_date=(created + datetime.timedelta(days=30 + i % 75)).date(),
                source=source, origin=Engagement.Origin.IMPORT,
                title=f"{who} — {SUBJECTS[i % len(SUBJECTS)]}"[:255],
                metadata={"seed": SEED_TAG, "seed_index": i}, tags=["demo"] if i % 4 == 0 else [],
                created_at=created, updated_at=end,
            )
            engagements.append(eng)

            # History: created at the first stage, then each move up to the target.
            path = list(range(first, target + 1))
            moments = [created + span * (k / (len(path) + 1)) for k in range(len(path))]
            moments[0] = created
            previous = None
            for stage_index, at in zip(path, moments):
                changes.append(EngagementStageChange(
                    engagement=eng, company=company, from_pipeline=pipeline if previous is not None else None,
                    to_pipeline=pipeline, from_stage=stages[previous] if previous is not None else None,
                    to_stage=stages[stage_index], from_status="open" if previous is not None else "",
                    to_status="open", changed_at=at, changed_by=owner, source="import",
                    created_at=at, updated_at=at,
                ))
                previous = stage_index
            eng.stage_entered_at = moments[-1]
            activity = [moments[-1]]
            if closed_at:
                changes.append(EngagementStageChange(
                    engagement=eng, company=company, from_pipeline=pipeline, to_pipeline=pipeline,
                    from_stage=stages[target], to_stage=stages[target], from_status="open", to_status=status,
                    changed_at=closed_at, changed_by=owner, source="import",
                    note="Closed in the seed" if status == "won" else "", created_at=closed_at, updated_at=closed_at,
                ))
                activity.append(closed_at)

            # Communications: an email thread, a call, a meeting, a note.
            for k in range(i % 5):
                at = created + span * ((k + 1) / (i % 5 + 2))
                kind = ("email", "email", "call", "meeting", "note")[(i + k) % 5]
                direction = "internal" if kind == "note" else ("inbound" if (i + k) % 3 == 0 else "outbound")
                subject = SUBJECTS[(i + k) % len(SUBJECTS)] if kind != "note" else "Internal note"
                comm = Communication(
                    company=company, kind=kind, direction=direction, status="completed",
                    subject=subject if kind != "email" else f"Re: {SUBJECTS[i % len(SUBJECTS)]}",
                    body_text=("Discussed quantities, lead times and the delivery window. "
                               "They want the revised numbers by Friday.") if kind != "email" else
                    "Thanks for the details; please send the updated pricing.",
                    summary="Agreed next steps." if kind in ("call", "meeting") else "",
                    occurred_at=at, duration_seconds=(5 + (i + k) % 40) * 60 if kind in ("call", "meeting") else None,
                    ended_at=at + datetime.timedelta(minutes=5 + (i + k) % 40) if kind in ("call", "meeting") else None,
                    provider="manual", thread_key=f"seed:{i}:mail" if kind == "email" else "",
                    engagement=eng, party=party, performed_by=owner, is_editable=True,
                    metadata={"seed": SEED_TAG}, created_at=at, updated_at=at,
                )
                comms.append(comm)
                activity.append(at)
                if party is not None and kind != "note":
                    role = {"email": "to" if direction == "outbound" else "from",
                            "call": "callee" if direction == "outbound" else "caller"}.get(kind, "attendee")
                    address = party.primary_email_normalized if kind != "call" else party.primary_phone_e164
                    participants.append(CommunicationParticipant(
                        communication=comm, company=company, role=role, party=party,
                        address_raw=address or "", address_normalized=address or "",
                        display_name=party.display_name, created_at=at, updated_at=at,
                    ))
            if status == "open" and i % 10 == 3:
                at = now + datetime.timedelta(days=2 + i % 9, hours=3)
                comms.append(Communication(
                    company=company, kind="meeting", direction="outbound", status="scheduled",
                    subject="Site walkthrough", occurred_at=at, provider="manual", engagement=eng,
                    party=party, performed_by=owner, metadata={"seed": SEED_TAG},
                    created_at=now, updated_at=now,
                ))

            # Tasks: done work, then the next action.
            if i % 3 != 2:
                title, kind = DONE_TASKS[i % len(DONE_TASKS)]
                at = created + span * 0.4
                task = Task(company=company, title=title, kind=kind, status="done", assignee=owner,
                            created_by=owner, engagement=eng, party=party, completed_at=at, completed_by=owner,
                            source="import:seed_crm", created_at=created, updated_at=at)
                tasks.append(task)
                events += [TaskEvent(task=task, company=company, event="created", at=created, by=owner,
                                     created_at=created, updated_at=created),
                           TaskEvent(task=task, company=company, event="completed", from_value="open",
                                     to_value="done", at=at, by=owner, created_at=at, updated_at=at)]
                activity.append(at)
            next_task = None
            if (status == "open" and i % 5 != 0) or (status != "open" and i % 15 == 0):
                title, kind = NEXT_ACTIONS[i % len(NEXT_ACTIONS)]
                slot = i % 8
                today = now.astimezone(zone).date()
                day = (today - datetime.timedelta(days=1 + i % 10) if slot < 2 else
                       today if slot == 2 else today + datetime.timedelta(days=1 + i % 21))
                due_at = end_of_local_day(day, zone)
                made = min(now, max(created, due_at - datetime.timedelta(days=3)))
                next_task = Task(company=company, title=title, kind=kind, status="open",
                                 priority="high" if i % 6 == 0 else "normal", assignee=owner, created_by=owner,
                                 engagement=eng, party=party, due_at=due_at, all_day=True, due_tz=zone.key,
                                 source="import:seed_crm", created_at=made, updated_at=made)
                tasks.append(next_task)
                events.append(TaskEvent(task=next_task, company=company, event="created", to_value=due_at.isoformat(),
                                        at=made, by=owner, created_at=made, updated_at=made))
            eng.last_activity_at = max(activity)

            touch = None
            if source in TOUCH_SOURCES and party is not None:
                utm_source, medium = TOUCH_SOURCES[source]
                at = created - datetime.timedelta(days=2)
                touch = Touchpoint(company=company, party=party, engagement=eng, occurred_at=at, channel=source,
                                   utm_source=utm_source, utm_medium=medium,
                                   utm_campaign=f"spring-{i % 4 + 1}", landing_url="https://example.com/quote",
                                   source="import:seed_crm", metadata={"seed": SEED_TAG},
                                   created_at=at, updated_at=at)
                touches.append(touch)
            links[i] = (eng, next_task, touch)

            if party is not None and party.kind == "organisation":
                index = int(party.metadata.get("seed_index", -1))
                person = by_index.get(index + 1)
                if person is not None and person.kind == "person":
                    contacts.append(EngagementContact(engagement=eng, company=company, party=person,
                                                      role="decision_maker", is_primary=True,
                                                      created_at=created, updated_at=created))

        Engagement._base_manager.bulk_create(engagements, batch_size=BATCH)
        EngagementStageChange._base_manager.bulk_create(changes, batch_size=BATCH)
        Communication._base_manager.bulk_create(comms, batch_size=BATCH)
        CommunicationParticipant._base_manager.bulk_create(participants, batch_size=BATCH)
        Task._base_manager.bulk_create(tasks, batch_size=BATCH)
        TaskEvent._base_manager.bulk_create(events, batch_size=BATCH)
        Touchpoint._base_manager.bulk_create(touches, batch_size=BATCH)
        EngagementContact._base_manager.bulk_create(contacts, batch_size=BATCH)
        updated = []
        for eng, next_task, touch in links.values():
            if next_task is None and touch is None:
                continue
            if next_task is not None:
                eng.next_task, eng.next_action_due_at = next_task, next_task.due_at
            if touch is not None:
                eng.first_touch = eng.last_touch = touch
            updated.append(eng)
        Engagement._base_manager.bulk_update(
            updated, ["next_task", "next_action_due_at", "first_touch", "last_touch"], batch_size=BATCH)
