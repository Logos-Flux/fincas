"""The assistant's CRM report tools (docs/crm/phase6-adapters.md 3.6, Phase 6 step 7).

Drives ``erp_assist.core.crm``'s report functions against the in-process DRF
stack with the shared service key (``test_party_ambiguity``'s harness). The
key reads every tenant, so ``company`` is required and every answer must be
that tenant's numbers only.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from core.models import Company
from crm import engagements as engagement_service
from crm import tasks as task_service
from crm.models import Pipeline
from crm.tests.helpers import organisation
from erp_assist import mcp_adapter
from erp_assist.core import ERPValidationError
from erp_assist.core import crm as crm_core

from .test_party_ambiguity import API_KEY, asgi_client

TOOLS = ("list_crm_reports", "crm_funnel_report", "crm_stage_report", "crm_follow_up_report")


def run(coro):
    return asyncio.run(coro)


@override_settings(API_KEY=API_KEY)
class CrmReportToolTests(TransactionTestCase):
    def setUp(self):
        self.root = User.objects.create_superuser(username="api-service-reports", password=None)
        self.a = Company.objects.create(name="Tenant A")
        self.b = Company.objects.create(name="Tenant B")
        org_a, org_b = organisation(self.a), organisation(self.b, "Rival Org")
        won = engagement_service.create(company=self.a, party=org_a, engagement_source="phone",
                                        expected_value=Decimal("1200"), currency="USD")
        engagement_service.win(won)
        engagement_service.create(company=self.a, party=org_a, engagement_source="referral")
        rival = engagement_service.create(company=self.b, party=org_b, engagement_source="phone",
                                          expected_value=Decimal("99999"), currency="USD")
        engagement_service.win(rival)
        task_service.create(company=self.b, title="Rival call", due=rival.created_at)
        self.client = asgi_client()

    def tearDown(self):
        run(self.client._http.aclose())

    def direct(self, report, **params):
        """The same report straight from the API, for comparison."""
        api = APIClient()
        api.force_login(self.root)
        response = api.get(f"/api/v1/crm/reports/{report}/", params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_funnel_is_the_named_tenants_and_equals_the_api(self):
        answer = run(crm_core.crm_funnel_report(self.client, company=str(self.a.pk)))
        self.assertEqual((answer["report"], answer["company"]), ("funnel", str(self.a.pk)))
        self.assertEqual(answer["data"]["totals"]["created"], 2)
        self.assertEqual([r["source"] for r in answer["data"]["rows"]], ["phone", "referral"])
        self.assertEqual(answer["data"]["totals"]["won_value"][0]["amount"], "1200.00")
        direct = self.direct("funnel", company=str(self.a.pk))
        self.assertEqual(answer["data"]["rows"], direct["rows"])
        self.assertEqual(answer["summary"], direct["summary"])
        self.assertNotIn("99,999", answer["summary"])
        other = run(crm_core.crm_funnel_report(self.client, company=str(self.b.pk)))
        self.assertEqual(other["data"]["totals"]["created"], 1)

    def test_company_is_required_for_the_shared_key(self):
        for fn in (crm_core.crm_funnel_report, crm_core.crm_stage_report,
                   crm_core.crm_follow_up_report):
            with self.subTest(fn=fn.__name__):
                with self.assertRaises(ERPValidationError):
                    run(fn(self.client, company=None))
                with self.assertRaises(ERPValidationError):
                    run(fn(self.client, company="tenant-a"))
        # And the server agrees: no company, several tenants, 400.
        api = APIClient()
        api.force_login(self.root)
        self.assertEqual(api.get("/api/v1/crm/reports/funnel/").status_code, 400)

    def test_stage_report_pipeline_stays_in_the_tenant(self):
        answer = run(crm_core.crm_stage_report(self.client, company=str(self.a.pk), pipeline="sales"))
        self.assertEqual(answer["params"]["pipeline"]["id"],
                         str(Pipeline.objects.get(company=self.a, key="sales").pk))
        rival = Pipeline.objects.get(company=self.b, key="sales")
        with self.assertRaises(ERPValidationError):
            run(crm_core.crm_stage_report(self.client, company=str(self.a.pk), pipeline=str(rival.pk)))

    def test_follow_ups_and_params_pass_through(self):
        answer = run(crm_core.crm_follow_up_report(self.client, company=str(self.a.pk), period="year"))
        self.assertEqual((answer["params"]["period"], answer["data"]["totals"]["due"]), ("year", 0))
        rival = run(crm_core.crm_follow_up_report(self.client, company=str(self.b.pk), period="year"))
        self.assertEqual(rival["data"]["totals"]["due"], 1)
        with self.assertRaises(ERPValidationError) as ctx:
            run(crm_core.crm_funnel_report(self.client, company=str(self.a.pk), attribution="last"))
        self.assertEqual(ctx.exception.details["allowed"], ["engagement", "first_touch"])

    def test_generic_report_and_catalog(self):
        answer = run(crm_core.crm_report(self.client, "stage_duration", company=str(self.a.pk)))
        self.assertEqual(answer["report"], "stage-duration")
        with self.assertRaises(ERPValidationError):
            run(crm_core.crm_report(self.client, "pipeline-value", company=str(self.a.pk)))
        catalog = run(crm_core.list_crm_reports(self.client))
        self.assertEqual([r["report"] for r in catalog["reports"]], list(crm_core.REPORTS))

    def test_summary_is_deterministic_and_carries_no_names(self):
        first = run(crm_core.crm_funnel_report(self.client, company=str(self.a.pk)))["summary"]
        second = run(crm_core.crm_funnel_report(self.client, company=str(self.a.pk)))["summary"]
        self.assertEqual(first, second)
        self.assertIn("2 engagements created", first)
        for word in ("Acme Widgets", "Tenant A", "@"):
            self.assertNotIn(word, first)


class McpReportToolTests(SimpleTestCase):
    def test_tools_registered_with_company_required(self):
        tools = {t.name: t for t in run(mcp_adapter.mcp.list_tools())}
        for name in TOOLS:
            self.assertIn(name, tools)
        for name in TOOLS[1:]:
            schema = tools[name].inputSchema
            self.assertIn("company", schema.get("required", []), name)
            self.assertIn("Required", tools[name].description)

    def test_docstrings_use_canonical_nouns_and_state_lower_bounds(self):
        tools = {t.name: t for t in run(mcp_adapter.mcp.list_tools())}
        self.assertIn("engagement", tools["crm_funnel_report"].description.lower())
        self.assertIn("LOWER BOUND", tools["crm_funnel_report"].description)
        self.assertIn("no_history_excluded", tools["crm_stage_report"].description)
        self.assertIn("task", tools["crm_follow_up_report"].description.lower())

    def test_errors_render_as_dicts(self):
        async def refuse(client, **kwargs):
            raise ERPValidationError("company is required")

        with mock.patch.object(mcp_adapter, "_get_client", new=mock.AsyncMock(return_value=object())), \
                mock.patch.object(crm_core, "crm_funnel_report", new=refuse):
            result = run(mcp_adapter.crm_funnel_report(company=""))
        self.assertEqual(result["error"], "ERPValidationError")
