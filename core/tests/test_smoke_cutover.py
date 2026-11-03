"""``manage.py smoke_cutover`` (runbook §11, plan 9.9): the ok, degraded and
roll-back paths, the per-check line format, ``--json`` and the shared exit
codes."""

from __future__ import annotations

import io
import json
import re

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.management import release_exit_codes as rc
from core.management.commands import smoke_cutover
from core.models import Company, OutboxEvent
from core.storefront import STOREFRONT_USERNAME, ensure_storefront_identity
from core.tests.helpers import make_staff_user

LINE = re.compile(r"^(?P<name>[a-z_]+): observed=(?P<observed>.+) threshold=(?P<threshold>.+) -> "
                  r"(?P<status>OK|DEGRADED|ROLL BACK) \((?P<where>.+)\)$")


def run(*args, **opts):
    out, err = io.StringIO(), io.StringIO()
    code = rc.OK
    try:
        call_command("smoke_cutover", *args, stdout=out, stderr=err, **opts)
    except SystemExit as exc:
        code = exc.code
    return code, out.getvalue(), err.getvalue()


class SmokeCutoverTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")
        make_staff_user("ops", "ops@example.com", "pw")
        ensure_storefront_identity()

    def test_ok_path(self):
        code, out, _ = run()
        self.assertEqual(code, rc.OK, out)
        lines = out.splitlines()
        checks = {}
        for line in lines:
            m = LINE.match(line)
            if m:
                checks[m.group("name")] = m.groupdict()
        self.assertEqual(
            sorted(checks),
            ["companies", "engagements_drift", "migrations", "outbox_dead", "outbox_overdue",
             "parties_drift", "staff_login", "storefront_identity"],
        )
        self.assertTrue(all(c["status"] == "OK" for c in checks.values()), checks)
        self.assertEqual(checks["companies"]["observed"], "1")
        self.assertEqual(checks["companies"]["threshold"], "1")
        self.assertEqual(checks["migrations"]["observed"], "0 unapplied")
        self.assertIn("1 staff principal(s)", checks["staff_login"]["observed"])
        self.assertIn("7/7 permissions", checks["storefront_identity"]["observed"])
        self.assertIn("pinned=none (by design)", checks["storefront_identity"]["observed"])
        self.assertEqual(checks["outbox_dead"]["threshold"], "<=0")
        for name, _how in smoke_cutover.MANUAL_ROWS:
            self.assertTrue(any(line.startswith(f"{name}: manual -> ") for line in lines), name)
        self.assertTrue(any(line.startswith("schema_parity: manual -> not re-run here") for line in lines))
        self.assertEqual(lines[-1], smoke_cutover.GUIDANCE[rc.STATUS_OK])

    def test_dead_outbox_rows_degrade(self):
        OutboxEvent.objects.create(consumer="webhooks", event="e", idempotency_key="k1", state="dead",
                                   completed_at=timezone.now())
        code, out, _ = run()
        self.assertEqual(code, rc.DEGRADED)
        self.assertIn("outbox_dead: observed=1 threshold=<=0 -> DEGRADED (docs/deployment.md §8", out)
        self.assertEqual(out.splitlines()[-1], smoke_cutover.GUIDANCE[rc.STATUS_DEGRADED])
        # The threshold is the operator's call.
        code, out, _ = run("--dead-max", "1")
        self.assertEqual(code, rc.OK, out)
        self.assertIn("outbox_dead: observed=1 threshold=<=1 -> OK", out)

    def test_overdue_pending_row_degrades(self):
        OutboxEvent.objects.create(consumer="webhooks", event="e", idempotency_key="k2",
                                   next_attempt_at=timezone.now() - timezone.timedelta(hours=2))
        code, out, _ = run()
        self.assertEqual(code, rc.DEGRADED)
        self.assertRegex(out, r"outbox_overdue: observed=12\d min threshold=<=30 min -> DEGRADED")

    def test_two_companies_roll_back(self):
        Company.objects.create(name="Other")
        code, out, _ = run()
        self.assertEqual(code, rc.ROLL_BACK)
        self.assertRegex(out, r"companies: observed=2 \(.*'Acme'.*'Other'.*\) threshold=1 -> ROLL BACK \(runbook §7")
        self.assertEqual(out.splitlines()[-1], smoke_cutover.GUIDANCE[rc.STATUS_ROLL_BACK])
        self.assertIn("§12", out.splitlines()[-1])

    def test_roll_back_outranks_degraded(self):
        Company.objects.create(name="Other")
        OutboxEvent.objects.create(consumer="webhooks", event="e", idempotency_key="k3", state="dead",
                                   completed_at=timezone.now())
        code, _, _ = run()
        self.assertEqual(code, rc.ROLL_BACK)

    def test_no_staff_login_rolls_back(self):
        from django.contrib.auth.models import User

        User.objects.filter(username="ops").update(is_active=False)
        code, out, _ = run()
        self.assertEqual(code, rc.ROLL_BACK)
        self.assertIn("staff_login: observed=0 staff principal(s), 0 superuser(s) threshold=>=1 -> ROLL BACK", out)

    def test_tampered_storefront_identity_degrades(self):
        from django.contrib.auth.models import User

        User.objects.filter(username=STOREFRONT_USERNAME).update(is_staff=True)
        code, out, _ = run()
        self.assertEqual(code, rc.DEGRADED)
        self.assertIn("storefront_identity: observed=present, 7/7 permissions, pinned=none (by design), is_staff", out)
        self.assertIn("-> DEGRADED (runbook §9: manage.py ensure_storefront_identity)", out)
        User.objects.filter(username=STOREFRONT_USERNAME).delete()
        code, out, _ = run()
        self.assertEqual(code, rc.DEGRADED)
        self.assertIn("storefront_identity: observed=missing", out)

    def test_json_output(self):
        OutboxEvent.objects.create(consumer="webhooks", event="e", idempotency_key="k4", state="dead",
                                   completed_at=timezone.now())
        code, out, _ = run("--json")
        self.assertEqual(code, rc.DEGRADED)
        data = json.loads(out)
        self.assertEqual(data["status"], "DEGRADED")
        self.assertEqual(data["exit_code"], rc.DEGRADED)
        by_name = {c["name"]: c for c in data["checks"]}
        self.assertEqual(by_name["outbox_dead"], {
            "name": "outbox_dead", "observed": "1", "threshold": "<=0", "status": "DEGRADED",
            "where": "docs/deployment.md §8: manage.py outbox list --state dead, requeue_outbox",
        })
        self.assertEqual([m["name"] for m in data["manual"]], [n for n, _ in smoke_cutover.MANUAL_ROWS])
        self.assertEqual(data["guidance"], smoke_cutover.GUIDANCE[rc.STATUS_DEGRADED])

    def test_bad_threshold_is_usage(self):
        code, _, err = run("--dead-max", "-1")
        self.assertEqual(code, rc.USAGE)
        self.assertIn("thresholds must be >= 0", err)

    def test_exit_code_table(self):
        self.assertEqual((rc.OK, rc.USAGE, rc.DEGRADED, rc.ROLL_BACK), (0, 2, 10, 20))
        self.assertEqual(rc.worst(["OK", "ROLL BACK", "DEGRADED"]), "ROLL BACK")
        self.assertEqual(rc.worst([]), "OK")
