"""One-time tenancy backfill for a single-company deployment.

Golden's company-scoped reads fail closed on a NULL company, so a deployment
that predates the tenancy columns shows no stock (``Warehouse.company`` NULL)
and session users resolve no company (no ``UserProfile`` rows). This command
attaches that legacy data to one Company. It is the cutover step the runbook
calls, and it is safe to re-run: a second run changes nothing.

Steps, all inside one transaction:

1. ``Warehouse`` rows with ``company`` NULL are assigned to the company.
2. Every active staff ``auth.User`` with no ``UserProfile`` gets one. Before
   identity step 8 the profile also got a ``local:<user pk>`` placeholder in
   the required ``supabase_uid`` column (``--placeholder-uid-prefix``); step 8
   dropped the column and the flag. Placeholders written earlier survive as
   inert ``provider="local"`` identities.
3. ``UserProfile`` rows with ``user`` NULL are listed in the report and never
   linked: linking a profile to a user is a human decision. Profiles with
   ``company`` NULL (linked or not) are assigned to the company.
4. Every other model whose default manager is ``CompanyScopedManager`` is
   reported with its count of NULL-company rows. Rows are only changed when
   the model is named by a repeatable ``--also app_label.Model`` flag.
   A model listed in ``SOURCED_BACKFILLS`` first takes its company from its
   own related rows, in that backfill's order, and only the rest get
   ``--company`` (``crm.ActivityLog``: lead, customer, performer).

``--dry-run`` runs every step and then rolls the transaction back.
"""

import uuid

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.managers import CompanyScopedManager
from core.models import Company, UserProfile
from core.service_accounts import service_account_user_ids
from crm.backfill import backfill_activity_company, format_counts
from inventory.models import Warehouse

# Models whose --also backfill derives the company from related rows before
# falling back to --company: label -> callable(model, company, now) -> summary line.
SOURCED_BACKFILLS = {
    "crm.ActivityLog": lambda model, company, now: format_counts(
        backfill_activity_company(model, Company, fallback=company, now=now)
    ),
}


def _resolve_company(value):
    """Return the single Company whose pk or exact name matches ``value``."""
    q = Q(name=value)
    try:
        q |= Q(pk=uuid.UUID(value))
    except ValueError:
        pass
    matches = list(Company.objects.filter(q).order_by("name"))
    if not matches:
        raise CommandError(f"No Company matches {value!r} (by uuid or exact name).")
    if len(matches) > 1:
        listing = ", ".join(f"{c.name!r} ({c.pk})" for c in matches)
        raise CommandError(
            f"{value!r} is ambiguous; it matches {len(matches)} companies: {listing}. "
            "Pass the company uuid instead."
        )
    return matches[0]


def _scoped_models():
    """Concrete models whose default manager is CompanyScopedManager."""
    found = []
    for model in apps.get_models():
        if model._meta.proxy:
            continue
        if isinstance(model._default_manager, CompanyScopedManager):
            found.append(model)
    return found


def _assign_company(model, company, now):
    """Set ``company`` on NULL-company rows; bump ``updated_at`` when present."""
    values = {"company": company}
    if any(f.name == "updated_at" for f in model._meta.get_fields()):
        values["updated_at"] = now
    return model._base_manager.filter(company__isnull=True).update(**values)


class Command(BaseCommand):
    help = (
        "Backfill tenancy on a single-company deployment: assign NULL-company "
        "warehouses and profiles to --company, create missing UserProfile rows "
        "for active users, and report (or with --also, fix) other company-scoped "
        "models. Idempotent. Users are never linked to unlinked profiles."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--company",
            required=True,
            help="Company uuid or exact name. Must match exactly one Company.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Run every step, report it, then roll back. Nothing is saved.",
        )
        parser.add_argument(
            "--also",
            action="append",
            default=[],
            metavar="APP_LABEL.MODEL",
            help="Backfill NULL company on this CompanyScopedManager model. "
            "Repeatable. Without it the model is only reported.",
        )

    def _resolve_also(self, labels):
        scoped = {m._meta.label: m for m in _scoped_models()}
        chosen = {}
        for label in labels:
            if label not in scoped:
                raise CommandError(
                    f"--also {label!r} is not a CompanyScopedManager model. "
                    f"Choices: {', '.join(sorted(scoped))}."
                )
            chosen[label] = scoped[label]
        return chosen

    def handle(self, *args, **options):
        company = _resolve_company(options["company"])
        also = self._resolve_also(options["also"])
        dry_run = options["dry_run"]
        now = timezone.now()

        self.stdout.write(f"backfill_tenancy: company {company.name!r} ({company.pk})")
        self.stdout.write(f"mode: {'DRY RUN' if dry_run else 'apply'}")

        with transaction.atomic():
            self._step_warehouses(company, now)
            self._step_create_profiles(company, now)
            self._step_profiles(company, now)
            self._step_scoped_models(company, also, now)
            if dry_run:
                transaction.set_rollback(True)

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN (rolled back): no changes were saved."))
        else:
            self.stdout.write(self.style.SUCCESS("OK"))

    def _step_warehouses(self, company, now):
        count = _assign_company(Warehouse, company, now)
        self.stdout.write(f"step 1 warehouses without company: {count} assigned")

    def _step_create_profiles(self, company, now):
        User = get_user_model()
        # A customer-only account (identity step 3) never gets a staff profile:
        # UserProfile is the back-office profile (identity-unification §2.1).
        # Nor does a service account (svc-*, the storefront, api-service): a
        # profile pins it to a company and silently grants that company's
        # rows (identity step 5); `create_api_key --company` does it on purpose.
        users = (
            User.objects.filter(is_active=True, userprofile__isnull=True)
            .exclude(customer__isnull=False, is_staff=False, is_superuser=False)
            .exclude(pk__in=service_account_user_ids())
            .order_by("pk")
        )
        created = 0
        for user in users:
            UserProfile.objects.create(
                user=user,
                company=company,
                display_name=(user.get_full_name() or user.username)[:255],
            )
            created += 1
        self.stdout.write(f"step 2 active users without profile: {created} created")

    def _step_profiles(self, company, now):
        unlinked = list(
            UserProfile.objects.filter(user__isnull=True)
            .order_by("display_name", "pk")
            .values_list("id", "display_name", "legacy_external_id")
        )
        null_company = UserProfile.objects.filter(company__isnull=True).update(
            company=company, updated_at=now
        )
        self.stdout.write(
            f"step 3 profiles without company: {null_company} assigned; "
            f"unlinked profiles (user NULL, NOT linked): {len(unlinked)}"
        )
        for profile_id, display_name, uid in unlinked:
            suffix = f" [{uid}]" if uid else ""
            self.stdout.write(f"    - {profile_id} {display_name or '(no display name)'}{suffix}")

    def _step_scoped_models(self, company, also, now):
        self.stdout.write("step 4 company-scoped models with NULL company:")
        for model in _scoped_models():
            label = model._meta.label
            nulls = model._base_manager.filter(company__isnull=True).count()
            if label in also and label in SOURCED_BACKFILLS:
                summary = SOURCED_BACKFILLS[label](model, company, now)
                self.stdout.write(f"    - {label}: {nulls} NULL; {summary} (--also)")
            elif label in also:
                assigned = _assign_company(model, company, now)
                self.stdout.write(f"    - {label}: {assigned} assigned (--also)")
            else:
                self.stdout.write(f"    - {label}: {nulls} NULL (not modified)")
