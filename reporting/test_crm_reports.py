"""CRM Phase 3 reports (docs/crm/phase3-engagement.md 4.2, step 9).

One fixture, two tenants, every number pinned. Tenant A's engagements
(created in the period unless said otherwise; days are offsets from
``BASE`` = 20 days ago):

====  ==========  =====================================  ======  ==========
name  source      history (stage at day)                 status  value
====  ==========  =====================================  ======  ==========
E1    phone       new 0, qualification 2, proposal 5     won 6   1000 USD
E2    phone       new 0, negotiation 1                   won 4   (none) USD
E3    phone       new 0, qualification 1                 lost 2  price
E4    referral    new 0                                  open    —
E5    referral    new 0                                  lost 3  disqualified
E6    (blank)     proposal 0, backfill rows only         open    —
E7    referral    new 0                                  won 4   500 EUR
E8    phone       new 40 days before BASE, qualif. 1     open    (created 60 days ago)
====  ==========  =====================================  ======  ==========

Tenant B has one won 99,999 USD engagement and its own tasks; tenant A's
user must never see them.
"""

from __future__ import annotations

import csv
import io
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Company
from crm import engagements, tasks
from crm.models import Engagement, EngagementStageChange, Pipeline, Task, Touchpoint
from crm.tests.helpers import organisation, profile

DAY = timedelta(days=1)


def table(response):
    """Visible rows as display strings (csv-only cells dropped)."""
    return [
        [c["display"] for c in row if not c["csv_only"]]
        for row in response.context["table_rows"]
    ]


def total(response):
    return [c["display"] for c in response.context["total_row"] if not c["csv_only"]]


def card_values(response):
    from django.template import Context, Template

    rendered = {}
    tpl = Template('{% include "erp/_report_heading.html" with h=h %}')
    for card in response.context["summary_cards"]:
        name = tpl.render(Context({"h": card["heading"], "request": response.wsgi_request}))
        rendered[name] = card["value"]
    return rendered


def read_csv(response):
    return list(csv.reader(io.StringIO(response.content.decode())))


class CrmReportFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now()
        cls.base = cls.now - 20 * DAY
        cls.company = Company.objects.create(name="Tenant A")
        cls.other = Company.objects.create(name="Tenant B")
        cls.pipeline = Pipeline.objects.get(company=cls.company, key="sales")
        cls.org = organisation(cls.company)
        cls.rep = profile(cls.company, "rep")
        cls.rep2 = profile(cls.company, "rep2")
        cls.rival_user = profile(cls.other, "rival")
        for staff in (cls.rep, cls.rival_user):  # /erp/ is staff-only
            staff.user.is_staff = True
            staff.user.save()
        cls.e = {}
        cls._engagements()
        cls._rival()
        cls._tasks()

    # ── fixture builders ────────────────────────────────────

    @classmethod
    def stage(cls, key, pipeline=None):
        return (pipeline or cls.pipeline).stages.get(key=key)

    @classmethod
    def thread(cls, name, steps, *, source="", value=None, currency="USD", company=None,
               pipeline=None):
        """Create an engagement and replay ``steps`` through the services,
        then date each history row ``BASE + day``.

        ``steps``: ``[(day, action, arg)]``; the first is ``("create", stage
        key)``, then ``("move", key)``, ``("win", None)`` or ``("lose", reason)``.
        """
        company = company or cls.company
        pipeline = pipeline or Pipeline.objects.get(company=company, key="sales")
        party = cls.org if company == cls.company else organisation(company, "Rival Org")
        (day, _, key), *rest = steps
        eng = engagements.create(
            company=company, party=party, stage=cls.stage(key, pipeline),
            engagement_source=source, expected_value=value, currency=currency,
        )
        rows = [(day, eng.stage_changes.get())]
        for day, action, arg in rest:
            if action == "move":
                change = engagements.move(eng, cls.stage(arg, pipeline)).change
            elif action == "win":
                change = engagements.win(eng)
            else:
                change = engagements.lose(eng, arg)
            rows.append((day, change))
        for day, change in rows:
            EngagementStageChange.objects.filter(pk=change.pk).update(
                changed_at=cls.base + day * DAY
            )
        cls.e[name] = eng
        return eng

    @classmethod
    def _engagements(cls):
        cls.thread("E1", [(0, "create", "new"), (2, "move", "qualification"),
                          (5, "move", "proposal_sent"), (6, "win", None)],
                   source="phone", value=Decimal("1000"))
        cls.thread("E2", [(0, "create", "new"), (1, "move", "negotiation"), (4, "win", None)],
                   source="phone")
        cls.thread("E3", [(0, "create", "new"), (1, "move", "qualification"),
                          (2, "lose", "price")], source="phone", value=Decimal("300"))
        cls.thread("E4", [(0, "create", "new")], source="referral")
        cls.thread("E5", [(0, "create", "new"), (3, "lose", "disqualified")], source="referral")
        e6 = cls.thread("E6", [(0, "create", "proposal_sent")])
        e6.stage_changes.update(source=EngagementStageChange.Source.BACKFILL)
        cls.thread("E7", [(0, "create", "new"), (4, "win", None)], source="referral",
                   value=Decimal("500"), currency="EUR")
        e8 = cls.thread("E8", [(-40, "create", "new"), (1, "move", "qualification")],
                        source="phone")
        Engagement.objects.filter(pk=e8.pk).update(created_at=cls.now - 60 * DAY)
        # E1's first touch was a paid-search click (the attribution toggle).
        touch = Touchpoint.objects.create(
            company=cls.company, occurred_at=cls.base, channel="paid_search", source="lead_form",
        )
        Engagement.objects.filter(pk=cls.e["E1"].pk).update(first_touch=touch)

    @classmethod
    def _rival(cls):
        cls.thread("R1", [(0, "create", "new"), (2, "move", "qualification"), (3, "win", None)],
                   source="phone", value=Decimal("99999"), company=cls.other)
        rival_task = tasks.create(company=cls.other, title="Rival call",
                                  assignee=cls.rival_user, due=cls.base + DAY)
        tasks.complete(rival_task)
        Task.objects.filter(pk=rival_task.pk).update(completed_at=cls.base)

    @classmethod
    def task(cls, title, due, *, assignee=None, engagement=None, completed=None,
             cancel=False):
        task = tasks.create(company=cls.company, title=title, assignee=assignee,
                            engagement=engagement, due=due, due_tz="UTC")
        if completed is not None:
            tasks.complete(task)
            Task.objects.filter(pk=task.pk).update(completed_at=completed)
        if cancel:
            tasks.cancel(task)
        return task

    @classmethod
    def _tasks(cls):
        b = cls.base
        cls.task("T1 on time", b + DAY, assignee=cls.rep, completed=b + DAY / 2)
        cls.task("T2 late", b + 2 * DAY, assignee=cls.rep, completed=b + 3 * DAY)
        cls.task("T3 overdue", b + 3 * DAY, assignee=cls.rep, engagement=cls.e["E4"])
        cls.task("T4 not yet due", cls.now + DAY, assignee=cls.rep, engagement=cls.e["E8"])
        cls.task("T5 cancelled", b + 4 * DAY, assignee=cls.rep2, cancel=True)
        cls.task("T6 on the dot", b + 5 * DAY, completed=b + 5 * DAY)
        t7 = cls.task("T7 rescheduled twice", b + DAY, assignee=cls.rep)
        tasks.reschedule(t7, b + 2 * DAY)
        tasks.reschedule(t7, b + 6 * DAY)
        tasks.complete(t7)
        Task.objects.filter(pk=t7.pk).update(completed_at=b + 5 * DAY)
        cls.task("T8 due long ago", cls.now - 60 * DAY, assignee=cls.rep,
                 completed=cls.now - 59 * DAY)
        cls.task("T9 undated", None, assignee=cls.rep)

    # ── requests ────────────────────────────────────────────

    def get(self, url_name, user=None, **params):
        user = user or self.rep.user
        self.client.force_login(user)
        today = timezone.localdate()
        params = {
            "date_from": (today - 30 * DAY).isoformat(),
            "date_to": (today + 7 * DAY).isoformat(),
            **params,
        }
        return self.client.get(reverse(url_name), params)


