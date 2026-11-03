"""Seed a staff user's own "My day" (the public demo login, by default).

    manage.py seed_my_day                       # DEMO_LOGIN_USERNAME
    manage.py seed_my_day --user admin          # any staff account
    manage.py seed_my_day --user demo --company <uuid>

``seed_crm`` spreads its deals over the company's staff and four seeded reps,
and the demo login usually does not exist yet when it runs, so the demo user's
My day is empty. This gives that user a working day, relative to today in
their zone: next actions overdue (by one, three and six days), due today (two
all-day, one at 16:00), and coming up; deals at every stage with recent
calls, emails and meetings; one waiting on a first reply; two gone stale
(nothing for 25 and 32 days, past the 14-day default); two with no next
action; a recent win and a recent loss; one meeting booked ahead.

Everything is written through the services (``crm.engagements``,
``crm.tasks``, ``communications.ingest.record``) inside ``suppressed()``, then
back-dated with ``QuerySet.update()`` (a data step: history needs past
timestamps, and the save guards refuse them). Completed activity is always
dated yesterday or earlier at a fixed local time, so after
``demo_roll_dates`` moves it by whole days it is still in the past.

Idempotent: nothing is added while the user owns an engagement tagged
``metadata.seed == "seed_my_day"``. Records today as the demo clock's anchor
when none is stored (``core.demo_clock``); on a demo with an older anchor,
run ``demo_roll_dates`` first.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from communications import ingest
from communications.models import Communication
from core import demo_clock, tz
from core.models import Company, UserProfile
from core.plugins.dispatch import suppressed
from crm import engagements, tasks
from crm.models import Engagement, EngagementStageChange, Pipeline, Task, TaskEvent
from crm.pipelines import seed_default_pipelines
from parties.models import Party

SEED_TAG = "seed_my_day"
TASK_SOURCE = "import:seed_my_day"
IMPORT = EngagementStageChange.Source.IMPORT

# Next action: (title, kind, due, priority). ``due`` is ("day", offset) for an
# all-day task or ("at", offset, "HH:MM") for a timed one, offsets from today.
# Communications: (kind, direction, day offset (always < 0), "HH:MM", subject).
# ``stage`` indexes the default pipeline's active stages (clamped).
SCENARIOS = (
    {"subject": "Pricing for the next order", "stage": 4, "created": 21, "value": 18400, "source": "referral",
     "next": ("Follow up on the proposal", "follow_up", ("day", -1), "high"),
     "comms": (("email", "outbound", -3, "10:05", "Proposal: 40 servo actuators"),
               ("email", "inbound", -1, "16:20", "Re: Proposal: 40 servo actuators")),
     "done": ("Prepare the first proposal", "todo", -4)},
    {"subject": "Volume discount request", "stage": 5, "created": 35, "value": 42750, "source": "trade_show",
     "next": ("Send the revised quote", "email", ("day", -3), "normal"),
     "comms": (("call", "outbound", -4, "11:30", "Discount tiers"),
               ("note", "internal", -4, "11:55", "Internal note"))},
    {"subject": "Installation schedule", "stage": 2, "created": 12, "value": 9600, "source": "phone",
     "next": ("Book a site visit", "meeting", ("day", -6), "normal"),
     "comms": (("meeting", "outbound", -7, "14:00", "Kick-off call"),)},
    {"subject": "Specification questions", "stage": 3, "created": 9, "value": 6250, "source": "website_form",
     "next": ("Call to confirm requirements", "call", ("day", 0), "normal"),
     "comms": (("email", "inbound", -2, "08:40", "Spec sheet for the laser sensor"),
               ("email", "outbound", -1, "09:15", "Re: Spec sheet for the laser sensor")),
     "done": ("Send the product sheet", "email", -2)},
    {"subject": "Lead times and delivery", "stage": 1, "created": 6, "value": 3900, "source": "email",
     "next": ("Check budget approval", "todo", ("day", 0), "high"),
     "comms": (("call", "inbound", -1, "15:45", "Delivery window"),)},
    {"subject": "Proposal follow-up", "stage": 6, "created": 48, "value": 27300, "source": "partner",
     "next": ("Confirm the delivery window", "call", ("at", 0, "16:00"), "normal"),
     "comms": (("meeting", "outbound", -5, "10:00", "Commercial review"),
               ("email", "inbound", -2, "17:10", "Purchase order on its way"))},
    {"subject": "Controller retrofit", "stage": 0, "created": 2, "value": 5200, "source": "website_form",
     "next": ("Qualify the requirements", "call", ("day", 1), "normal"),
     "comms": (("email", "inbound", -2, "13:25", "Quote request: controller retrofit"),)},
    {"subject": "Site walkthrough", "stage": 3, "created": 15, "value": 12800, "source": "outbound",
     "next": ("Prepare the walkthrough agenda", "todo", ("day", 3), "normal"),
     "comms": (("call", "outbound", -3, "10:40", "Walkthrough date"),),
     "scheduled": (3, "10:00", "Site walkthrough")},
    {"subject": "Annual supply agreement", "stage": 4, "created": 30, "value": 64000, "source": "referral",
     "next": ("Send the contract draft", "email", ("day", 9), "normal"),
     "comms": (("meeting", "outbound", -6, "15:00", "Terms review"),)},
    {"subject": "Bracket reorder", "stage": 2, "created": 4, "value": 1450, "source": "marketplace",
     "comms": (("email", "inbound", -1, "11:05", "Reorder: corner brackets"),)},
    {"subject": "Sensor evaluation", "stage": 1, "created": 60, "value": 7400, "source": "paid_search",
     "comms": (("email", "outbound", -32, "09:30", "Evaluation units"),)},
    {"subject": "Warehouse expansion", "stage": 3, "created": 70, "value": 38500, "source": "trade_show",
     "next": ("Check in after their budget review", "follow_up", ("day", 14), "normal"),
     "comms": (("call", "outbound", -25, "16:10", "Budget timing"),)},
    {"subject": "Replacement actuators", "stage": 5, "created": 26, "value": 15900, "source": "phone",
     "comms": (("call", "inbound", -6, "09:50", "Verbal yes"),), "outcome": ("won", -3)},
    {"subject": "Spare parts kit", "stage": 3, "created": 40, "value": 2800, "source": "email",
     "comms": (("email", "outbound", -12, "14:30", "Spare parts pricing"),), "outcome": ("lost", -9)},
)

BODIES = {
    "email": "Thanks for the details; please send the updated pricing and lead times.",
    "call": "Went through quantities and the delivery window; they want revised numbers by Friday.",
    "meeting": "Reviewed scope, timeline and commercial terms. Next steps agreed.",
    "note": "Decision maker is the operations director; procurement signs off above 25k.",
}


class Command(BaseCommand):
    help = "Give a staff user (default: the demo login) a realistic My day relative to today; idempotent."

    def add_arguments(self, parser):
        parser.add_argument("--user", help="Username (default: DEMO_LOGIN_USERNAME).")
        parser.add_argument("--company", help="Company pk (default: the user's, GOLDEN_PORTAL_COMPANY, or the only one).")
        parser.add_argument("--force", action="store_true",
                            help="Run although DEMO_USERNAMES is unset (never on a real company's database).")

    def handle(self, *args, **options):
        if not settings.DEMO_USERNAMES and not options.get("force"):
            raise CommandError("Refusing: DEMO_USERNAMES is not set, so this is not a demo deployment. "
                               "seed_my_day writes fake parties, deals and tasks into the company; pass "
                               "--force only on a throwaway demo copy.")
        username = options.get("user") or settings.DEMO_LOGIN_USERNAME
        if not username:
            raise CommandError("Pass --user: DEMO_LOGIN_USERNAME is not set.")
        user = get_user_model().objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"No user {username!r} (for the demo login, run setup_demo_user first).")
        if not user.is_staff:
            raise CommandError(f"{username!r} is not staff: My day is a back-office page.")
        profile, company = self._profile(user, options.get("company"))
        mine = Engagement._base_manager.filter(company=company, owner=profile, metadata__seed=SEED_TAG)
        if mine.exists():
            self.stdout.write(f"  My day: {username!r} already has {mine.count()} seeded engagements; nothing to add.")
            return
        call_command("seed_parties", count=40, company=str(company.pk), stdout=self.stdout)
        seed_default_pipelines(company)
        pipeline = Pipeline.objects.filter(company=company, is_default=True).first()
        stages = list(pipeline.stages.filter(is_active=True).order_by("sort_order", "key"))
        if not stages:
            raise CommandError("The default pipeline has no active stage.")
        parties = list(Party.objects.filter(company=company, metadata__seed="seed_parties", merged_into__isnull=True,
                                            erased_at__isnull=True))
        parties.sort(key=lambda p: int(p.metadata.get("seed_index", 0)))
        zone = tz.resolve(profile, company)
        now = timezone.now()
        with suppressed(), transaction.atomic():
            for n, scenario in enumerate(SCENARIOS):
                party = parties[(7 + 3 * n) % len(parties)] if parties else None
                self._scenario(company, pipeline, stages, profile, party, zone, now, n, scenario)
        demo_clock.ensure_anchor(stdout=self.stdout)
        # Demo setup scripts match "My day: seeded" to decide whether to
        # re-take the baseline snapshot: keep the words.
        self.stdout.write(self.style.SUCCESS(
            f"  My day: seeded {len(SCENARIOS)} engagements, tasks and communications for {username!r} "
            f"in '{company.name}' ({zone.key})."))

    @staticmethod
    def _profile(user, company_pk):
        profile, _ = UserProfile.objects.get_or_create(user=user)
        wanted = company_pk or (None if profile.company_id else settings.GOLDEN_PORTAL_COMPANY)
        if wanted:
            try:
                company = Company.objects.get(pk=wanted)
            except (Company.DoesNotExist, ValueError) as exc:
                raise CommandError(f"No company {wanted!r}.") from exc
            if profile.company_id and profile.company_id != company.pk:
                raise CommandError(f"{user.username!r} belongs to another company; pass that one.")
        elif profile.company_id:
            company = profile.company
        else:
            companies = list(Company.objects.order_by("created_at")[:2])
            if len(companies) != 1:
                raise CommandError("Pass --company: there is not exactly one company.")
            company = companies[0]
        if profile.company_id is None:
            # A profile with no company is attached to the one it seeds into,
            # which only ever happens on a demo copy (the DEMO_USERNAMES gate).
            UserProfile.objects.filter(pk=profile.pk).update(company=company)
            profile.company = company
        return profile, company

    def _scenario(self, company, pipeline, stages, me, party, zone, now, n, s):
        today = now.astimezone(zone).date()

        def local(days, hhmm):
            hour, minute = map(int, hhmm.split(":"))
            return datetime.datetime.combine(today + datetime.timedelta(days=days), datetime.time(hour, minute),
                                             tzinfo=zone).astimezone(datetime.UTC)

        created = local(-s["created"], "09:00")
        target = min(s["stage"], len(stages) - 1)
        eng = engagements.create(
            company=company, party=party, pipeline=pipeline, stage=stages[0], owner=me, by=me, source=IMPORT,
            origin=Engagement.Origin.IMPORT, engagement_source=s["source"],
            title=f"{party.display_name if party else 'No counterparty'} — {s['subject']}"[:255],
            expected_value=Decimal(s["value"]), currency="USD",
            expected_close_date=today + datetime.timedelta(days=10 + 4 * n),
            metadata={"seed": SEED_TAG, "seed_index": n},
        )
        history = [EngagementStageChange.objects.get(engagement=eng, from_stage__isnull=True).pk]
        for stage in stages[1:target + 1]:
            history.append(engagements.move(eng, stage, by=me, source=IMPORT).change.pk)

        activity = []
        for kind, direction, days, hhmm, subject in s["comms"]:
            at = local(days, hhmm)
            minutes = 12 + (n * 7 + days) % 30 if kind in ("call", "meeting") else None
            participants = []
            if party is not None and kind != "note":
                role = {"email": "to" if direction == "outbound" else "from",
                        "call": "callee" if direction == "outbound" else "caller"}.get(kind, "attendee")
                address = party.primary_phone_e164 if kind == "call" else party.primary_email_normalized
                participants.append({"role": role, "address": address or "", "display_name": party.display_name,
                                     "party": party})
            comm = ingest.record(
                company, kind, direction=direction, occurred_at=at, participants=participants, subject=subject,
                body_text=BODIES[kind], summary="Agreed next steps." if minutes else "",
                duration_seconds=minutes * 60 if minutes else None,
                ended_at=at + datetime.timedelta(minutes=minutes) if minutes else None,
                thread_key=f"seed_my_day:{n}" if kind == "email" else "", engagement=eng, party=party,
                performed_by=me, metadata={"seed": SEED_TAG},
            ).communication
            Communication._base_manager.filter(pk=comm.pk).update(created_at=at, updated_at=at)
            activity.append(at)
        if "scheduled" in s:
            days, hhmm, subject = s["scheduled"]
            ingest.record(company, "meeting", direction="outbound", occurred_at=local(days, hhmm),
                          status=Communication.Status.SCHEDULED, subject=subject, engagement=eng, party=party,
                          performed_by=me, metadata={"seed": SEED_TAG})

        if "done" in s:
            title, kind, days = s["done"]
            at = local(days, "12:30")
            task = tasks.create(title=title, kind=kind, engagement=eng, party=party, assignee=me, by=me,
                                source=TASK_SOURCE, at=created)
            tasks.complete(task, by=me, at=at)
            Task._base_manager.filter(pk=task.pk).update(created_at=created, updated_at=at)
            TaskEvent._base_manager.filter(task=task, event="created").update(created_at=created, updated_at=created)
            TaskEvent._base_manager.filter(task=task, event="completed").update(created_at=at, updated_at=at)
            activity.append(at)

        last = max(activity) if activity else created
        # Stage moves sit between creation and the latest activity.
        moments = [created + (last - created) * (k / len(history)) for k in range(len(history))]
        for pk, at in zip(history, moments, strict=True):
            EngagementStageChange._base_manager.filter(pk=pk).update(changed_at=at, created_at=at, updated_at=at)
        fields = {"created_at": created, "updated_at": last, "stage_entered_at": moments[-1],
                  "last_activity_at": last}

        if "outcome" in s:
            status, days = s["outcome"]
            closed = local(days, "17:00")
            if status == "won":
                change = engagements.win(eng, by=me, source=IMPORT, note="Purchase order received")
            else:
                change = engagements.lose(eng, "price", by=me, competitor="Northwind Supply", source=IMPORT)
            EngagementStageChange._base_manager.filter(pk=change.pk).update(
                changed_at=closed, created_at=closed, updated_at=closed)
            fields.update(closed_at=closed, updated_at=closed, last_activity_at=max(last, closed))

        if "next" in s:
            title, kind, due, priority = s["next"]
            when = (today + datetime.timedelta(days=due[1]) if due[0] == "day"
                    else local(due[1], due[2]).astimezone(zone))
            made = min(local(-1, "17:30"), max(created, last))
            task = tasks.create(title=title, kind=kind, engagement=eng, party=party, assignee=me, by=me, due=when,
                                priority=priority, source=TASK_SOURCE, at=made)
            Task._base_manager.filter(pk=task.pk).update(created_at=made, updated_at=made)
            TaskEvent._base_manager.filter(task=task).update(created_at=made, updated_at=made)
        Engagement._base_manager.filter(pk=eng.pk).update(**fields)
