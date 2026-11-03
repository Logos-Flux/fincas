"""Seed the release-test database (docs/release-hardening-plan.md 9.1; docs/release-testing.md).

    manage.py seed_release_test                       # fresh seed into an empty (migrated) database
    manage.py seed_release_test --as-of 2026-10-15    # the day the data is written for
    manage.py seed_release_test --overlay             # role logins + fixtures onto an existing database
    manage.py seed_release_test --overlay --company <uuid>

Fresh mode composes the existing seeds, ``seed_demo_data`` (catalog, chart
of accounts, posting rules; it calls ``seed_parties`` and ``seed_crm``, which
are called again here, idempotently), under one company with the fixed id
``core.release_test.HARNESS_COMPANY_ID`` (created first with that pk, so
``GOLDEN_PORTAL_COMPANY`` is known before any command runs), and adds what
the journeys need: a staff login per staff role (``docs/roles.md``: the
built-in ``staff`` plus every staff role in ``GOLDEN_ROLES``), stock in a
second warehouse, two more suppliers (one with ship-from locations), a
closed fiscal period beside an open one, duplicate parties, a staged CSV
import batch, and the first staff login's "My day" (``seed_my_day``).

Deterministic: the clock is pinned to ``--as-of`` (10:00 in TIME_ZONE) and
every ``uuid4`` primary key comes from one seeded stream
(``core.release_test.deterministic_uuids``), so two runs, on different days,
give the same ids and counts (``core/tests/test_seed_release_test.py``
hashes them). The demo clock is anchored to ``--as-of``; the harness runs
``demo_roll_dates --force`` afterwards to bring the dates to today.

Passwords are random and never printed: ``seed_demo_data`` gets a random
``admin_password`` and its credentials line is dropped; every login goes to
``<runs-dir>/logins.txt`` (mode 0600; ``--runs-dir``, else
``$HARNESS_RUN_DIR``, else ``.runs/<git sha>``). The command ends by printing
the environment lines the harness expects (``GOLDEN_PORTAL_COMPANY``,
``DATABASE_URL`` with the password masked).

``--overlay`` adds the role logins and the additive fixtures (warehouse,
suppliers, duplicate parties, import batch) to an existing database, for a
clone of the scrubbed copy: idempotent, no second company, no clock pin,
no fiscal periods (the clone has real books). It takes ``--company``, or
the only Company, else refuses.
"""

from __future__ import annotations

import datetime
import io
import os
import secrets
import subprocess
import uuid
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounting.models import FiscalPeriod
from core import demo_clock
from core.models import Company, ERPUserSettings, UserProfile
from core.release_test import (
    DEFAULT_AS_OF,
    HARNESS_COMPANY_ID,
    HARNESS_COMPANY_NAME,
    as_of_moment,
    deterministic_uuids,
    frozen_now,
    seed_paid_shipped_order,
)
from core.role_registry import ROLES, STAFF_ROLE
from core.roles import grant_role
from customers.models import Customer
from inventory.models import Warehouse, WarehouseStock
from products.models import ProductVariant
from purchasing.models import Supplier
from unify.models import ImportBatch, StagedRow, content_hash

LOGIN_PREFIX = "harness-"
SUPERUSER = "admin"

# (code, name, city, state, postal, bin prefix)
SECOND_WAREHOUSE = ("WH-EAST", "East Coast Fulfillment", "Harrisburg", "PA", "17101", "E")
# Quantities per seeded SKU in the second warehouse (seed_demo_data's catalog order).
SECOND_WAREHOUSE_STOCK = {"ACT-500W": 12, "MLC-CORE-08": 0, "BRK-90-HD": 120, "SNR-LASER-30": 3}

SUPPLIERS = (
    {"supplier_code": "SUPP-002", "company_name": "Keystone Drop-Ship Partners", "display_name": "Keystone",
     "supplier_type": Supplier.SupplierType.DROP_SHIP, "primary_contact_name": "Lena Ortiz", "primary_contact_email": "orders@keystone-dropship.example.com",
     "po_email": "po@keystone-dropship.example.com", "is_active": True, "is_approved": True,
     "address_line_1": "12 Canal Street", "city": "Scranton", "state_province": "PA", "postal_code": "18503",
     "ship_from_locations": [{"id": "keystone-west", "label": "Keystone West, Reno, NV 89501"},
                             {"id": "keystone-south", "label": "Keystone South, Dallas, TX 75201"}]},
    {"supplier_code": "SUPP-003", "company_name": "Meridian Sensor Works", "display_name": "Meridian",
     "supplier_type": Supplier.SupplierType.MANUFACTURER, "primary_contact_name": "Tomas Reiss",
     "primary_contact_email": "sales@meridian-sensors.example.com", "is_active": True, "is_approved": False,
     "address_line_1": "900 Research Drive", "city": "Ann Arbor", "state_province": "MI", "postal_code": "48103"},
)

