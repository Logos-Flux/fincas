"""Assert that denormalised ``company`` columns agree with their parents (phase2-party.md 3.1).

A model opts in by declaring ``tenancy_parents``: the FK names whose target's
``company`` must equal the row's own (``parties`` child tables copy it from
the owning party on save). Rows written around ``save()`` (``update()``,
``bulk_create``, raw SQL) can disagree; this command lists them. CI runs it,
and it runs after every merge apply.

    manage.py check_tenancy --app parties

It also lists the rows of ``NULL_COMPANY_MODELS`` with no company at all
(phase4-fields.md 6, item 3): a NULL-company lead gets no party and never
reaches the CRM, and a NULL-company customer belongs to no tenant.

Read-only. Exit 0 when every row agrees and none is company-less, 1 otherwise.
"""

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db.models import F

_SAMPLE = 10

# Models whose rows must carry a company; checked when their app is.
NULL_COMPANY_MODELS = ("customers.Customer", "crm.Lead")


class Command(BaseCommand):
    help = (
        "Report rows whose company differs from their parent's, and customers and leads "
        "with no company (exit 1 if any)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--app", action="append", dest="apps", metavar="APP_LABEL",
            help="Only check this app (repeatable). Default: every installed app.",
        )

    def handle(self, *args, **options):
        labels = options.get("apps")
        if labels:
            try:
                configs = [apps.get_app_config(label) for label in labels]
            except LookupError as exc:
                raise CommandError(str(exc)) from exc
        else:
            configs = list(apps.get_app_configs())

        checked = 0
        mismatched_total = 0
        for config in configs:
            for model in config.get_models():
                parents = getattr(model, "tenancy_parents", ())
                for parent in parents:
                    checked += 1
                    mismatched = (
                        model._base_manager.filter(**{f"{parent}__isnull": False})
                        .exclude(company=F(f"{parent}__company"))
                    )
                    count = mismatched.count()
                    if not count:
                        continue
                    mismatched_total += count
                    self.stdout.write(
                        f"{model._meta.label}: {count} row(s) whose company differs from "
                        f"{parent}.company"
                    )
                    rows = mismatched.order_by("pk").values_list(
                        "pk", "company_id", f"{parent}__company"
                    )[:_SAMPLE]
                    for pk, company_id, parent_company_id in rows:
                        self.stdout.write(
                            f"  {pk}: company {company_id}, {parent}.company {parent_company_id}"
                        )
                    if count > _SAMPLE:
                        self.stdout.write(f"  … and {count - _SAMPLE} more")

        checked_labels = {config.label for config in configs}
        null_total = 0
        for label in NULL_COMPANY_MODELS:
            app_label = label.split(".")[0]
            if app_label not in checked_labels:
                continue
            model = apps.get_model(label)
            rows = model._base_manager.filter(company__isnull=True)
            count = rows.count()
            if not count:
                continue
            null_total += count
            self.stdout.write(f"{label}: {count} row(s) with no company")
            for pk in rows.order_by("pk").values_list("pk", flat=True)[:_SAMPLE]:
                self.stdout.write(f"  {pk}")
            if count > _SAMPLE:
                self.stdout.write(f"  … and {count - _SAMPLE} more")

        self.stdout.write(
            f"{checked} parent link(s) checked, {mismatched_total} mismatched row(s), "
            f"{null_total} company-less row(s)."
        )
        problems = []
        if mismatched_total:
            problems.append(f"{mismatched_total} row(s) disagree with their parent's company")
        if null_total:
            problems.append(f"{null_total} row(s) have no company")
        if problems:
            raise CommandError("; ".join(problems) + ".", returncode=1)
