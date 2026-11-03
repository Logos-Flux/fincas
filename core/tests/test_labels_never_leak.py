"""Labels never reach machine-facing output (CRM Phase 3 §3.4, decision E11).

API payloads, CSV exports, PDFs, transactional emails, signal and webhook
payloads and ``erp_assist`` output are contracts with machines, auditors and
customers: they always use canonical names. Two guards:

1. **Static** — machine-facing templates must not load or use the ``labels``
   library, and machine-facing Python must not import the label reader. A
   deployment that wants "Proposal" on its PDF overrides the PDF template.
2. **Behavioural** — with every label key overridden at both the deployment
   and the tenant layer, a staff API response and the list views' CSV export
   are byte-identical to the same calls without overrides.

The spec's full leak test also renders a quote PDF, a document email and a
webhook payload under overrides; those templates are covered statically here
and join the behavioural half when the Phase 3 CRM resources land.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from core import label_registry
from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user

ROOT = Path(settings.BASE_DIR)
SKIP_DIRS = {".venv", "node_modules", "storefront", ".git", "staticfiles", "__pycache__"}

TEMPLATE_USES = re.compile(
    r"\{%\s*load\b[^%]*\blabels\b"   # {% load labels %} / {% load static labels %}
    r"|\{%\s*label\b"                 # {% label "…" %}
    r"|\|\s*label\b"                  # {{ "…"|label }}
    r"|\{\{\s*labels\."               # {{ labels.crm.engagement }}
)
PYTHON_USES = re.compile(
    r"\bcore\.labels\b|\blabel_registry\b|from\s+core\s+import\s+[^\n]*\blabels\b"
    r"|\bget_entity_label\b|\blabels_for_request\b"
)


def _walk(pattern: str):
    for path in ROOT.rglob(pattern):
        if SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            continue
        yield path


def machine_templates():
    """PDF / print / email / plain-text templates."""
    for path in _walk("templates/**/*"):
        if not path.is_file() or path.suffix not in (".html", ".txt"):
            continue
        rel = path.relative_to(ROOT).as_posix()
        name = path.name.lower()
        if (
            rel.startswith("documents/templates/")
            or "/emails/" in rel
            or path.suffix == ".txt"
            or any(word in name for word in ("email", "pdf", "print"))
        ):
            yield path


# Whole apps whose every module is machine-facing: webhook payloads, the
# assistant's tool output, and the importer (canonical files, mapping
# reports, applied rows). The importer's staff review screens may use labels
# (hand-off section 8), so its ERP view modules are left out.
MACHINE_APPS = ("webhooks", "erp_assist", "crm_import")
#: Add-on modules are machine-facing (docs/crm/phase6-adapters.md 2.2 rule 7):
#: export adapters write canonical files, live adapters write provider
#: requests, rows and outbox payloads. Only an add-on's staff UI modules
#: (``STAFF_UI_MODULES``) may read labels.
MACHINE_ADDONS = ("golden_erp/addons/",)
#: Single modules that build machine output beside staff pages: the CRM
#: reports' typed tables feed the report API (Phase 6 step 7), and the core
#: modules on the live-adapter path (webhook endpoint, poller, contract,
#: fixture replay).
MACHINE_MODULES = ("reporting/crm_reports.py", "core/integrations/webhook.py", "core/integrations/poll.py",
                   "core/integrations/transport.py", "core/plugins/crm_adapter.py",
                   "core/testing/integration_fixtures.py")
STAFF_UI_MODULES = ("views.py", "erp_views.py", "erp_urls.py", "urls.py", "forms.py")


def is_machine_python(rel: str) -> bool:
    if "/tests/" in rel or "/migrations/" in rel:
        return False
    parts = rel.split("/")
    if parts[-1] in ("serializers.py", "api.py", "signals.py", "services.py") or "management/commands" in rel:
        return True
    if parts[0] == "crm_import" and (parts[-1] in STAFF_UI_MODULES or "views" in parts[1:-1]):
        return False
    if rel.startswith(MACHINE_ADDONS):
        return parts[-1] not in STAFF_UI_MODULES
    if rel in MACHINE_MODULES:
        return True
    return parts[0] in MACHINE_APPS


def machine_python():
    """Serializers, APIs, signals, webhooks, erp_assist, crm_import, email services, commands."""
    for path in _walk("*.py"):
        if is_machine_python(path.relative_to(ROOT).as_posix()):
            yield path


class StaticGuardTests(SimpleTestCase):
    def test_the_scan_sees_the_known_machine_outputs(self):
        templates = {p.relative_to(ROOT).as_posix() for p in machine_templates()}
        for expected in (
            "documents/templates/documents/sales_quote.html",
            "communications/templates/communications/emails/quote_email.html",
            "communications/templates/communications/emails/order_confirmation.txt",
            "golden_erp/templates/erp/pick_ticket_print.html",
        ):
            self.assertIn(expected, templates)
        python = {p.relative_to(ROOT).as_posix() for p in machine_python()}
        for expected in ("customers/serializers.py", "webhooks/outbox.py", "communications/services.py",
                         "crm_import/canonical/validate.py", "crm_import/canonical/io.py",
                         "golden_erp/addons/hubspot_export/convert.py", "golden_erp/addons/fixture_crm/adapter.py",
                         "core/integrations/webhook.py", "core/plugins/crm_adapter.py"):
            self.assertIn(expected, python)

    def test_every_crm_import_module_is_machine_facing(self):
        for rel in ("crm_import/appliers/deals.py", "crm_import/mapping.py", "crm_import/__init__.py"):
            self.assertTrue(is_machine_python(rel), rel)
        for rel in ("crm_import/tests/test_canonical.py", "crm_import/erp_views.py",
                    "crm_import/views/review.py"):
            self.assertFalse(is_machine_python(rel), rel)

    def test_machine_templates_never_use_labels(self):
        offenders = [
            p.relative_to(ROOT).as_posix()
            for p in machine_templates()
            if TEMPLATE_USES.search(p.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [], "labels are staff-UI only; override the template instead")

    def test_machine_python_never_reads_labels(self):
        offenders = [
            p.relative_to(ROOT).as_posix()
            for p in machine_python()
            if PYTHON_USES.search(p.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [], "labels are staff-UI only; use canonical names")

    def test_patterns_catch_the_documented_syntax(self):
        for source in (
            "{% load labels %}", "{% load static labels %}", '{% label "crm.stage" %}',
            '{{ "crm.stage"|label }}', "{{ labels.crm.stage }}",
        ):
            self.assertRegex(source, TEMPLATE_USES)
        for source in ('<span class="label">', "{{ field.label }}", ".addr-label {"):
            self.assertNotRegex(source, TEMPLATE_USES)


def every_key_overridden():
    return {key: {"singular": f"Zz{i}", "plural": f"Zz{i}s"} for i, key in enumerate(label_registry.DEFAULTS)}


class BehaviouralGuardTests(TestCase):
    def setUp(self):
        from customers.models import Customer
        from orders.models import Quote

        self.company = Company.objects.create(name="Leak Co")
        customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace", email="ada@example.com", company=self.company
        )
        Quote.objects.create(customer=customer, company=self.company)
        self.staff = make_staff_user("leak-staff")
        UserProfile.objects.create(user=self.staff, company=self.company)
        self.session_user = User.objects.create_user("leak-session", password="pw", is_staff=True)
        UserProfile.objects.create(user=self.session_user, company=self.company)

    def _overridden(self):
        overrides = every_key_overridden()
        return override_settings(
            GOLDEN_LABEL_OVERRIDES=overrides,
            GOLDEN_LABEL_TENANT_OVERRIDES={str(self.company.pk): overrides},
        )

    def _api(self):
        client = APIClient()
        client.force_authenticate(self.staff)
        response = client.get("/api/customers/customers/")
        self.assertEqual(response.status_code, 200)
        return response.content

    def _csv(self, name):
        self.client.force_login(self.session_user)
        response = self.client.get(reverse(name) + "?export=csv")
        self.assertEqual(response.status_code, 200)
        return response["Content-Disposition"], response.content

    def test_api_response_is_byte_identical(self):
        baseline = self._api()
        with self._overridden():
            self.assertEqual(self._api(), baseline)

    def test_csv_export_is_byte_identical(self):
        for name in ("erp-quote-list", "erp-customer-list"):
            with self.subTest(view=name):
                baseline = self._csv(name)
                with self._overridden():
                    self.assertEqual(self._csv(name), baseline)
                    # and the screen did change, so the override was live
                    page = self.client.get(reverse(name))
                    self.assertIn("Zz", page.context["page_title"])