# Two pairs of duplicates for R8 and party_dedupe_report: one person twice
# (same name and phone, different addresses), one organisation twice.
DUPLICATE_CUSTOMERS = (
    {"first_name": "Dana", "last_name": "Whitfield", "email": "dana.whitfield@example.com",
     "phone": "+1-312-555-0177", "company_name": "Whitfield Fabrication",
     "customer_type": Customer.CustomerType.BUSINESS},
    {"first_name": "Dana", "last_name": "Whitfield", "email": "d.whitfield@example.org",
     "phone": "+1-312-555-0177", "company_name": "Whitfield Fabrication Inc.",
     "customer_type": Customer.CustomerType.BUSINESS},
    {"first_name": "Harbor", "last_name": "Lights", "email": "accounts@harborlights.example.com",
     "phone": "+1-401-555-0150", "company_name": "Harbor Lights Marine",
     "customer_type": Customer.CustomerType.BUSINESS},
    {"first_name": "Harbor", "last_name": "Lights", "email": "ap@harborlights.example.com",
     "phone": "+1-401-555-0150", "company_name": "Harbor Lights Marine LLC",
     "customer_type": Customer.CustomerType.BUSINESS},
)

IMPORT_SOURCE = "harness-products.csv"
IMPORT_HEADERS = ("sku", "title", "brand", "retail_price", "stock")
IMPORT_ROWS = (
    ("HRN-CBL-3M", "Shielded Control Cable 3 m", "Apex Dynamics", "18.40", "200"),
    ("HRN-CBL-10M", "Shielded Control Cable 10 m", "Apex Dynamics", "49.00", "80"),
    ("HRN-PSU-24V", "24 V DIN-Rail Power Supply 120 W", "Vanguard Precision", "132.50", "25"),
)


def _git_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                             cwd=settings.BASE_DIR, timeout=10)
        return out.stdout.strip() or "nosha"
    except (OSError, subprocess.SubprocessError):
        return "nosha"


def masked_database_url() -> str:
    db = settings.DATABASES["default"]
    engine = db.get("ENGINE", "")
    if "postgres" in engine:
        user = db.get("USER") or ""
        host = db.get("HOST") or "localhost"
        port = db.get("PORT") or "5432"
        auth = f"{user}:***@" if user else ""
        return f"postgres://{auth}{host}:{port}/{db.get('NAME', '')}"
    return f"{engine.rsplit('.', 1)[-1]}:///{db.get('NAME', '')}"