class FunnelBySourceTests(CrmReportFixture):
    URL = "erp-report-crm-funnel"

    def test_rows_and_total(self):
        response = self.get(self.URL)
        self.assertEqual(response.status_code, 200)
        # source, created, reached, won, lost, disq/dup, open, win rate, won value, amount known
        self.assertEqual(table(response), [
            ["Phone", 3, 3, 2, 1, 0, 0, "67%", "≥ $1,000.00", "1 of 2"],
            ["Referral", 3, 1, 1, 0, 1, 1, "100%", "€500.00", "1 of 1"],
            ["Unknown", 1, 1, 0, 0, 0, 1, "—", "—", "—"],
        ])
        self.assertEqual(total(response), [
            "All sources", 7, 5, 3, 1, 1, 2, "75%", "€500.00; ≥ $1,000.00", "2 of 3",
        ])

    def test_lower_bound_and_completeness_are_rendered(self):
        response = self.get(self.URL)
        self.assertContains(response, "≥ $1,000.00")
        self.assertContains(
            response, "Amounts known for 67% of won engagements (2 of 3).", html=False
        )
        self.assertContains(response, "no FX conversion")
        self.assertContains(response, "How these numbers are calculated")

    def test_cards(self):
        cards = card_values(self.get(self.URL))
        self.assertEqual(cards, {
            "Engagements created": 7, "Won": 3, "Win rate": "75%",
            "Won value": "€500.00; ≥ $1,000.00",
        })

    def test_complete_amounts_render_without_marker(self):
        response = self.get(self.URL, user=self.rival_user.user)
        self.assertEqual(table(response), [
            ["Phone", 1, 1, 1, 0, 0, 0, "100%", "$99,999.00", "1 of 1"],
        ])
        self.assertNotContains(response, "≥ $")  # (the definitions still explain ≥)
        self.assertContains(response, "Amounts known for 100% of won engagements (1 of 1).")

    def test_first_touch_attribution(self):
        response = self.get(self.URL, by="first_touch")
        self.assertEqual([row[:4] for row in table(response)], [
            ["Unknown", 6, 4, 2],
            ["Paid search", 1, 1, 1],
        ])

    def test_created_outside_the_period_is_not_in_the_cohort(self):
        today = timezone.localdate()
        response = self.get(self.URL, date_from=(today - 90 * DAY).isoformat(),
                            date_to=(today - 50 * DAY).isoformat())
        self.assertEqual(table(response), [
            ["Phone", 1, 1, 0, 0, 0, 1, "—", "—", "—"],  # E8: qualification from history
        ])
        self.assertNotContains(response, "Amounts known")

    def test_reached_comes_from_history_not_only_the_current_stage(self):
        # Qualified, then moved back to Prospecting: still "reached".
        self.thread("E9", [(0, "create", "new"), (1, "move", "qualification"),
                           (2, "move", "prospecting")], source="phone")
        self.assertEqual(table(self.get(self.URL))[0][:3], ["Phone", 4, 4])

    def test_default_period_is_the_site_day(self):
        from unittest import mock

        # 23:30 in New York is 03:30 UTC the next day: "today" is the local day.
        late = timezone.make_aware(timezone.datetime(2026, 3, 10, 23, 30))
        Engagement.objects.filter(pk=self.e["E4"].pk).update(created_at=late)
        self.client.force_login(self.rep.user)
        with mock.patch("django.utils.timezone.now", return_value=late):
            response = self.client.get(reverse(self.URL), {"period": "today"})
        self.assertEqual(response.context["date_from"].isoformat(), "2026-03-10")
        self.assertEqual(total(response)[:2], ["All sources", 1])

    def test_csv_is_canonical(self):
        rows = read_csv(self.get(self.URL, export="csv"))
        self.assertEqual(rows[0], [
            "source", "created", "reached_qualification", "won", "lost", "not_counted",
            "open", "win_rate", "won_value", "won_amount_known",
        ])
        self.assertEqual(rows[1:], [
            ["phone", "3", "3", "2", "1", "0", "0", "67%", "≥1000.00 USD", "1"],
            ["referral", "3", "1", "1", "0", "1", "1", "100%", "500.00 EUR", "1"],
            ["", "1", "1", "0", "0", "0", "1", "", "", "0"],
            ["(total)", "7", "5", "3", "1", "1", "2", "75%", "500.00 EUR; ≥1000.00 USD", "2"],
        ])


