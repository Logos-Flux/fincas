"""``manage.py check_sidecar`` (phase4-fields.md 3.3)."""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from core.models import Company
from crm.models import Task
from parties.models import Party

DEFINITIONS = {"party": [
    {"key": "custom.budget", "label": "Budget", "type": "decimal"},
    {"key": "custom.job_title", "label": "Job title", "type": "string", "pii": True},
    {"key": "custom.package", "label": "Package", "type": "choice",
     "choices": [["accelerator", "Accelerator"], ["other", "Other"]]},
]}

INVENTORY = (
    "provider,object,source_id,source_label,source_type,options,populated,total,filtered,sorted,"
    "reported,evidence,pii,decision,target,notes\n"
    "hubspot,contacts,hs_lead_status,Lead status,enumeration,,10,20,no,no,no,,no,sidecar,"
    "hubspot.hs_lead_status,\n"
)


def run(*args):
    out = io.StringIO()
    try:
        call_command("check_sidecar", *args, stdout=out)
    except CommandError as exc:
        return 1, out.getvalue() + str(exc)
    return 0, out.getvalue()


@override_settings(GOLDEN_FIELD_DEFINITIONS=DEFINITIONS)
class CheckSidecarTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")

    def party(self, metadata):
        return Party.objects.create(company=self.company, kind="organisation", display_name="P",
                                    origin="erp", metadata=metadata)

    def test_clean_data_exits_zero(self):
        self.party({"name_inferred": True, "custom.budget": "10.00", "custom.job_title": "a@b.example",
                    "hubspot.hs_lead_status": "new"})
        code, out = run()
        self.assertEqual(code, 0, out)
        self.assertIn("0 finding(s)", out)
        self.assertIn("not checked against an inventory", out)

    def test_each_finding(self):
        cases = {
            "invalid_value": {"custom.budget": "lots"},
            "undefined_custom_key": {"custom.nope": 1},
            "pii_shaped_value": {"hubspot.owner_email": "someone@example.com"},
            "unregistered_system_key": {"note": "x"},
            "invalid_key": {"Bad Key": 1},
            "bad_value": {"hubspot.nested": {"a": 1}},
            "too_large": {f"hubspot.k{i}": i for i in range(201)},
        }
        for code, metadata in cases.items():
            with self.subTest(code=code):
                party = self.party(metadata)
                status, out = run("--model", "party")
                self.assertEqual(status, 1, out)
                self.assertIn(f"party: {code}", out)
                self.assertIn(str(party.pk), out)
                party.delete()

    def test_pii_shaped_values_under_a_pii_definition_are_fine(self):
        self.party({"custom.job_title": "+1 212 555 0143"})
        self.assertEqual(run("--model", "party")[0], 0)
        self.party({"hubspot.phone_copy": "+1 212 555 0143"})
        self.assertIn("pii_shaped_value", run("--model", "party")[1])

    def test_a_task_sidecar_holds_system_keys_per_model(self):
        Task.objects.create(company=self.company, title="T", kind="todo", metadata={"name_inferred": True})
        status, out = run("--model", "task")
        self.assertEqual(status, 1)
        self.assertIn("task: unregistered_system_key", out)

    def test_inventory(self):
        self.party({"hubspot.hs_lead_status": "new", "hubspot.unlisted": "x"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hubspot.csv"
            path.write_text(INVENTORY, encoding="utf-8")
            status, out = run("--model", "party", "--inventory", str(path))
            self.assertEqual(status, 1, out)
            self.assertIn("provider_key_not_in_inventory x1", out)
            self.assertIn("hubspot.unlisted", out)
            with override_settings(GOLDEN_FIELD_INVENTORIES=[str(path)]):
                self.assertIn("hubspot.unlisted", run("--model", "party")[1])
            path.write_text("provider\n", encoding="utf-8")
            status, out = run("--model", "party", "--inventory", str(path))
            self.assertEqual(status, 1)
            self.assertIn("missing column", out)

    def test_stats(self):
        self.party({"custom.budget": "10.00"})
        status, out = run("--model", "party", "--stats")
        self.assertEqual(status, 0, out)
        self.assertIn("stats party custom.budget: 1 row(s)", out)