class Command(BaseCommand):
    help = "Seed the release-test database: one company, a login per staff role, journey fixtures; deterministic."

    def add_arguments(self, parser):
        parser.add_argument("--as-of", type=datetime.date.fromisoformat, default=DEFAULT_AS_OF,
                            help=f"The day the fresh data is written for (YYYY-MM-DD, default {DEFAULT_AS_OF}).")
        parser.add_argument("--overlay", action="store_true",
                            help="Add role logins and fixtures to an existing database (no seeds, no clock pin).")
        parser.add_argument("--company", help="--overlay: the Company pk (default: the only Company, else refuse).")
        parser.add_argument("--runs-dir", help="Where logins.txt goes (default $HARNESS_RUN_DIR or .runs/<sha>).")
        parser.add_argument("--my-day", action="store_true", default=None,
                            help="Fresh mode: also seed the first staff login's My day (default on).")
        parser.add_argument("--no-my-day", dest="my_day", action="store_false")

    # ── entry ───────────────────────────────────────────────────────────────

    def handle(self, *args, **opts):
        self.logins: list[tuple[str, str, str]] = []  # (username, password, role)
        runs_dir = Path(opts["runs_dir"] or os.environ.get("HARNESS_RUN_DIR") or
                        Path(settings.BASE_DIR) / ".runs" / _git_sha())
        if opts["overlay"]:
            company = self._overlay_company(opts["company"])
            self.stdout.write(f"Overlay onto company {company.pk} ({company.name})")
            with transaction.atomic():
                self._role_logins(company)
                self._fixtures(company, periods=False)
        else:
            self._refuse_foreign_data()
            as_of = opts["as_of"]
            if isinstance(as_of, str):  # call_command() skips argparse's type
                as_of = datetime.date.fromisoformat(as_of)
            self.stdout.write(f"Fresh seed as of {as_of} into {masked_database_url()}")
            with frozen_now(as_of_moment(as_of)), deterministic_uuids(), transaction.atomic():
                company = self._company()
                self._base_seeds(company)
                self._role_logins(company)
                self._fixtures(company, periods=True, as_of=as_of)
                if opts["my_day"] is not False:
                    call_command("seed_my_day", user=f"{LOGIN_PREFIX}{STAFF_ROLE}", company=str(company.pk),
                                 force=True, stdout=self.stdout)
                demo_clock.ensure_anchor(as_of, stdout=self.stdout)
        path = self._write_logins(runs_dir)
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(self.logins)} logins -> {path} (mode 0600). Environment the harness expects:"))
        self.stdout.write(f"GOLDEN_PORTAL_COMPANY={company.pk}")
        self.stdout.write(f"DATABASE_URL={masked_database_url()}")

    # ── company ─────────────────────────────────────────────────────────────

    def _refuse_foreign_data(self):
        other = Company.objects.exclude(pk=HARNESS_COMPANY_ID).order_by("created_at").first()
        if other is not None:
            raise CommandError(
                f"Refusing a fresh seed: the database already holds Company {other.pk} ({other.name!r}). "
                "A fresh seed expects an empty, migrated database; use --overlay for an existing one."
            )

    def _company(self) -> Company:
        company, created = Company.objects.get_or_create(
            pk=HARNESS_COMPANY_ID,
            defaults={"name": HARNESS_COMPANY_NAME, "website_url": "https://golden-erp.org",
                      "phone": "+1-800-555-0199", "notes": "Release-test company (seed_release_test)."},
        )
        self.stdout.write(f"  Company {company.pk} {'created' if created else 'exists'}.")
        return company

    def _overlay_company(self, pk: str | None) -> Company:
        if pk:
            try:
                return Company.objects.get(pk=uuid.UUID(pk))
            except (ValueError, Company.DoesNotExist) as exc:
                raise CommandError(f"--company {pk!r}: no such Company.") from exc
        companies = list(Company.objects.order_by("created_at")[:2])
        if len(companies) == 1:
            return companies[0]
        raise CommandError(
            f"--overlay needs --company <uuid>: the database holds {Company.objects.count()} companies."
        )

    # ── the composed seeds ──────────────────────────────────────────────────

    def _base_seeds(self, company: Company):
        admin_password = secrets.token_urlsafe(18)
        captured = io.StringIO()
        call_command("seed_demo_data", admin_username=SUPERUSER, admin_password=admin_password,
                     admin_email="admin@example.com", stdout=captured)
        for line in captured.getvalue().splitlines():
            # seed_demo_data ends with "Admin credentials: admin / <password>".
            if admin_password in line or "credentials" in line.lower():
                continue
            self.stdout.write(line)
        self.logins.append((SUPERUSER, admin_password, "superuser"))
        # Called by seed_demo_data already; explicit and idempotent, so the
        # composition does not depend on what seed_demo_data chooses to call.
        call_command("seed_parties", count=40, company=str(company.pk), stdout=self.stdout)
        call_command("seed_crm", count=60, company=str(company.pk), stdout=self.stdout)

    # ── staff logins ────────────────────────────────────────────────────────

    def _role_logins(self, company: Company):
        User = get_user_model()
        roles = [STAFF_ROLE] + sorted(name for name, role in ROLES.items() if role.staff and name != STAFF_ROLE)
        for role in roles:
            username = f"{LOGIN_PREFIX}{role}"
            password = secrets.token_urlsafe(12)
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"email": f"{username}@example.com", "first_name": "Harness",
                          "last_name": ROLES[role].label},
            )
            user.set_password(password)
            user.is_active = True
            user.save()
            # The baseline staff role carries the back-office permissions; the
            # named role is what R2 and R10 refuse or allow by.
            grant_role(user, STAFF_ROLE)
            if role != STAFF_ROLE:
                grant_role(user, role)
            UserProfile.objects.get_or_create(
                user=user, defaults={"company": company, "display_name": f"Harness {ROLES[role].label}"})
            ERPUserSettings.objects.update_or_create(
                user=user, defaults={"must_change_password": False, "email_verified": True})
            self.logins.append((username, password, role))
            self.stdout.write(f"  Login {username} ({role}) {'created' if created else 'reset'}.")
        # Every staff profile gets its staff party (parties backfill step 6),
        # so reconcile_parties --check reports nothing pending on the seed.
        call_command("reconcile_parties", fix=True, company=str(company.pk), stdout=self.stdout)

    # ── fixtures ────────────────────────────────────────────────────────────

    def _fixtures(self, company: Company, *, periods: bool, as_of: datetime.date | None = None):
        self._second_warehouse(company)
        self._suppliers(company)
        if periods:
            self._fiscal_periods(company, as_of)
        self._duplicate_parties(company)
        self._import_batch(company)
        # R5 starts from a paid, shipped order (core.release_test.seed_paid_shipped_order).
        seed_paid_shipped_order(company, stdout=self.stdout)

    def _second_warehouse(self, company: Company):
        code, name, city, state, postal, bin_prefix = SECOND_WAREHOUSE
        warehouse, created = Warehouse.objects.get_or_create(
            warehouse_code=code,
            defaults={"name": name, "company": company, "address_line_1": "2 Dock Road", "city": city,
                      "state_province": state, "postal_code": postal, "country": "US", "is_active": True},
        )
        variants = ProductVariant.objects.filter(sku__in=SECOND_WAREHOUSE_STOCK).order_by("sku")
        for n, variant in enumerate(variants, 1):
            WarehouseStock.objects.get_or_create(
                warehouse=warehouse, variant=variant,
                defaults={"quantity": Decimal(SECOND_WAREHOUSE_STOCK[variant.sku]),
                          "bin_location": f"{bin_prefix}-{n:02d}-01", "reorder_level": 5},
            )
        self.stdout.write(f"  Warehouse {code} {'created' if created else 'exists'} ({variants.count()} stock rows).")

    def _suppliers(self, company: Company):
        # A shipment group keeps a vendor only when it belongs to the order's
        # company (orders.erp_views._save_shipment_groups); bind them.
        for spec in SUPPLIERS:
            spec = dict(spec)
            code = spec.pop("supplier_code")
            _, created = Supplier.objects.get_or_create(supplier_code=code, defaults={**spec, "company": company})
            self.stdout.write(f"  Supplier {code} {'created' if created else 'exists'}.")

    def _fiscal_periods(self, company: Company, as_of: datetime.date):
        prior = as_of.year - 1
        closed, created = FiscalPeriod.objects.get_or_create(
            company=company, name=f"FY{prior} (closed)",
            defaults={"start_date": datetime.date(prior, 1, 1), "end_date": datetime.date(prior, 12, 31),
                      "status": FiscalPeriod.Status.CLOSED,
                      "closed_at": as_of_moment(datetime.date(as_of.year, 1, 15))},
        )
        FiscalPeriod.objects.get_or_create(
            company=company, name=f"FY{as_of.year}-{as_of.year + 1} (open)",
            defaults={"start_date": datetime.date(as_of.year, 1, 1),
                      "end_date": datetime.date(as_of.year + 1, 12, 31), "status": FiscalPeriod.Status.OPEN},
        )
        self.stdout.write(f"  Fiscal periods: {closed.name} {'created' if created else 'exists'}, one open.")

    def _duplicate_parties(self, company: Company):
        made = 0
        for spec in DUPLICATE_CUSTOMERS:
            if Customer.objects.filter(company=company, email__iexact=spec["email"]).exists():
                continue
            # Customer.save() runs the party sync: each row gets its person and organisation party.
            Customer.objects.create(company=company, pricing_tier=Customer.PricingTier.WHOLESALE,
                                    is_active=True, **spec)
            made += 1
        self.stdout.write(f"  Duplicate parties: {made} customers created ({len(DUPLICATE_CUSTOMERS)} wanted).")

    def _import_batch(self, company: Company):
        batch, created = ImportBatch.objects.get_or_create(
            company=company, source_kind=ImportBatch.SourceKind.CSV, source_name=IMPORT_SOURCE,
            defaults={"entity_kind": "product", "status": ImportBatch.Status.STAGED},
        )
        if created:
            for number, values in enumerate(IMPORT_ROWS, 1):
                raw = dict(zip(IMPORT_HEADERS, values))
                StagedRow.objects.create(batch=batch, row_number=number, raw=raw, content_hash=content_hash(raw))
        self.stdout.write(f"  Import batch {IMPORT_SOURCE} {'created' if created else 'exists'} ({len(IMPORT_ROWS)} rows).")

    # ── logins file ─────────────────────────────────────────────────────────

    def _write_logins(self, runs_dir: Path) -> Path:
        runs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = runs_dir / "logins.txt"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("# username password role  (seed_release_test; never commit)\n")
            for username, password, role in self.logins:
                fh.write(f"{username} {password} {role}\n")
        os.chmod(path, 0o600)
        # The import fixture as a file too, for the CSV-import journeys.
        fixtures = runs_dir / "fixtures"
        fixtures.mkdir(exist_ok=True)
        with open(fixtures / IMPORT_SOURCE, "w", encoding="utf-8", newline="") as fh:
            fh.write(",".join(IMPORT_HEADERS) + "\n")
            for row in IMPORT_ROWS:
                fh.write(",".join(row) + "\n")
        return path