class StageDurationTests(CrmReportFixture):
    URL = "erp-report-crm-stages"

    def test_stage_rows(self):
        response = self.get(self.URL)
        self.assertEqual(response.status_code, 200)
        # stage, entered, median, p75, still in, still-in median so far,
        # advanced, advanced %, lost here, lost here %
        self.assertEqual(table(response), [
            ["New", 6, "2.0", "3.0", 1, "20.0", 4, "67%", 1, "17%"],
            ["Prospecting", 0, "—", "—", 0, "—", 0, "—", 0, "—"],
            ["Qualification", 3, "2.0", "2.5", 1, "19.0", 1, "33%", 1, "33%"],
            ["Needs analysis", 0, "—", "—", 0, "—", 0, "—", 0, "—"],
            ["Proposal sent", 1, "1.0", "1.0", 0, "—", 1, "100%", 0, "0%"],
            ["Negotiation", 1, "3.0", "3.0", 0, "—", 1, "100%", 0, "0%"],
            ["Verbal commit", 0, "—", "—", 0, "—", 0, "—", 0, "—"],
            ["Closed/Signed", 0, "—", "—", 0, "—", 0, "—", 0, "—"],
        ])

    def test_no_history_footnote(self):
        response = self.get(self.URL)
        self.assertEqual(response.context["no_history_excluded"], 1)  # E6
        self.assertContains(response, 'data-testid="no-history-footnote"')
        self.assertContains(
            response, "1 engagement was left out: no stage history except backfill or import"
        )

    def test_no_footnote_when_every_engagement_has_history(self):
        response = self.get(self.URL, user=self.rival_user.user)
        self.assertEqual(response.context["no_history_excluded"], 0)
        self.assertNotContains(response, 'data-testid="no-history-footnote"')
        rows = {row[0]: row for row in table(response)}
        self.assertEqual(rows["New"][:4], ["New", 1, "2.0", "2.0"])
        self.assertEqual(rows["Qualification"][:4], ["Qualification", 1, "1.0", "1.0"])

    def test_import_only_history_is_excluded_too(self):
        self.e["E4"].stage_changes.update(source=EngagementStageChange.Source.IMPORT)
        response = self.get(self.URL)
        self.assertEqual(response.context["no_history_excluded"], 2)
        self.assertContains(response, "2 engagements were left out")
        new = table(response)[0]
        self.assertEqual(new[:2] + new[4:5], ["New", 5, 0])  # E4 no longer "still in"

    def test_reconstructed_rows_never_time_a_visit(self):
        # Backfilled at New, then worked for real: the backfill row starts no
        # visit; the observed move to Qualification does.
        eng = self.thread("E9", [(0, "create", "new"), (2, "move", "qualification"),
                                 (3, "move", "needs_analysis")], source="phone")
        first = eng.stage_changes.order_by("changed_at").first()
        EngagementStageChange.objects.filter(pk=first.pk).update(
            source=EngagementStageChange.Source.BACKFILL
        )
        # An import row ends E4's New visit: counted as entered, not timed.
        EngagementStageChange.objects.create(
            engagement=self.e["E4"], from_stage=self.stage("new"),
            to_stage=self.stage("prospecting"), from_pipeline=self.pipeline,
            to_pipeline=self.pipeline, from_status="open", to_status="open",
            changed_at=self.base + 7 * DAY, source=EngagementStageChange.Source.IMPORT,
        )
        response = self.get(self.URL)
        rows = {row[0]: row for row in table(response)}
        self.assertEqual(rows["New"][:6], ["New", 6, "2.0", "3.0", 0, "—"])  # unchanged durations
        self.assertEqual(rows["Qualification"][:3], ["Qualification", 4, "1.0"])
        self.assertEqual(rows["Needs analysis"][:2], ["Needs analysis", 1])
        # E9's backfilled New, E4's New (ended by the import row) and the
        # import row's own Prospecting entry.
        self.assertEqual(response.context["reconstructed_visits"], 3)
        self.assertEqual(response.context["no_history_excluded"], 1)
        self.assertContains(response, "3 visits in the period began or ended at a backfill")

    def test_reentry_counts_the_engagement_once_and_times_each_visit(self):
        self.thread("E9", [(0, "create", "new"), (1, "move", "qualification"),
                           (2, "move", "new"), (5, "move", "qualification")], source="phone")
        rows = {row[0]: row for row in table(self.get(self.URL))}
        # New: 6 + E9 once; visits [1,1,2,3,4] + E9's 1 and 3.
        self.assertEqual(rows["New"][:4], ["New", 7, "2.0", "3.0"])

    def test_another_tenants_pipeline_is_not_selectable(self):
        rival_pipeline = Pipeline.objects.get(company=self.other, key="sales")
        response = self.get(self.URL, pipeline=str(rival_pipeline.pk))
        self.assertEqual(response.context["selected_pipeline"], str(self.pipeline.pk))
        self.assertEqual(table(response)[0][:2], ["New", 6])

    def test_csv_is_canonical(self):
        rows = read_csv(self.get(self.URL, export="csv"))
        self.assertEqual(rows[0], [
            "stage", "stage_name", "entered", "median_days", "p75_days", "still_in_stage",
            "still_in_stage_median_days", "advanced", "advanced_pct", "lost_here",
            "lost_here_pct",
        ])
        self.assertEqual(rows[1], ["new", "New", "6", "2.0", "3.0", "1", "20.0", "4", "67%",
                                   "1", "17%"])
        self.assertEqual(rows[2][:3], ["prospecting", "Prospecting", "0"])
        self.assertEqual(len(rows), 9)


