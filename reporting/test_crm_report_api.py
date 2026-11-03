"""The CRM reports as JSON (docs/crm/phase6-adapters.md 3.6, Phase 6 step 7).

Runs on the #70 fixture (``test_crm_reports.CrmReportFixture``: same tenants,
engagements, tasks and dates), so every number here is one the report pages
already pin. The core check is parity: for the same params, the API's typed
rows rendered the CSV's way equal the page's CSV byte for byte, and its
counts equal the page's table cells.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from crm.models import Engagement, EngagementStageChange, Pipeline

from .crm_reports import pct
from .test_crm_reports import CrmReportFixture, read_csv, table, total

DAY = timedelta(days=1)
API = "/api/v1/crm/reports/"
PAGES = {"funnel": "erp-report-crm-funnel", "stage-duration": "erp-report-crm-stages",
         "follow-ups": "erp-report-crm-follow-ups", "attribution": "erp-report-crm-attribution"}


def csv_money(parts) -> str:
    """The CSV's ``won_value`` cell from the API's money parts."""
    return "; ".join(f"{'≥' if p['lower_bound'] else ''}{p['amount']} {p['currency']}"
                     for p in parts)


def pct_csv(part, whole) -> str:
    text = pct(part, whole)
    return "" if text == "—" else text


def blank(value) -> str:
    return "" if value is None else str(value)


def funnel_csv_row(row, *, key="") -> list[str]:
    return [key, *(str(row[c]) for c in ("created", "reached_qualification", "won", "lost",
                                         "not_counted", "open")),
            pct_csv(row["won"], row["won"] + row["lost"]), csv_money(row["won_value"]),
            str(row["won_amount_known"])]


def stage_csv_row(row) -> list[str]:
    return [row["stage"], row["stage_name"], str(row["entered"]), blank(row["median_days"]),
            blank(row["p75_days"]), str(row["still_in_stage"]),
            blank(row["still_in_stage_median_days"]), str(row["advanced"]),
            pct_csv(row["advanced"], row["entered"]), str(row["lost_here"]),
            pct_csv(row["lost_here"], row["entered"])]


def follow_up_csv_row(row, *, total=False) -> list[str]:
    head = ["(total)", "(total)"] if total else [blank(row["assignee"]), blank(row["assignee_id"])]
    return head + [str(row[c]) for c in ("due", "completed_on_time", "completed_late",
                                         "open_overdue", "open_not_yet_due", "cancelled",
                                         "times_rescheduled")] + \
        [pct_csv(row["completed_on_time"], row["due"])]


class ApiFixture(CrmReportFixture):
    def period(self):
        today = timezone.localdate()
        return {"date_from": (today - 30 * DAY).isoformat(), "date_to": (today + 7 * DAY).isoformat()}

    def api(self, report, user=None, **params):
        client = APIClient()
        client.force_login(user or self.rep.user)
        path = API if report == "" else f"{API}{report}/"
        return client.get(path, {**self.period(), **params} if report else params)

    def ok(self, report, user=None, **params):
        response = self.api(report, user, **params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def page_csv(self, report, user=None, **params):
        return read_csv(self.get(PAGES[report], user=user, export="csv", **params))


class ParityTests(ApiFixture):
    """The API answers what the pages show, on the same data and params."""

    def test_funnel_equals_the_page_csv_and_table(self):
        for api_params, page_params in (({}, {}), ({"attribution": "first_touch"}, {"by": "first_touch"})):
            with self.subTest(**api_params):
                body = self.ok("funnel", **api_params)
                csv_rows = self.page_csv("funnel", **page_params)
                self.assertEqual(csv_rows[0], body["columns"][:-1])  # + amount_incomplete
                rendered = [funnel_csv_row(r, key=r["source"] or "") for r in body["rows"]]
                rendered.append(funnel_csv_row(body["totals"], key="(total)"))
                self.assertEqual(rendered, csv_rows[1:])
                page = self.get(PAGES["funnel"], **page_params)
                counts = [[r["created"], r["reached_qualification"], r["won"], r["lost"],
                           r["not_counted"], r["open"]] for r in body["rows"]]
                self.assertEqual(counts, [row[1:7] for row in table(page)])

    def test_funnel_numbers(self):
        body = self.ok("funnel")
        self.assertEqual(body["report"], "funnel")
        self.assertEqual(body["company"], str(self.company.pk))
        self.assertEqual(body["params"]["attribution"], "engagement")
        phone, referral, unknown = body["rows"]
        self.assertEqual((phone["source"], phone["created"], phone["won"], phone["win_rate"]),
                         ("phone", 3, 2, "66.7"))
        self.assertIsNone(unknown["source"])  # blank shows as null, never "Unknown"
        self.assertIsNone(unknown["win_rate"])
        self.assertEqual(body["totals"]["win_rate"], "75.0")

    def test_lower_bound_is_a_flag_and_counts_never_a_string(self):
        body = self.ok("funnel")
        phone, referral, unknown = body["rows"]
        self.assertTrue(phone["amount_incomplete"])
        self.assertEqual(phone["won_value"], [{"currency": "USD", "amount": "1000.00",
                                               "lower_bound": True, "won_amount_known": 1,
                                               "won": 2}])
        self.assertFalse(referral["amount_incomplete"])
        self.assertEqual(unknown["won_value"], [])
        self.assertEqual(body["coverage"], {"won": 3, "won_amount_known": 2,
                                            "amount_incomplete": True})
        # Currencies grouped, never converted or summed together.
        self.assertEqual([(p["currency"], p["amount"], p["lower_bound"])
                          for p in body["totals"]["won_value"]],
                         [("EUR", "500.00", False), ("USD", "1000.00", True)])
        self.assertEqual([n["code"] for n in body["notes"]], ["no_fx_conversion", "amount_lower_bound"])
        self.assertNotIn("≥", json.dumps(body["rows"]) + json.dumps(body["totals"]))
        self.assertNotIn("—", json.dumps(body))

    def test_stage_duration_equals_the_page_csv(self):
        body = self.ok("stage-duration")
        csv_rows = self.page_csv("stage-duration")
        self.assertEqual(csv_rows[0], body["columns"])
        self.assertEqual([stage_csv_row(r) for r in body["rows"]], csv_rows[1:])
        self.assertEqual(body["params"]["pipeline"], {"id": str(self.pipeline.pk), "key": "sales"})
        new = body["rows"][0]
        self.assertEqual((new["stage"], new["entered"], new["median_days"], new["advanced_pct"]),
                         ("new", 6, "2.0", "66.7"))
        self.assertIsNone(body["rows"][1]["median_days"])
        self.assertEqual(body["totals"], {"entered": 11})

    def test_no_history_footnote_is_structured(self):
        body = self.ok("stage-duration")
        self.assertEqual(body["coverage"], {"no_history_excluded": 1, "reconstructed_visits": 0})
        self.assertEqual([(n["code"], n["count"]) for n in body["notes"]], [("no_history_excluded", 1)])
        self.e["E4"].stage_changes.update(source=EngagementStageChange.Source.IMPORT)
        body = self.ok("stage-duration")
        page = self.get(PAGES["stage-duration"])
        self.assertEqual(body["coverage"]["no_history_excluded"],
                         page.context["no_history_excluded"])
        self.assertEqual(body["coverage"]["no_history_excluded"], 2)

    def test_reconstructed_visits_match_the_page(self):
        EngagementStageChange.objects.create(
            engagement=self.e["E4"], from_stage=self.stage("new"),
            to_stage=self.stage("prospecting"), from_pipeline=self.pipeline,
            to_pipeline=self.pipeline, from_status="open", to_status="open",
            changed_at=self.base + 7 * DAY, source=EngagementStageChange.Source.IMPORT,
        )
        body = self.ok("stage-duration")
        page = self.get(PAGES["stage-duration"])
        self.assertEqual(body["coverage"]["reconstructed_visits"], page.context["reconstructed_visits"])
        self.assertGreater(body["coverage"]["reconstructed_visits"], 0)
        self.assertIn("reconstructed_visits", [n["code"] for n in body["notes"]])

    def test_pipeline_by_key_or_id(self):
        by_key = self.ok("stage-duration", pipeline="sales")
        by_id = self.ok("stage-duration", pipeline=str(self.pipeline.pk))
        self.assertEqual(by_key["rows"], by_id["rows"])
        upper = self.ok("stage-duration", pipeline=str(self.pipeline.pk).upper())
        self.assertEqual(upper["rows"], by_id["rows"])
        response = self.api("stage-duration", pipeline="nope")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["allowed"], ["sales"])

    def test_follow_ups_equal_the_page_csv(self):
        body = self.ok("follow-ups")
        csv_rows = self.page_csv("follow-ups")
        self.assertEqual(csv_rows[0], body["columns"])
        rendered = [follow_up_csv_row(r) for r in body["rows"]]
        rendered.append(follow_up_csv_row(body["totals"], total=True))
        self.assertEqual(rendered, csv_rows[1:])
        self.assertEqual(body["rows"][-1]["assignee"], None)  # unassigned: null, never a word
        t = body["totals"]
        self.assertEqual((t["due"], t["on_time_rate"], t["open_engagements"],
                          t["without_next_action"], t["overdue_next_actions"]),
                         (7, "42.9", 3, 1, 1))
        self.assertEqual(total(self.get(PAGES["follow-ups"]))[1], t["due"])

    def test_attribution_equals_the_page_csv(self):
        from crm import touchpoints

        won = Engagement.objects.filter(company=self.company, status="won").order_by("pk")
        for eng in won:
            touch, _ = touchpoints.record(self.company, party=eng.party, engagement=eng, source="api",
                                          occurred_at=eng.created_at, channel="referral")
            Engagement.objects.filter(pk=eng.pk, first_touch__isnull=True).update(first_touch=touch)
        body = self.ok("attribution")
        csv_rows = self.page_csv("attribution")
        self.assertEqual(csv_rows[0], body["columns"][:6])
        rendered = [[blank(r["channel"]), str(r["first_touch"]), str(r["last_touch"]), r["linear"],
                     csv_money(r["first_touch_won_value"]), str(r["won_amount_known"])]
                    for r in body["rows"]]
        t = body["totals"]
        rendered.append(["(total)", str(t["first_touch"]), str(t["last_touch"]), t["linear"],
                         csv_money(t["first_touch_won_value"]), str(t["won_amount_known"])])
        self.assertEqual(rendered, csv_rows[1:])
        self.assertEqual(body["coverage"]["won"], t["first_touch"] + body["coverage"]["no_touch"])
        self.assertIn("won", body["summary"])

    def test_period_is_the_site_day_as_on_the_page(self):
        late = timezone.make_aware(timezone.datetime(2026, 3, 10, 23, 30))
        Engagement.objects.filter(pk=self.e["E4"].pk).update(created_at=late)
        client = APIClient()
        client.force_login(self.rep.user)
        self.client.force_login(self.rep.user)
        with mock.patch("django.utils.timezone.now", return_value=late):
            body = client.get(f"{API}funnel/", {"period": "today"}).json()
            page = self.client.get(reverse(PAGES["funnel"]), {"period": "today"})
        self.assertEqual((body["params"]["date_from"], body["params"]["period"]), ("2026-03-10", "today"))
        self.assertEqual(body["timezone"], "America/New_York")
        self.assertEqual(body["totals"]["created"], 1)
        self.assertEqual(total(page)[1], 1)

    def test_default_period_is_the_month(self):
        response = APIClient()
        response.force_login(self.rep.user)
        body = response.get(f"{API}funnel/").json()
        today = timezone.localdate()
        self.assertEqual((body["params"]["period"], body["params"]["date_from"], body["params"]["date_to"]),
                         ("month", today.replace(day=1).isoformat(), today.isoformat()))


class ParameterTests(ApiFixture):
    def test_unknown_parameter_is_400_with_the_allowed_list(self):
        response = self.api("funnel", by="first_touch")  # the page's name, not the API's
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual((body["code"], body["param"]), ("unknown_parameter", "by"))
        self.assertIn("attribution", body["allowed"])

    def test_unknown_values_are_400_with_the_allowed_list(self):
        response = self.api("funnel", attribution="last_touch")
        self.assertEqual((response.status_code, response.json()["allowed"]),
                         (400, ["engagement", "first_touch"]))
        client = APIClient()
        client.force_login(self.rep.user)
        response = client.get(f"{API}funnel/", {"period": "decade"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("quarter", response.json()["allowed"])

    def test_dates(self):
        client = APIClient()
        client.force_login(self.rep.user)
        for params in ({"date_from": "2026-01-01"}, {"date_from": "2026-13-01", "date_to": "2026-12-01"},
                       {"date_from": "2026-03-02", "date_to": "2026-03-01"},
                       {"date_from": "2025-01-01", "date_to": "2026-01-02"},
                       {"date_from": "20260101", "date_to": "2026-01-02"},
                       {"date_from": "2026-W01-1", "date_to": "2026-01-09"}):
            with self.subTest(**params):
                self.assertEqual(client.get(f"{API}funnel/", params).status_code, 400)
        ok = client.get(f"{API}funnel/", {"date_from": "2025-01-01", "date_to": "2026-01-01"})
        self.assertEqual(ok.status_code, 200)  # 366 days

    def test_catalog(self):
        body = self.ok("")
        self.assertEqual([r["report"] for r in body["reports"]], ["funnel", "stage-duration", "follow-ups", "attribution"])
        funnel = body["reports"][0]
        self.assertEqual(funnel["params"], {"attribution": ["engagement", "first_touch"]})
        self.assertEqual(funnel["definitions"][0]["term"], "Cohort")
        self.assertEqual(body["common_params"]["max_days"], 366)


class TenancyAndPermissionTests(ApiFixture):
    def test_tenant_user_sees_only_its_company(self):
        body = self.ok("funnel", user=self.rival_user.user)
        self.assertEqual(body["company"], str(self.other.pk))
        self.assertEqual([(r["source"], r["created"]) for r in body["rows"]], [("phone", 1)])
        body = self.ok("follow-ups", user=self.rival_user.user)
        self.assertEqual(body["totals"]["due"], 1)

    def test_naming_another_tenant_is_404(self):
        for report in ("funnel", "stage-duration", "follow-ups", "attribution"):
            with self.subTest(report=report):
                response = self.api(report, user=self.rival_user.user, company=str(self.company.pk))
                self.assertEqual(response.status_code, 404)
                # Its own company, in any uuid spelling, is fine.
                own = str(self.other.pk).upper()
                self.assertEqual(self.api(report, user=self.rival_user.user, company=own).status_code, 200)
        self.assertEqual(self.api("funnel", company="not-a-uuid").status_code, 400)

    def test_another_tenants_pipeline_is_unknown(self):
        rival_pipeline = Pipeline.objects.get(company=self.other, key="sales")
        response = self.api("stage-duration", pipeline=str(rival_pipeline.pk))
        self.assertEqual(response.status_code, 400)

    def test_cross_tenant_principal_must_name_the_company(self):
        root = User.objects.create_superuser("root", "root@example.com", "pw")
        response = self.api("funnel", user=root)
        self.assertEqual((response.status_code, response.json()["code"]), (400, "company_required"))
        mine = self.ok("funnel", user=root, company=str(self.company.pk))
        self.assertEqual(mine["rows"], self.ok("funnel")["rows"])
        theirs = self.ok("funnel", user=root, company=str(self.other.pk))
        self.assertEqual(theirs["totals"]["created"], 1)
        missing = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(self.api("funnel", user=root, company=missing).status_code, 404)

    def test_single_company_superuser_needs_no_company(self):
        root = User.objects.create_superuser("root", "root@example.com", "pw")
        from core.models import Company

        only_a = Company.objects.filter(pk=self.company.pk)
        with mock.patch("reporting.api.Company.objects.all", return_value=only_a):
            body = self.ok("follow-ups", user=root)
        self.assertEqual(body["company"], str(self.company.pk))

    def test_superuser_naming_a_tenant_equals_that_tenants_user(self):
        root = User.objects.create_superuser("root", "root@example.com", "pw")
        for report in ("funnel", "stage-duration", "follow-ups", "attribution"):
            with self.subTest(report=report):
                mine = self.ok(report)
                named = self.ok(report, user=root, company=str(self.company.pk))
                self.assertEqual({k: v for k, v in named.items()},
                                 {k: v for k, v in mine.items()})

    def test_login_without_company_sees_nothing(self):
        from core.tests.helpers import make_staff_user

        orphan = make_staff_user(username="orphan", password="pw")
        for report in ("funnel", "stage-duration", "follow-ups", "attribution"):
            body = self.ok(report, user=orphan)
            self.assertEqual((body["company"], body["rows"]), (None, []))
            self.assertEqual(body["notes"][0]["code"], "no_company")
            self.assertTrue(body["summary"].startswith("This account has no company"))

    def test_same_gate_as_the_pages(self):
        """A non-staff session is refused by both; anonymous too."""
        shopper = User.objects.create_user("shopper", "shopper@example.com", "pw")
        for report in ("", "funnel", "stage-duration", "follow-ups"):
            with self.subTest(report=report or "catalog"):
                self.assertEqual(self.api(report, user=shopper).status_code, 403)
                path = API if not report else f"{API}{report}/"
                self.assertIn(APIClient().get(path).status_code, (401, 403))
        for name in PAGES.values():
            self.client.force_login(shopper)
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)
            self.client.logout()
            self.assertEqual(self.client.get(reverse(name)).status_code, 302)  # to the login page


SVC_ROLES = [
    {"name": "crm-report-reader", "label": "CRM report reader", "kind": "service",
     "permissions": ["crm.view_engagement"]},
]


@override_settings(GOLDEN_ROLES=SVC_ROLES)
class ServiceAccountTests(ApiFixture):
    def test_contract_and_pinned_tenant(self):
        from core.service_accounts import ensure_service_account, issue_api_key

        svc = ensure_service_account("crm-report-reader", ["crm-report-reader"], company=self.company)
        _key, raw = issue_api_key(svc, name="crm-report-reader")
        client = APIClient(HTTP_AUTHORIZATION=f"Api-Key {raw}")
        body = client.get(f"{API}funnel/", self.period()).json()
        self.assertEqual((body["company"], body["totals"]["created"]), (str(self.company.pk), 7))
        self.assertEqual(client.get(f"{API}stage-duration/", self.period()).status_code, 200)
        self.assertEqual(client.get(API).status_code, 200)
        # Follow-ups need crm.view_task, which this contract lacks.
        self.assertEqual(client.get(f"{API}follow-ups/", self.period()).status_code, 403)
        # Pinned to tenant A: tenant B is not found.
        response = client.get(f"{API}funnel/", {**self.period(), "company": str(self.other.pk)})
        self.assertEqual(response.status_code, 404)

    def client_for(self, name, roles, company=None):
        from core.service_accounts import ensure_service_account, issue_api_key

        svc = ensure_service_account(name, roles, company=company)
        _key, raw = issue_api_key(svc, name=name)
        return APIClient(HTTP_AUTHORIZATION=f"Api-Key {raw}")

    def test_unpinned_account_reads_nothing(self):
        client = self.client_for("crm-report-loose", ["crm-report-reader"])
        body = client.get(f"{API}funnel/", self.period()).json()
        self.assertEqual((body["company"], body["rows"], body["notes"][0]["code"]),
                         (None, [], "no_company"))
        response = client.get(f"{API}funnel/", {**self.period(), "company": str(self.company.pk)})
        self.assertEqual(response.status_code, 404)

    @override_settings(GOLDEN_ROLES=SVC_ROLES + [
        {"name": "task-reader", "label": "Task reader", "kind": "service",
         "permissions": ["crm.view_task"]},
        {"name": "crm-reader", "label": "CRM reader", "kind": "service",
         "permissions": ["crm.view_task", "crm.view_engagement"]},
    ])
    def test_follow_ups_need_task_and_engagement_permissions(self):
        tasks_only = self.client_for("task-reader", ["task-reader"], company=self.company)
        self.assertEqual(tasks_only.get(f"{API}follow-ups/", self.period()).status_code, 403)
        both = self.client_for("crm-reader", ["crm-reader"], company=self.company)
        body = both.get(f"{API}follow-ups/", self.period()).json()
        self.assertEqual(body["totals"]["due"], 7)


class SummaryTests(ApiFixture):
    def test_funnel_summary(self):
        p = self.period()
        self.assertEqual(self.ok("funnel")["summary"], (
            f"7 engagements created from {p['date_from']} to {p['date_to']}: 3 won, 1 lost, "
            "1 closed without counting as a loss, 2 open; 5 reached qualification. Win rate 75.0%. "
            "Won value EUR 500.00; $1,000.00 (lower bound: 1 of 2 won engagements has no amount); "
            "no FX conversion. Largest engagement source: phone (3 engagements)."
        ))

    def test_stage_summary_states_the_exclusions(self):
        p = self.period()
        self.assertEqual(self.ok("stage-duration")["summary"], (
            f"Pipeline sales, from {p['date_from']} to {p['date_to']}: 11 stage entries across "
            "8 stages. Longest median time in stage: negotiation (3.0 days). 2 visits still in stage. "
            "1 engagement excluded: no observed stage history, so time in stage is unknown."
        ))

    def test_follow_up_summary(self):
        p = self.period()
        self.assertEqual(self.ok("follow-ups")["summary"], (
            f"7 tasks due from {p['date_from']} to {p['date_to']}: 3 completed on time, 1 late, "
            "1 open and overdue, 1 not yet due, 1 canceled; on-time rate 42.9%; 2 reschedules. "
            "Now: 3 open engagements, 1 without a next action, 1 with an overdue next action."
        ))

    def test_summaries_are_deterministic(self):
        for report in ("funnel", "stage-duration", "follow-ups", "attribution"):
            self.assertEqual(self.ok(report)["summary"], self.ok(report)["summary"])

    def test_no_personal_data_in_any_summary(self):
        """Reports are aggregates: no staff or party name, email or phone."""
        from parties.models import PartyEmail, PartyPhone

        self.rep.display_name = "Rosalind Franklin"
        self.rep.save()
        self.rep.user.email = "rosalind@example.com"
        self.rep.user.save()
        PartyEmail.objects.create(party=self.org, email="buyer@example.com", purpose="work", is_primary=True)
        PartyPhone.objects.create(party=self.org, number_raw="+12025550123", number_e164="+12025550123",
                                  purpose="work", line_type="mobile", is_primary=True)
        forbidden = ["Rosalind", "Franklin", "rosalind@example.com", "buyer@example.com",
                     "+12025550123", self.org.display_name, "rep2", "Tenant A"]
        for report in ("funnel", "stage-duration", "follow-ups", "attribution"):
            body = self.ok(report)
            for word in forbidden:
                self.assertNotIn(word, body["summary"], report)
            self.assertNotRegex(body["summary"], r"[\w.+-]+@[\w-]+\.\w+")
            undated = re.sub(r"\d{4}-\d{2}-\d{2}", "", body["summary"])
            self.assertNotRegex(undated, r"\+?\d[\d ()-]{8,}\d")
            self.assertIsNone(re.search(r"\brep\b", body["summary"]))

    def test_free_text_source_is_never_echoed(self):
        Engagement.objects.filter(company=self.company).update(source="leak@example.com")
        summary = self.ok("funnel")["summary"]
        self.assertNotIn("leak@example.com", summary)
        self.assertIn("Largest engagement source: an unregistered source (7 engagements)", summary)


class LabelLeakTests(ApiFixture):
    """With every label overridden, the JSON is byte-identical (E11)."""

    def test_labels_never_reach_the_json(self):
        from core import label_registry

        before = {r: self.api(r).content for r in ("", "funnel", "stage-duration", "follow-ups")}
        overrides = {key: {"singular": f"Zz{i}"} for i, key in enumerate(label_registry.GOLDEN_LABELS)}
        tenant = {str(self.company.pk): {key: {"singular": f"Yy{i}"}
                                         for i, key in enumerate(label_registry.GOLDEN_LABELS)}}
        with override_settings(GOLDEN_LABEL_OVERRIDES=overrides, GOLDEN_LABEL_TENANT_OVERRIDES=tenant):
            self.assertContains(self.get(PAGES["funnel"]), "Yy")  # the page does reword
            for report, content in before.items():
                with self.subTest(report=report or "catalog"):
                    self.assertEqual(self.api(report).content, content)

    def test_the_static_scan_covers_the_api_module(self):
        from django.conf import settings

        from core.tests.test_labels_never_leak import machine_python

        scanned = {p.relative_to(settings.BASE_DIR).as_posix() for p in machine_python()}
        self.assertIn("reporting/api.py", scanned)
        self.assertIn("erp_assist/core/crm.py", scanned)
        self.assertIn("reporting/crm_reports.py", scanned)


class QueryCountTests(ApiFixture):
    def test_constant_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def queries(report):
            self.api(report)
            with CaptureQueriesContext(connection) as ctx:
                self.api(report)
            return len(ctx.captured_queries)

        reports = ("funnel", "stage-duration", "follow-ups", "attribution")
        before = {r: queries(r) for r in reports}
        for i in range(4):
            eng = self.thread(f"X{i}", [(0, "create", "new"), (1, "move", "qualification"),
                                        (2, "win", None)], source="email", value=Decimal("10"))
            self.task(f"X{i} call", self.base + DAY, assignee=self.rep2, engagement=eng)
        for r in reports:
            self.assertEqual(queries(r), before[r], r)

