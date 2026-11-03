"""``manage.py check_engagements``: invariants the database cannot enforce (spec 1.3).

- an engagement's stage belongs to its pipeline (spans two tables, so no CHECK);
- pipeline, stage, party, owner and next task are in the engagement's company;
- ``source`` / ``loss_reason`` / ``Task.kind`` are registry keys (registries
  are settings, so a removed deployment entry strands rows);
- ``next_task`` is an open task of the same engagement;
- each company has exactly one default pipeline, and every open engagement's
  stage is reachable (its pipeline's stage);
- an EngagementContact is a person and is not the engagement's own party;
- a task's ``due_tz`` is a known zone, and its engagement, party,
  assignee and communication are in its company.

Read-only. Exit 0 when clean, 1 when any row violates. CI runs it on the
seeded database (like Phase 2's ``check_external_refs``).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db.models import F, Q

from core.models import Company
from core.tz import is_valid_timezone
from crm.models import OPEN_TASK_STATUSES, Engagement, EngagementContact, Pipeline, Task
from crm.registries import ENGAGEMENT_SOURCES, LOSS_REASONS, TASK_KINDS
from parties.constants import PERSON

_SAMPLE = 10


def _checks():
    eng = Engagement._base_manager
    yield ("engagement stage outside its pipeline",
           eng.filter(stage__isnull=False).exclude(stage__pipeline_id=F("pipeline_id")))
    for name in ("pipeline", "stage", "party", "owner", "next_task"):
        yield (f"engagement {name} in another company",
               eng.filter(**{f"{name}__isnull": False}).exclude(company_id=F(f"{name}__company_id")))
    yield ("engagement source not in GOLDEN_ENGAGEMENT_SOURCES",
           eng.exclude(source="").exclude(source__in=list(ENGAGEMENT_SOURCES)))
    yield ("engagement loss_reason not in GOLDEN_LOSS_REASONS",
           eng.exclude(loss_reason="").exclude(loss_reason__in=list(LOSS_REASONS)))
    yield ("engagement next_task not an open task of the engagement",
           eng.filter(next_task__isnull=False).filter(
               ~Q(next_task__engagement_id=F("pk")) | ~Q(next_task__status__in=OPEN_TASK_STATUSES)))
    contacts = EngagementContact._base_manager
    yield ("engagement contact is not a person", contacts.exclude(party__kind=PERSON))
    yield ("engagement contact duplicates the engagement's party",
           contacts.filter(party_id=F("engagement__party_id")))
    tasks = Task._base_manager
    yield ("task kind not in GOLDEN_TASK_KINDS", tasks.exclude(kind__in=list(TASK_KINDS)))
    for name in ("engagement", "party", "assignee", "communication"):
        yield (f"task {name} in another company",
               tasks.filter(**{f"{name}__isnull": False}).exclude(company_id=F(f"{name}__company_id")))


class Command(BaseCommand):
    help = "Report engagement invariant violations (exit 1 if any)."

    def handle(self, *args, **options):
        problems = 0
        for what, qs in _checks():
            problems += self._report(what, qs.order_by("pk").values_list("pk", flat=True))
        problems += self._report_rows(
            "task due_tz not a known IANA zone",
            [pk for pk, zone in Task._base_manager.exclude(due_tz="").values_list("pk", "due_tz")
             if not is_valid_timezone(zone)],
        )
        defaults = {
            row["company_id"]: row
            for row in Pipeline._base_manager.filter(is_default=True).values("company_id")
        }
        problems += self._report_rows(
            "company without a default pipeline",
            list(Company.objects.exclude(pk__in=list(defaults)).values_list("pk", flat=True)),
        )
        self.stdout.write(f"{Engagement._base_manager.count()} engagement(s) checked, "
                          f"{problems} violation(s).")
        if problems:
            raise CommandError(f"{problems} engagement invariant violation(s).", returncode=1)

    def _report(self, what, pks):
        return self._report_rows(what, list(pks))

    def _report_rows(self, what, pks):
        if not pks:
            return 0
        self.stdout.write(f"{what}: {len(pks)}")
        for pk in pks[:_SAMPLE]:
            self.stdout.write(f"  {pk}")
        if len(pks) > _SAMPLE:
            self.stdout.write(f"  … and {len(pks) - _SAMPLE} more")
        return len(pks)