class FollowUpCompletionTests(CrmReportFixture):
    URL = "erp-report-crm-follow-ups"

    def test_rows_and_total(self):
        response = self.get(self.URL)
        self.assertEqual(response.status_code, 200)
        # assignee, due, on time, late, overdue, not yet due, cancelled,
        # rescheduled, on-time rate
        self.assertEqual(table(response), [
            ["rep", 5, 2, 1, 1, 1, 0, 2, "40%"],
            ["rep2", 1, 0, 0, 0, 0, 1, 0, "0%"],
            ["Unassigned", 1, 1, 0, 0, 0, 0, 0, "100%"],
        ])
        self.assertEqual(total(response), ["All assignees", 7, 3, 1, 1, 1, 1, 2, "43%"])

    def test_cards(self):
        cards = card_values(self.get(self.URL))
        self.assertEqual(cards, {
            "Tasks due": 7,
            "Completed on time": "43%",
            # E4, E6, E8 are open; only E6 has no open task.
            "Open engagements without a next action": "1 (33%)",
            "Overdue next actions": 1,  # E4's T3
        })

    def test_rival_sees_only_its_own_tasks(self):
        response = self.get(self.URL, user=self.rival_user.user)
        self.assertEqual(table(response), [["rival", 1, 1, 0, 0, 0, 0, 0, "100%"]])

    def test_csv_is_canonical_and_formula_safe(self):
        self.rep.display_name = "=HYPERLINK(1)"
        self.rep.save()
        rows = read_csv(self.get(self.URL, export="csv"))
        self.assertEqual(rows[0], [
            "assignee", "assignee_id", "due", "completed_on_time", "completed_late",
            "open_overdue", "open_not_yet_due", "cancelled", "times_rescheduled",
            "on_time_rate",
        ])
        self.assertEqual(rows[1][0], "'=HYPERLINK(1)")
        self.assertEqual(rows[1][1], str(self.rep.pk))
        self.assertEqual(rows[3][:2], ["", ""])  # unassigned: blank, never the display word
        self.assertEqual(rows[-1][:3], ["(total)", "(total)", "7"])


