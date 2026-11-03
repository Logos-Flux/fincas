"""``manage.py seed_release_test`` (docs/release-hardening-plan.md 9.1).

Two fresh seeds of an empty database (flushed between them) give the same
ids and counts for the main models; no password reaches stdout; the logins
file is 0600; ``--overlay`` is idempotent and refuses an ambiguous company.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from io import StringIO
from pathlib import Path

from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TransactionTestCase

from core.models import Company
from core.release_test import HARNESS_COMPANY_ID

HASHED_MODELS = (
    "core.Company", "auth.User", "core.UserProfile", "inventory.Warehouse", "inventory.WarehouseStock",
    "purchasing.Supplier", "pricing.PriceList", "pricing.PriceListItem", "products.Product",
    "products.ProductVariant", "customers.Customer", "parties.Party", "parties.PartyEmail",
    "parties.PartyPhone", "crm.Engagement", "crm.EngagementStageChange", "crm.Task",
    "communications.Communication", "accounting.Account", "accounting.DocumentPostingRule",
    "accounting.FiscalPeriod", "unify.ImportBatch", "unify.StagedRow", "core.AuditLog",
)


def fingerprint() -> tuple[dict[str, str], dict[str, int]]:
    """Per model: a digest of (count, sorted pks), and the count."""
    digests, counts = {}, {}
    for label in HASHED_MODELS:
        model = apps.get_model(label)
        pks = sorted(str(pk) for pk in model._base_manager.values_list("pk", flat=True))
        counts[label] = len(pks)
        digests[label] = hashlib.sha256(f"{len(pks)}:{','.join(pks)}".encode()).hexdigest()
    return digests, counts


def read_logins(path: Path) -> list[tuple[str, str, str]]:
    rows = []
    for line in path.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        username, password, role = line.split()
        rows.append((username, password, role))
    return rows


class SeedReleaseTestTests(TransactionTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name) / "run"
        # The runner's teardown flush keeps integer sequences; a fresh
        # database starts auth.User at 1, so reset before the first seed.
        call_command("flush", verbosity=0, interactive=False)

    def seed(self, *args, **opts) -> str:
        out = StringIO()
        call_command("seed_release_test", *args, runs_dir=str(self.runs), stdout=out, **opts)
        return out.getvalue()

    def test_two_fresh_seeds_match_and_print_no_password(self):
        out = self.seed(as_of="2026-10-15")
        first, counts = fingerprint()
        logins = read_logins(self.runs / "logins.txt")
        self.assertEqual(stat.S_IMODE(os.stat(self.runs / "logins.txt").st_mode), 0o600)
        roles = {role for _u, _p, role in logins}
        self.assertIn("superuser", roles)
        self.assertIn("staff", roles)
        for _username, password, _role in logins:
            self.assertNotIn(password, out)
        self.assertNotIn("credentials", out.lower())
        self.assertIn(f"GOLDEN_PORTAL_COMPANY={HARNESS_COMPANY_ID}", out)
        self.assertIn("DATABASE_URL=", out)
        self.assertNotIn("golden_secret", out)
        self.assertEqual(Company.objects.count(), 1)
        self.assertEqual(Company.objects.get().pk, HARNESS_COMPANY_ID)
        self.assertEqual(counts["inventory.Warehouse"], 2)
        self.assertGreaterEqual(counts["purchasing.Supplier"], 3)
        self.assertEqual(counts["accounting.FiscalPeriod"], 2)
        self.assertEqual(counts["unify.StagedRow"], 3)
        self.assertGreaterEqual(counts["crm.Engagement"], 60)
        self.assertTrue((self.runs / "fixtures" / "harness-products.csv").exists())

        call_command("flush", verbosity=0, interactive=False)
        self.assertEqual(Company.objects.count(), 0)
        # A different day and time: the pinned clock and the seeded uuid
        # stream make the second run identical.
        self.seed(as_of="2026-10-15")
        second, counts_again = fingerprint()
        self.assertEqual(counts, counts_again)
        for label in HASHED_MODELS:
            with self.subTest(model=label):
                self.assertEqual(first[label], second[label], f"{label}: ids differ between two fresh seeds")

    def test_fresh_seed_refuses_a_foreign_company(self):
        Company.objects.create(name="Somebody Else Ltd")
        with self.assertRaisesMessage(CommandError, "use --overlay"):
            self.seed()

    def test_overlay_is_idempotent_and_needs_a_company_when_ambiguous(self):
        self.seed(my_day=False)
        before, _ = fingerprint()
        self.seed(overlay=True)
        self.seed(overlay=True)
        after, counts = fingerprint()
        self.assertEqual(before, after)
        self.assertEqual(counts["accounting.FiscalPeriod"], 2)  # the overlay adds no periods
        Company.objects.create(name="Second Co")
        with self.assertRaisesMessage(CommandError, "--company"):
            self.seed(overlay=True)
        self.seed(overlay=True, company=str(HARNESS_COMPANY_ID))