class CrmReportScopeAndLabelTests(CrmReportFixture):
    URLS = ("erp-report-crm-funnel", "erp-report-crm-stages", "erp-report-crm-follow-ups")

    def test_superuser_sees_every_company(self):
        root = User.objects.create_superuser("root", "root@example.com", "pw")
        response = self.get("erp-report-crm-funnel", user=root)
        self.assertEqual(total(response)[1], 8)  # 7 + the rival's
        self.assertContains(response, "All companies (superuser view)")
        response = self.get("erp-report-crm-follow-ups", user=root)
        self.assertEqual(total(response)[1], 8)

    def test_login_without_company_sees_nothing(self):
        from core.tests.helpers import make_staff_user

        orphan = make_staff_user(username="orphan", password="pw")
        for url in self.URLS:
            response = self.get(url, user=orphan)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["table_rows"], [])

    def test_sidebar_links_every_report(self):
        response = self.get("erp-report-crm-funnel")
        for url in self.URLS:
            self.assertContains(response, f'href="{reverse(url)}"')

    def test_labels_reword_headings_but_never_the_csv(self):
        before = {url: self.get(url, export="csv").content for url in self.URLS}
        overrides = {
            "crm.engagement": {"singular": "Idea"},
            "crm.stage": {"singular": "Phase"},
            "crm.task": {"singular": "To-do"},
        }
        with override_settings(GOLDEN_LABEL_OVERRIDES=overrides):
            page = self.get("erp-report-crm-funnel")
            self.assertContains(page, "Idea funnel by source")
            self.assertEqual(page.context["page_title"], "Idea funnel by source")
            self.assertContains(page, "Amounts known for 67% of won ideas")
            self.assertContains(self.get("erp-report-crm-stages"), "Phase duration and conversion")
            self.assertContains(self.get("erp-report-crm-follow-ups"), "To-dos due")
            for url in self.URLS:
                self.assertEqual(self.get(url, export="csv").content, before[url], url)


class CrmReportQueryCountTests(CrmReportFixture):
    """Queries do not grow with the data (no per-row lookups)."""

    URLS = ("erp-report-crm-funnel", "erp-report-crm-stages", "erp-report-crm-follow-ups")

    def queries(self, url):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.get(url)  # warm the session and caches
        with CaptureQueriesContext(connection) as ctx:
            self.get(url)
        return len(ctx.captured_queries)

    def test_constant_queries(self):
        before = {url: self.queries(url) for url in self.URLS}
        for i in range(4):
            eng = self.thread(f"X{i}", [(0, "create", "new"), (1, "move", "qualification"),
                                        (2, "win", None)], source="email", value=Decimal("10"))
            self.task(f"X{i} call", self.base + DAY, assignee=self.rep2, engagement=eng)
        for url in self.URLS:
            self.assertEqual(self.queries(url), before[url], url)


class BackfilledHistoryExclusionTests(CrmReportFixture):
    """Plan 7.4: history a backfill or an import wrote is excluded from the
    stage report and disclosed, as the report's definitions claim.

    One engagement (E9) with an observed creation row, then a ``backfill``
    row and an ``import`` row written directly, as ``crm.backfill`` and
    ``crm_import`` do. Neither row starts a visit, the observed visit they
    end is counted but never timed, and every one of them is in the
    footnote. The funnel reads a row's *stage* only (never its time) by its
    "Reached qualification" definition, so it is not under test here.
    """

    URL = "erp-report-crm-stages"

    def setUp(self):
        super().setUp()
        self.e9 = self.thread("E9", [(0, "create", "new")], source="phone")
        self.reconstructed = [
            EngagementStageChange.objects.create(
                engagement=self.e9, from_stage=self.stage("new"),
                to_stage=self.stage("qualification"), from_pipeline=self.pipeline,
                to_pipeline=self.pipeline, from_status="open", to_status="open",
                changed_at=self.base + 1 * DAY, source=EngagementStageChange.Source.BACKFILL,
            ),
            EngagementStageChange.objects.create(
                engagement=self.e9, from_stage=self.stage("qualification"),
                to_stage=self.stage("proposal_sent"), from_pipeline=self.pipeline,
                to_pipeline=self.pipeline, from_status="open", to_status="open",
                changed_at=self.base + 2 * DAY, source=EngagementStageChange.Source.IMPORT,
            ),
        ]

    def baseline(self):
        """The fixture's stage table and footnote without E9."""
        EngagementStageChange.objects.filter(engagement=self.e9).delete()
        self.e9.delete()
        data = self.report()
        return data, {row.stage.key: row for row in data["rows"]}

    def report(self):
        from reporting.crm_reports import stage_duration

        today = timezone.localdate()
        return stage_duration(
            self.pipeline, Engagement.objects.filter(company=self.company),
            today - timedelta(days=60), today, now=self.now,
        )

    def test_reconstructed_rows_never_enter_a_stage_or_time_a_visit(self):
        data = self.report()
        rows = {row.stage.key: row for row in data["rows"]}
        # E9 entered New (observed). Its New visit ended at the backfill row:
        # counted, not timed. The backfill and import rows entered nothing.
        self.assertIn(self.e9.pk, rows["new"].entered)
        self.assertNotIn(self.e9.pk, rows["qualification"].entered)
        self.assertNotIn(self.e9.pk, rows["proposal_sent"].entered)
        self.assertNotIn(self.e9.pk, rows["new"].still_in)
        self.assertEqual(data["reconstructed_visits"], 3)
        new_durations = sorted(rows["new"].durations)
        base_data, base_rows = self.baseline()
        self.assertEqual(sorted(base_rows["new"].durations), new_durations)
        self.assertEqual(base_data["reconstructed_visits"], 0)
        # E9 advanced to Qualification only in reconstructed rows: not counted.
        self.assertNotIn(self.e9.pk, rows["new"].advanced)

    def test_each_source_is_excluded_on_its_own(self):
        for source in (EngagementStageChange.Source.BACKFILL, EngagementStageChange.Source.IMPORT):
            with self.subTest(source=source):
                EngagementStageChange.objects.filter(engagement=self.e9).exclude(
                    source=EngagementStageChange.Source.ERP
                ).delete()
                EngagementStageChange.objects.create(
                    engagement=self.e9, from_stage=self.stage("new"),
                    to_stage=self.stage("qualification"), from_pipeline=self.pipeline,
                    to_pipeline=self.pipeline, from_status="open", to_status="open",
                    changed_at=self.base + 1 * DAY, source=source,
                )
                data = self.report()
                rows = {r.stage.key: r for r in data["rows"]}
                # E9's New visit ends at the row (counted, not timed) and the
                # row's own Qualification entry is skipped: two, one each.
                self.assertEqual(data["reconstructed_visits"], 2)
                self.assertNotIn(self.e9.pk, rows["qualification"].entered)
                self.assertIn(self.e9.pk, rows["new"].entered)
                self.assertNotIn(self.e9.pk, rows["new"].still_in)

    def test_an_engagement_with_only_reconstructed_history_is_left_out_and_counted(self):
        EngagementStageChange.objects.filter(engagement=self.e9).update(
            source=EngagementStageChange.Source.IMPORT
        )
        data = self.report()
        self.assertFalse(any(self.e9.pk in row.entered for row in data["rows"]))
        self.assertEqual(data["excluded"], 2)  # E6 (backfill only) and E9
        self.assertEqual(data["reconstructed_visits"], 0)

    def test_the_page_and_the_table_disclose_the_counts(self):
        from reporting.crm_reports import stage_table

        table = stage_table(self.report())
        self.assertEqual(table["coverage"], {"no_history_excluded": 1, "reconstructed_visits": 3})
        self.assertEqual(
            [(n["code"], n["count"]) for n in table["notes"]],
            [("no_history_excluded", 1), ("reconstructed_visits", 3)],
        )
        response = self.get(self.URL)
        self.assertEqual(response.context["reconstructed_visits"], 3)
        self.assertEqual(response.context["no_history_excluded"], 1)
        self.assertContains(response, "3 visits in the period began or ended at a backfill")
        self.assertContains(response, "1 engagement was left out")
