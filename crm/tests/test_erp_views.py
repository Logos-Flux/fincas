"""CRM Phase 3 step 8: the server-rendered back office (docs/crm/phase3-engagement.md 6).

Every page is driven by a staff user (``make_staff_user`` plus a tenant
profile). Two tenants throughout: another tenant's ids are 404 on every
route, GET and POST. Writes are asserted through their service side effects
(history rows, task events, next action), never through ``save()``.
"""

from __future__ import annotations

import datetime
import zoneinfo
from io import StringIO

from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from communications import ingest
from communications.models import Communication
from core import nav_registry, tz
from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from crm import engagements, tasks
from crm.erp_views import local_today
from crm.models import (
    Engagement,
    EngagementContact,
    EngagementStageChange,
    Lead,
    Pipeline,
    Stage,
    Task,
    TaskEvent,
)
from parties.models import PartyEmail

from .helpers import organisation, person

# Staff without a zone of their own see settings.TIME_ZONE.
VIEWER_TZ = zoneinfo.ZoneInfo("America/New_York")


def staff(company, username, *perms, tz=""):
    user = make_staff_user(username, password="pw")
    for perm in perms:
        app_label, codename = perm.split(".")
        user.user_permissions.add(Permission.objects.get(content_type__app_label=app_label, codename=codename))
    prof, _ = UserProfile.objects.update_or_create(
        user=user, defaults={"company": company, "display_name": username.title(), "timezone": tz})
    return user, prof


class CrmViewTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other_company = Company.objects.create(name="Tenant B")
        cls.pipeline = Pipeline.objects.get(company=cls.company, key="sales")
        cls.user, cls.me = staff(cls.company, "sam")
        cls.colleague_user, cls.colleague = staff(cls.company, "kim")
        cls.other_user, cls.other_me = staff(cls.other_company, "rival")
        cls.org = organisation(cls.company, "Acme Widgets")
        cls.ada = person(cls.company)

    def setUp(self):
        self.client.force_login(self.user)

    def stage(self, key, pipeline=None):
        return (pipeline or self.pipeline).stages.get(key=key)

    def eng(self, **kwargs):
        kwargs.setdefault("company", self.company)
        kwargs.setdefault("party", self.org)
        kwargs.setdefault("owner", self.me)
        return engagements.create(**kwargs)

    def other_eng(self):
        return engagements.create(company=self.other_company, owner=self.other_me,
                                  party=organisation(self.other_company, "Rival Co"))

    def task(self, eng, due=None, **kwargs):
        kwargs.setdefault("assignee", self.me)
        kwargs.setdefault("title", "Call back")
        return tasks.create(engagement=eng, due=due, **kwargs)

    def today(self):
        # "Today" in the viewer's zone, exactly as the views compute it. A UTC
        # date is tomorrow for a New York viewer between 00:00 UTC and local
        # midnight, which made "due today" tests red every US evening.
        return local_today(tz.resolve(self.me, self.company))


# --- nav and labels ------------------------------------------------------------------


class CrmNavTests(CrmViewTestBase):
    def test_entries_under_crm_with_label_keys(self):
        items = nav_registry.NAV
        labels = [i.label for i in items]
        crm = labels.index("CRM")
        next_header = next(i for i in items[crm + 1:] if i.type == "header")
        expected = {"erp-crm-my-day": "", "erp-crm-engagement-list": "crm.engagement",
                    "erp-crm-task-list": "crm.task", "erp-crm-lead-list": "crm.lead"}
        for url_name, key in expected.items():
            entry = next(i for i in items if i.url_name == url_name)
            self.assertEqual(entry.label_key, key)
            self.assertTrue(crm < items.index(entry) < items.index(next_header))
        my_day = next(i for i in items if i.url_name == "erp-crm-my-day")
        self.assertEqual(items[crm + 1], my_day)  # the landing page for sales users
        pipelines = next(i for i in items if i.url_name == "erp-crm-pipeline-list")
        self.assertEqual(pipelines.label_key, "crm.pipeline")

    @override_settings(GOLDEN_LABEL_OVERRIDES={"crm.engagement": {"singular": "Idea", "plural": "Ideas",
                                                                  "article": "an"}})
    def test_label_override_reaches_pages_not_csv(self):
        eng = self.eng()
        response = self.client.get(reverse("erp-crm-engagement-list"))
        self.assertContains(response, "Ideas")
        self.assertContains(self.client.get(reverse("erp-crm-engagement-detail", args=[eng.pk])), "Idea")
        csv = self.client.get(reverse("erp-crm-engagement-list") + "?export=csv")
        self.assertNotIn(b"Idea", b"".join(csv.streaming_content))
        self.assertIn("engagements.csv", csv["Content-Disposition"])

    def test_non_staff_is_refused(self):
        from django.contrib.auth import get_user_model

        plain = get_user_model().objects.create_user("plain", password="pw")
        self.client.force_login(plain)
        for name in ("erp-crm-my-day", "erp-crm-engagement-list", "erp-crm-task-list", "erp-crm-lead-list",
                     "erp-crm-pipeline-list"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)


# --- 6.1 the engagement list -----------------------------------------------------


class EngagementListTests(CrmViewTestBase):
    url = reverse("erp-crm-engagement-list")

    def numbers(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return [e.number for e in response.context["object_list"]]

    def test_default_is_open_and_mine(self):
        mine = self.eng()
        theirs = self.eng(owner=self.colleague)
        won = self.eng()
        engagements.win(won, by=self.me)
        response = self.client.get(self.url)
        self.assertEqual(response.context["active_chip"], "mine")
        self.assertEqual([e.number for e in response.context["object_list"]], [mine.number])
        self.assertNotIn(theirs.number, self.numbers())

    def test_default_falls_back_to_unassigned_with_notice(self):
        nobody = self.eng(owner=None)
        self.eng(owner=self.colleague)
        response = self.client.get(self.url)
        self.assertEqual(response.context["active_chip"], "unassigned")
        self.assertEqual([e.number for e in response.context["object_list"]], [nobody.number])
        self.assertContains(response, "You own no open engagements")

    def test_attention_order(self):
        """No next action first (oldest first), then due soonest, overdue on top."""
        now = timezone.now()
        later = self.eng(title="later")
        self.task(later, due=now + datetime.timedelta(days=3))
        overdue = self.eng(title="overdue")
        self.task(overdue, due=now - datetime.timedelta(days=2))
        idle_old = self.eng(title="idle old")
        idle_new = self.eng(title="idle new")
        Engagement.objects.filter(pk=idle_old.pk).update(created_at=now - datetime.timedelta(days=9))
        self.assertEqual(self.numbers(chip="mine"),
                         [idle_old.number, idle_new.number, overdue.number, later.number])

    def test_attention_queue_says_which_rule_fired(self):
        now = timezone.now()
        no_task = self.eng()
        late = self.eng()
        self.task(late, due=now - datetime.timedelta(hours=2))
        ok = self.eng()
        self.task(ok, due=now + datetime.timedelta(days=1))
        orphan = self.eng(party=None)
        self.task(orphan, due=now + datetime.timedelta(days=1))
        ownerless = self.eng(owner=None)
        self.task(ownerless, due=now + datetime.timedelta(days=1))
        response = self.client.get(self.url, {"queue": "attention"})
        rows = {e.number: e.rules for e in response.context["object_list"]}
        self.assertEqual(rows[no_task.number], ["No next action"])
        self.assertEqual(rows[late.number], ["Next action overdue"])
        self.assertEqual(rows[orphan.number], ["No counterparty"])
        self.assertEqual(rows[ownerless.number], ["No owner"])
        self.assertNotIn(ok.number, rows)
        self.assertContains(response, "Next action overdue")
        self.assertContains(response, 'name="ids"')  # bulk actions

    @override_settings(GOLDEN_CRM={"overdue_grace_hours": 24})
    def test_attention_overdue_grace(self):
        late = self.eng()
        self.task(late, due=timezone.now() - datetime.timedelta(hours=2))
        self.assertNotIn(late.number, self.numbers(queue="attention"))

    def test_chips(self):
        now = timezone.now()
        overdue = self.eng()
        self.task(overdue, due=now - datetime.timedelta(days=1))
        stale = self.eng()
        Engagement.objects.filter(pk=stale.pk).update(last_activity_at=now - datetime.timedelta(days=40))
        orphan = self.eng(party=None)
        lost = self.eng()
        engagements.lose(lost, "price", by=self.me)
        self.assertEqual(self.numbers(chip="overdue"), [overdue.number])
        self.assertEqual(self.numbers(chip="stale"), [stale.number])
        self.assertEqual(self.numbers(chip="no_party"), [orphan.number])
        self.assertEqual(self.numbers(chip="closed_month"), [lost.number])
        self.assertNotIn(lost.number, self.numbers(chip="open"))
        response = self.client.get(self.url, {"chip": "open"})
        self.assertContains(response, "Stale · 40 d")

    def test_stale_does_not_reorder(self):
        now = timezone.now()
        stale = self.eng()
        self.task(stale, due=now + datetime.timedelta(days=5))
        Engagement.objects.filter(pk=stale.pk).update(last_activity_at=now - datetime.timedelta(days=60))
        fresh = self.eng()
        self.task(fresh, due=now + datetime.timedelta(days=1))
        self.assertEqual(self.numbers(chip="open"), [fresh.number, stale.number])

    def test_owner_without_login_renders(self):
        """Seeded and imported profiles may have no login (seed_crm's reps)."""
        rep = UserProfile.objects.create(company=self.company, display_name="")
        eng = self.eng(owner=rep)
        self.task(eng, assignee=rep, due=self.today())
        for url in (self.url + "?chip=open", reverse("erp-crm-engagement-detail", args=[eng.pk]),
                    reverse("erp-crm-task-list") + "?assignee=all"):
            self.assertContains(self.client.get(url), "Unnamed profile")

    def test_columns_and_value_never_zero(self):
        eng = self.eng(expected_value=None)
        response = self.client.get(self.url)
        self.assertContains(response, eng.number)
        self.assertContains(response, "Acme Widgets")
        self.assertContains(response, "New")  # stage
        self.assertContains(response, ">None<")  # no next action badge
        self.assertNotContains(response, "$0.00")

    def test_search_and_filters(self):
        a = self.eng(title="Solar retrofit")
        b = self.eng(party=self.ada)
        self.assertEqual(self.numbers(q="retrofit", chip="open"), [a.number])
        self.assertEqual(self.numbers(q="Ada", chip="open"), [b.number])
        self.assertEqual(self.numbers(q=a.number.lower(), chip="open"), [a.number])
        stage = self.stage("negotiation")
        engagements.move(b, stage, by=self.me)
        self.assertEqual(self.numbers(stage=str(stage.pk), chip="open"), [b.number])
        self.assertEqual(self.numbers(owner="none", chip="open"), [])
        self.assertEqual(self.numbers(stage="not-a-uuid", chip="open"), [])

    def test_sort_allowlist(self):
        small = self.eng(expected_value=10)
        big = self.eng(expected_value=900)
        self.assertEqual(self.numbers(sort="value", dir="desc"), [big.number, small.number])
        # An unlisted field (or a related path) falls back to the attention order, no error.
        self.assertEqual(self.numbers(sort="owner__user__password"), [small.number, big.number])
        response = self.client.get(self.url, {"sort": "value", "dir": "asc"})
        self.assertContains(response, 'aria-sort="ascending"')

    def test_csv_export_is_canonical(self):
        eng = self.eng(expected_value=1500)
        response = self.client.get(self.url, {"export": "csv"})
        self.assertEqual(response["Content-Type"], "text/csv")
        header, row = b"".join(response.streaming_content).decode().splitlines()[:2]
        # The visible columns (the 5.1 defaults), canonical ids; "All fields" has the rest.
        self.assertTrue(header.startswith("number,party,stage,next_action,expected_value,owner,last_activity_at"))
        self.assertIn(eng.number, row)
        self.assertIn("1500.00", row)

    def test_other_tenant_rows_never_show(self):
        theirs = self.other_eng()
        self.assertNotIn(theirs.number, self.numbers(chip="open"))
        self.assertNotIn(theirs.number, self.numbers(chip="unassigned"))

    def _populate(self, n):
        now = timezone.now()
        for i in range(n):
            eng = self.eng(party=self.ada if i % 2 else self.org, expected_value=100 + i)
            if i % 3:
                self.task(eng, due=now + datetime.timedelta(days=i))
            if i % 4 == 0:
                EngagementContact.objects.create(engagement=eng, party=person(self.company, f"P{i}", "Q"))

    def test_query_count_is_constant(self):
        self._populate(3)
        self.client.get(self.url)
        with CaptureQueriesContext(connection) as small:
            self.client.get(self.url, {"chip": "open"})
        self._populate(25)
        with CaptureQueriesContext(connection) as large:
            response = self.client.get(self.url, {"chip": "open"})
        self.assertEqual(len(response.context["object_list"]), 28)
        self.assertEqual(len(small), len(large))
        with CaptureQueriesContext(connection) as attention_small:
            self.client.get(self.url, {"queue": "attention"})
        self._populate(10)
        with CaptureQueriesContext(connection) as attention_large:
            self.client.get(self.url, {"queue": "attention"})
        self.assertEqual(len(attention_small), len(attention_large))

    def test_query_budget(self):
        self._populate(10)
        self.client.get(self.url, {"chip": "open"})
        # 8 shared by every /erp/ page + count, rows, pipelines, their stages,
        # owner choices, saved filters (phase4-fields.md 5.4).
        with self.assertNumQueries(14):
            self.client.get(self.url, {"chip": "open"})


class EngagementBulkTests(CrmViewTestBase):
    url = reverse("erp-crm-engagement-bulk")

    def test_assign_follow_up_and_lose(self):
        a, b = self.eng(owner=None), self.eng(owner=None)
        self.client.post(self.url, {"ids": [a.pk, b.pk], "action": "assign", "owner": self.colleague.pk})
        a.refresh_from_db()
        self.assertEqual(a.owner, self.colleague)
        self.client.post(self.url, {"ids": [a.pk], "action": "follow_up"})
        a.refresh_from_db()
        self.assertEqual(a.next_task.title, "Follow up")
        self.assertEqual(a.next_task.assignee, self.colleague)
        self.client.post(self.url, {"ids": [b.pk], "action": "lose", "loss_reason": "timing"})
        b.refresh_from_db()
        self.assertEqual((b.status, b.loss_reason), ("lost", "timing"))
        self.assertTrue(b.stage_changes.filter(to_status="lost", changed_by=self.me).exists())

    def test_reassigning_someone_elses_needs_permission(self):
        theirs = self.eng(owner=self.colleague)
        self.client.post(self.url, {"ids": [theirs.pk], "action": "assign", "owner": self.me.pk})
        theirs.refresh_from_db()
        self.assertEqual(theirs.owner, self.colleague)

    def test_other_tenant_ids_are_ignored(self):
        theirs = self.other_eng()
        self.client.post(self.url, {"ids": [theirs.pk], "action": "lose", "loss_reason": "price"})
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, "open")


# --- 6.2 the thread view ------------------------------------------------------------


class ThreadViewTests(CrmViewTestBase):
    def url(self, eng, name="erp-crm-engagement-detail"):
        return reverse(name, args=[eng.pk])

    def test_ranking_next_action_then_deal_then_timeline(self):
        eng = self.eng()
        html = self.client.get(self.url(eng)).content.decode()
        positions = [html.index(f'id="{anchor}"') for anchor in ("next-action", "deal-facts", "timeline")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('id="contacts"', html)
        self.assertIn('id="documents"', html)

    def test_no_next_action_is_the_call_to_action(self):
        eng = self.eng()
        response = self.client.get(self.url(eng))
        self.assertContains(response, "No next action")
        self.assertContains(response, "mb-4 crm-needs-action\"")
        self.assertContains(response, reverse("erp-crm-engagement-task-create", args=[eng.pk]))

    def test_next_action_shows_set_in_zone_when_different(self):
        eng = self.eng()
        self.task(eng, due=self.today() + datetime.timedelta(days=2), due_tz="Europe/Berlin")
        response = self.client.get(self.url(eng))
        self.assertContains(response, "set in Europe/Berlin")
        self.assertContains(response, "Complete")
        self.assertNotContains(response, "mb-4 crm-needs-action\"")

    def test_deal_facts(self):
        eng = self.eng(expected_value=1000)
        response = self.client.get(self.url(eng))
        self.assertContains(response, "5%")  # stage `new` default probability
        self.assertContains(response, "(from stage)")
        self.assertContains(response, "$50.00")  # weighted
        self.assertContains(response, 'aria-current="step"')
        stale_closed = self.eng(expected_close_date=self.today() - datetime.timedelta(days=3))
        self.assertContains(self.client.get(self.url(stale_closed)), "(past)")

    def test_contacts_documents_history(self):
        eng = self.eng()
        bob = person(self.company, "Bob", "Builder")
        PartyEmail.objects.create(party=bob, email="bob@example.com", purpose="work", is_primary=True)
        EngagementContact.objects.create(engagement=eng, party=bob, role="champion", is_primary=True)
        response = self.client.get(self.url(eng))
        self.assertContains(response, "Bob Builder")
        self.assertContains(response, "Champion")
        self.assertContains(response, "bob@example.com")
        self.assertContains(response, "Email marketing: Unknown")
        self.assertContains(response, "1 change")

    def test_other_tenant_is_404_everywhere(self):
        theirs = self.other_eng()
        task = tasks.create(engagement=theirs, title="Theirs")
        for name in ("erp-crm-engagement-detail", "erp-crm-engagement-timeline", "erp-crm-engagement-edit",
                     "erp-crm-engagement-log"):
            self.assertEqual(self.client.get(self.url(theirs, name)).status_code, 404, name)
        for name in ("erp-crm-engagement-move", "erp-crm-engagement-win", "erp-crm-engagement-lose",
                     "erp-crm-engagement-reopen", "erp-crm-engagement-close-remaining",
                     "erp-crm-engagement-task-create", "erp-crm-engagement-contact-create",
                     "erp-crm-engagement-log", "erp-crm-engagement-edit"):
            self.assertEqual(self.client.post(self.url(theirs, name), {}).status_code, 404, name)
        for name in ("erp-crm-task-complete", "erp-crm-task-reopen", "erp-crm-task-cancel",
                     "erp-crm-task-reschedule", "erp-crm-task-reassign"):
            self.assertEqual(self.client.post(reverse(name, args=[task.pk]), {}).status_code, 404, name)
        theirs.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual((theirs.status, task.status), ("open", "open"))

    def _grow(self, eng, n):
        now = timezone.now()
        for i in range(n):
            self.task(eng, due=now + datetime.timedelta(days=i + 1), title=f"T{i}")
            done = self.task(eng, title=f"D{i}")
            tasks.complete(done, by=self.me)
            ingest.record(self.company, "call", direction="outbound", occurred_at=now - datetime.timedelta(hours=i),
                          engagement=eng, party=self.org, subject=f"Call {i}",
                          participants=[{"role": "callee", "party": self.org}])
            EngagementContact.objects.create(engagement=eng, party=person(self.company, f"C{i}", "X"),
                                             role="other")
        engagements.move(eng, self.stage("qualification"), by=self.me)

    def test_query_count_is_constant(self):
        eng = self.eng()
        self._grow(eng, 1)
        url = self.url(eng)
        self.client.get(url)
        with CaptureQueriesContext(connection) as small:
            self.client.get(url)
        self._grow(eng, 12)
        with CaptureQueriesContext(connection) as large:
            response = self.client.get(url)
        self.assertContains(response, "Call 11")
        self.assertEqual(len(small), len(large))


class ThreadActionTests(CrmViewTestBase):
    def post(self, name, eng, data=None, **kwargs):
        return self.client.post(reverse(name, args=[eng.pk]), data or {}, **kwargs)

    def test_move_writes_history_through_the_service(self):
        eng = self.eng()
        response = self.post("erp-crm-engagement-move", eng, {"stage": self.stage("negotiation").pk})
        self.assertRedirects(response, reverse("erp-crm-engagement-detail", args=[eng.pk]) + "#deal-facts",
                             fetch_redirect_response=False)
        eng.refresh_from_db()
        self.assertEqual(eng.stage.key, "negotiation")
        change = eng.stage_changes.order_by("-changed_at").first()
        self.assertEqual((change.source, change.changed_by), ("erp", self.me))

    def test_move_to_an_outcome_stage_asks_never_closes(self):
        eng = self.eng()
        closing = Stage.objects.create(pipeline=self.pipeline, key="closed_won", name="Closed won",
                                       sort_order=900, outcome_hint="won")
        response = self.post("erp-crm-engagement-move", eng, {"stage": closing.pk})
        self.assertIn("confirm=won", response["Location"])
        eng.refresh_from_db()
        self.assertEqual((eng.status, eng.stage_id), ("open", closing.pk))

    def test_move_to_another_pipeline(self):
        other = Pipeline.objects.create(company=self.company, key="service", name="Service")
        target = Stage.objects.create(pipeline=other, key="triage", name="Triage", sort_order=10)
        eng = self.eng()
        self.post("erp-crm-engagement-move", eng, {"stage": target.pk})
        eng.refresh_from_db()
        self.assertEqual((eng.pipeline_id, eng.stage_id), (other.pk, target.pk))

    def test_another_tenants_stage_is_refused(self):
        eng = self.eng()
        theirs = Pipeline.objects.get(company=self.other_company, key="sales").stages.first()
        self.post("erp-crm-engagement-move", eng, {"stage": theirs.pk})
        eng.refresh_from_db()
        self.assertEqual(eng.stage.key, "new")

    def test_win_lose_reopen(self):
        eng = self.eng()
        self.post("erp-crm-engagement-lose", eng, {"loss_reason": ""})
        eng.refresh_from_db()
        self.assertEqual(eng.status, "open")
        self.post("erp-crm-engagement-lose", eng, {"loss_reason": "unknown"})  # backfill-only key
        eng.refresh_from_db()
        self.assertEqual(eng.status, "open")
        self.post("erp-crm-engagement-lose", eng, {"loss_reason": "competitor", "loss_competitor": "Rival"})
        eng.refresh_from_db()
        self.assertEqual((eng.status, eng.loss_reason, eng.loss_competitor), ("lost", "competitor", "Rival"))
        self.assertEqual(eng.stage.key, "new")  # closing keeps the stage
        # Reopen needs the permission.
        self.post("erp-crm-engagement-reopen", eng)
        eng.refresh_from_db()
        self.assertEqual(eng.status, "lost")
        self.user.user_permissions.add(Permission.objects.get(codename="reopen_engagement"))
        self.post("erp-crm-engagement-reopen", eng, {"stage": Stage.objects.filter(
            company=self.other_company).first().pk})  # another tenant's stage: refused, not ignored
        eng.refresh_from_db()
        self.assertEqual(eng.status, "lost")
        self.post("erp-crm-engagement-reopen", eng, {"stage": self.stage("proposal_sent").pk})
        eng.refresh_from_db()
        self.assertEqual((eng.status, eng.stage.key), ("open", "proposal_sent"))
        self.post("erp-crm-engagement-win", eng)
        eng.refresh_from_db()
        self.assertEqual(eng.status, "won")
        self.assertEqual(eng.stage_changes.count(), 4)  # create, lose, reopen, win

    def test_close_remaining_cancels_open_tasks(self):
        eng = self.eng()
        task = tasks.create(engagement=eng, title="Welcome pack", assignee=self.me)
        engagements.win(eng, by=self.me)
        response = self.client.get(reverse("erp-crm-engagement-detail", args=[eng.pk]))
        self.assertContains(response, "Close remaining")
        self.post("erp-crm-engagement-close-remaining", eng)
        task.refresh_from_db()
        self.assertEqual(task.status, "cancelled")
        self.assertTrue(task.events.filter(event="cancelled", by=self.me).exists())

    def test_task_create_sets_next_action(self):
        eng = self.eng()
        due = self.today() + datetime.timedelta(days=1)
        self.post("erp-crm-engagement-task-create", eng, {
            "title": "Send pricing", "kind": "email", "due_date": due.isoformat(), "assignee": self.colleague.pk,
            "priority": "high"})
        eng.refresh_from_db()
        self.assertEqual(eng.next_task.title, "Send pricing")
        self.assertEqual((eng.next_task.assignee, eng.next_task.priority, eng.next_task.all_day),
                         (self.colleague, "high", True))
        self.assertEqual(eng.next_task.events.get().event, "created")

    def test_task_with_time_and_bad_input(self):
        eng = self.eng()
        self.post("erp-crm-engagement-task-create", eng, {"title": "", "kind": "todo", "priority": "normal"})
        self.assertFalse(eng.tasks.exists())
        self.post("erp-crm-engagement-task-create", eng, {
            "title": "Call", "kind": "call", "due_date": "2031-03-04", "due_time": "14:30", "priority": "normal",
            "assignee": self.me.pk})
        task = eng.tasks.get()
        self.assertFalse(task.all_day)

    def test_complete_opens_whats_next_and_reschedule(self):
        eng = self.eng()
        first = self.task(eng, due=self.today())
        second = self.task(eng, due=self.today() + datetime.timedelta(days=5), title="Later")
        response = self.client.post(reverse("erp-crm-task-complete", args=[first.pk]))
        self.assertIn("whats_next=1", response["Location"])
        self.assertContains(self.client.get(response["Location"]), "What&#x27;s next?")
        first.refresh_from_db()
        self.assertEqual((first.status, first.completed_by), ("done", self.me))
        eng.refresh_from_db()
        self.assertEqual(eng.next_task_id, second.pk)
        self.client.post(reverse("erp-crm-task-reschedule", args=[second.pk]), {"when": "tomorrow"})
        second.refresh_from_db()
        self.assertEqual(second.due_at.astimezone(zoneinfo.ZoneInfo(second.due_tz)).date(),
                         timezone.now().astimezone(zoneinfo.ZoneInfo(second.due_tz)).date()
                         + datetime.timedelta(days=1))
        self.assertTrue(second.events.filter(event="rescheduled").exists())
        self.client.post(reverse("erp-crm-task-reassign", args=[second.pk]), {"assignee": self.colleague.pk})
        second.refresh_from_db()
        self.assertEqual(second.assignee, self.colleague)
        self.client.post(reverse("erp-crm-task-reopen", args=[first.pk]))
        first.refresh_from_db()
        self.assertEqual(first.status, "open")

    def test_reassign_refuses_another_tenants_profile(self):
        eng = self.eng()
        task = self.task(eng)
        self.client.post(reverse("erp-crm-task-reassign", args=[task.pk]), {"assignee": self.other_me.pk})
        task.refresh_from_db()
        self.assertEqual(task.assignee, self.me)

    def test_next_is_never_offsite(self):
        eng = self.eng()
        task = self.task(eng)
        response = self.client.post(reverse("erp-crm-task-cancel", args=[task.pk]),
                                    {"next": "https://evil.example/"})
        self.assertTrue(response["Location"].startswith("/erp/crm/"))

    def test_contacts_add_and_remove(self):
        eng = self.eng()
        bob = person(self.company, "Bob", "Builder")
        self.post("erp-crm-engagement-contact-create", eng, {"party": str(bob.pk), "role": "billing",
                                                            "is_primary": "on"})
        contact = eng.contacts.get()
        self.assertEqual((contact.party, contact.role, contact.is_primary), (bob, "billing", True))
        # Organisations and other tenants' people are refused.
        self.post("erp-crm-engagement-contact-create", eng, {"party": str(self.org.pk), "role": "other"})
        self.post("erp-crm-engagement-contact-create", eng,
                  {"party": str(person(self.other_company).pk), "role": "other"})
        self.assertEqual(eng.contacts.count(), 1)
        self.assertContains(self.client.get(reverse("erp-crm-engagement-detail", args=[eng.pk])),
                            reverse("erp-crm-contact-remove", args=[contact.pk]))
        self.client.post(reverse("erp-crm-contact-remove", args=[contact.pk]))
        self.assertFalse(eng.contacts.exists())


class LogCommunicationTests(CrmViewTestBase):
    def url(self, eng):
        return reverse("erp-crm-engagement-log", args=[eng.pk])

    def test_logs_through_ingest_and_sets_next_action(self):
        eng = self.eng()
        Engagement.objects.filter(pk=eng.pk).update(last_activity_at=timezone.now() - datetime.timedelta(days=30))
        email = PartyEmail.objects.create(party=self.org, email="buyer@acme.example", purpose="work",
                                          is_primary=True)
        form = self.client.get(self.url(eng)).context["form"]
        choice = next(value for value, _label in form.fields["participants"].choices if value == f"e:{email.pk}")
        when = timezone.now().astimezone(VIEWER_TZ) - datetime.timedelta(hours=1)
        response = self.client.post(self.url(eng), {
            "kind": "call", "direction": "outbound", "occurred_at": when.strftime("%Y-%m-%dT%H:%M"),
            "duration_minutes": "12", "subject": "Pricing call", "body_text": "They want 10% off.",
            "participants": [choice], "next_title": "Send revised quote",
            "next_due": (self.today() + datetime.timedelta(days=2)).isoformat(), "next_assignee": self.me.pk,
        })
        self.assertEqual(response.status_code, 302)
        comm = Communication.objects.get(engagement=eng)
        self.assertEqual((comm.kind, comm.provider, comm.duration_seconds, comm.performed_by),
                         ("call", "manual", 720, self.me))
        self.assertEqual(comm.participants.get().party, self.org)
        eng.refresh_from_db()
        self.assertGreater(eng.last_activity_at, timezone.now() - datetime.timedelta(hours=2))
        self.assertEqual(eng.next_task.title, "Send revised quote")
        self.assertEqual([str(m) for m in get_messages(response.wsgi_request)], ["Logged. Task added."])
        timeline = self.client.get(reverse("erp-crm-engagement-detail", args=[eng.pk]))
        self.assertContains(timeline, "Pricing call")
        self.assertContains(timeline, "They want 10% off.")

    def test_a_task_due_after_the_next_action_shows_in_the_next_action_card(self):
        """Bug batch 2026-10-15: a task added from the log, due after the
        existing next action, was listed only in a card near the bottom of
        the page, so it looked as if it had not been created."""
        eng = self.eng()
        self.task(eng, due=self.today(), title="Respond to inquiry", assignee=None)
        now = timezone.now().astimezone(VIEWER_TZ)
        self.client.post(self.url(eng), {
            "kind": "note", "direction": "internal", "occurred_at": now.strftime("%Y-%m-%dT%H:%M"),
            "subject": "Met them", "next_title": "Meet up at the spot",
            "next_due": (self.today() + datetime.timedelta(days=2)).isoformat(), "next_assignee": self.colleague.pk,
        })
        eng.refresh_from_db()
        self.assertEqual(eng.next_task.title, "Respond to inquiry")
        html = self.client.get(reverse("erp-crm-engagement-detail", args=[eng.pk])).content.decode()
        card = html[html.index('id="next-action"'):html.index('id="deal-facts"')]
        self.assertIn("Respond to inquiry", card)
        self.assertIn("Also open", card)
        self.assertIn("Meet up at the spot", card)
        listed = self.client.get(reverse("erp-crm-task-list"))
        self.assertEqual({t.title for t in listed.context["object_list"]},
                         {"Respond to inquiry", "Meet up at the spot"})

    def test_refusals(self):
        eng = self.eng()
        now = timezone.now().astimezone(VIEWER_TZ)
        base = {"kind": "note", "direction": "outbound", "occurred_at": now.strftime("%Y-%m-%dT%H:%M"),
                "subject": "x"}
        self.assertEqual(self.client.post(self.url(eng), base).status_code, 400)  # a note is internal
        future = dict(base, direction="internal",
                      occurred_at=(now + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"))
        self.assertEqual(self.client.post(self.url(eng), future).status_code, 400)
        no_text = dict(base, direction="internal", subject="")
        self.assertEqual(self.client.post(self.url(eng), no_text).status_code, 400)
        self.assertFalse(Communication.objects.filter(engagement=eng).exists())
        ok = dict(base, direction="internal")
        self.assertEqual(self.client.post(self.url(eng), ok).status_code, 302)


class EngagementFormTests(CrmViewTestBase):
    def test_create_through_the_service(self):
        PartyEmail.objects.create(party=self.ada, email="ada@example.com", purpose="work", is_primary=True)
        response = self.client.post(reverse("erp-crm-engagement-create"), {
            "party": "ADA@example.com", "owner": self.me.pk, "currency": "usd", "expected_value": "2500",
            "source": "referral", "tags_text": "solar, retrofit"})
        eng = Engagement.objects.get(company=self.company, party=self.ada)
        self.assertRedirects(response, reverse("erp-crm-engagement-detail", args=[eng.pk]) + "#next-action",
                             fetch_redirect_response=False)
        self.assertEqual((eng.origin, eng.currency, eng.tags, eng.stage.key), ("erp", "USD", ["solar", "retrofit"],
                                                                               "new"))
        self.assertEqual(eng.stage_changes.get().source, "erp")

    def test_create_refuses_other_tenant_party(self):
        theirs = person(self.other_company, "Theirs", "Person")
        response = self.client.post(reverse("erp-crm-engagement-create"), {"party": str(theirs.pk),
                                                                           "currency": "USD"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Engagement.objects.filter(party=theirs).exists())

    def test_edit_resolves_counterparty_and_guards_owner(self):
        eng = self.eng(party=None, owner=self.colleague)
        url = reverse("erp-crm-engagement-edit", args=[eng.pk])
        response = self.client.post(url, {"party": str(self.org.pk), "owner": self.me.pk, "currency": "USD"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post(url, {"party": str(self.org.pk), "owner": self.colleague.pk,
                                          "currency": "USD", "probability": "40"})
        self.assertEqual(response.status_code, 302)
        eng.refresh_from_db()
        self.assertEqual((eng.party, eng.probability), (self.org, 40))
        self.user.user_permissions.add(Permission.objects.get(codename="reassign_engagement"))
        self.user = type(self.user).objects.get(pk=self.user.pk)
        self.client.force_login(self.user)
        self.client.post(url, {"party": str(self.org.pk), "owner": self.me.pk, "currency": "USD"})
        eng.refresh_from_db()
        self.assertEqual(eng.owner, self.me)


# --- 6.3 the timeline -----------------------------------------------------------------


class TimelineTests(CrmViewTestBase):
    def test_grouping_by_day_and_thread(self):
        eng = self.eng()
        now = timezone.now()
        for i in range(3):
            ingest.record(self.company, "email", direction="outbound", occurred_at=now - datetime.timedelta(minutes=i),
                          engagement=eng, party=self.org, subject="Quote QT-00123", thread_key="golden:quote:1")
        ingest.record(self.company, "note", direction="internal", occurred_at=now - datetime.timedelta(days=1),
                      engagement=eng, subject="Yesterday's note")
        engagements.move(eng, self.stage("negotiation"), by=self.me)
        response = self.client.get(reverse("erp-crm-engagement-detail", args=[eng.pk]))
        days = response.context["timeline_days"]
        self.assertEqual([d.label for d in days[:2]], ["Today", "Yesterday"])
        thread = next(r for r in days[0].rows if r.type == "thread")
        self.assertEqual(len(thread.items), 3)
        self.assertContains(response, "3 emails in &#x27;Quote QT-00123&#x27;")
        self.assertContains(response, "Moved New → Negotiation")

    def test_order_ties_and_upcoming(self):
        eng = self.eng()
        future = self.task(eng, due=self.today() + datetime.timedelta(days=4), title="Future call")
        response = self.client.get(reverse("erp-crm-engagement-detail", args=[eng.pk]))
        self.assertEqual([r["title"] for r in response.context["timeline_upcoming"]], [future.title])
        self.assertContains(response, "Upcoming")

    def test_cursor_paging_and_kinds(self):
        eng = self.eng()
        now = timezone.now()
        for i in range(60):
            ingest.record(self.company, "call", direction="outbound",
                          occurred_at=now - datetime.timedelta(hours=i + 1), engagement=eng, subject=f"Call {i:02d}")
        url = reverse("erp-crm-engagement-timeline", args=[eng.pk])
        first = self.client.get(url)
        cursor = first.context["timeline_next"]
        self.assertTrue(cursor)
        self.assertContains(first, "Load older")
        seen = {c["subject"] for d in first.context["timeline_days"] for r in d.rows for c in r.items}
        second = self.client.get(url, {"before": cursor})
        later = {c["subject"] for d in second.context["timeline_days"] for r in d.rows for c in r.items}
        self.assertFalse(seen & later)
        self.assertEqual(len(seen | later), 60)
        self.assertIsNone(second.context["timeline_upcoming"])
        calls_only = self.client.get(url, {"kinds": "stage"})
        self.assertEqual([r.type for d in calls_only.context["timeline_days"] for r in d.rows], ["stage"])
        bad = self.client.get(url, {"before": "nonsense"})
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, "not valid")

    def test_query_count_is_constant(self):
        eng = self.eng()
        url = reverse("erp-crm-engagement-timeline", args=[eng.pk])
        now = timezone.now()

        def grow(n):
            for i in range(n):
                ingest.record(self.company, "email", direction="outbound", occurred_at=now - datetime.timedelta(hours=i),
                              engagement=eng, party=self.org, subject=f"E{i}",
                              participants=[{"role": "to", "party": self.org}])
                done = self.task(eng, title=f"D{i}")
                tasks.complete(done, by=self.me)

        grow(2)
        self.client.get(url)
        with CaptureQueriesContext(connection) as small:
            self.client.get(url)
        grow(30)
        with CaptureQueriesContext(connection) as large:
            self.client.get(url)
        self.assertEqual(len(small), len(large))


# --- 6.4 My day, 6.5 tasks, leads, pipelines ----------------------------------------


class MyDayTests(CrmViewTestBase):
    url = reverse("erp-crm-my-day")

    def test_sections_are_mine(self):
        now = timezone.now()
        eng = self.eng()
        overdue = self.task(eng, due=now - datetime.timedelta(hours=3), title="Overdue one")
        today = self.task(eng, due=self.today(), title="Today one")
        later = self.task(eng, due=self.today() + datetime.timedelta(days=3), title="Later one")
        self.task(self.eng(), due=now - datetime.timedelta(hours=1), title="Kim's", assignee=self.colleague)
        idle = self.eng(title="Idle deal")
        self.eng(owner=self.colleague, title="Kim's idle")
        response = self.client.get(self.url)
        self.assertEqual([t.pk for t in response.context["overdue"]], [overdue.pk])
        self.assertEqual([t.pk for t in response.context["due_today"]], [today.pk])
        self.assertEqual([e.pk for e in response.context["idle"]], [idle.pk])
        self.assertNotContains(response, later.title)
        self.assertNotContains(response, "Kim&#x27;s")
        self.assertContains(response, "Snooze")

    def test_today_is_the_viewers_zone(self):
        UserProfile.objects.filter(pk=self.me.pk).update(timezone="Pacific/Kiritimati")  # UTC+14
        eng = self.eng()
        task = self.task(eng, due=timezone.now() + datetime.timedelta(hours=2))
        response = self.client.get(self.url)
        self.assertEqual(response.context["viewer_tz"], "Pacific/Kiritimati")
        local_end = (timezone.now().astimezone(zoneinfo.ZoneInfo("Pacific/Kiritimati")).replace(
            hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1))
        in_today = task.due_at < local_end
        self.assertEqual(task.pk in [t.pk for t in response.context["due_today"]], in_today)

    def test_empty_states(self):
        response = self.client.get(self.url)
        self.assertContains(response, "All clear")
        self.eng()
        response = self.client.get(self.url)
        self.assertContains(response, "Nothing due. 1")

    def test_snooze_returns_to_my_day(self):
        task = self.task(self.eng(), due=timezone.now() - datetime.timedelta(hours=1))
        response = self.client.post(reverse("erp-crm-task-reschedule", args=[task.pk]),
                                    {"when": "next_week", "next": self.url})
        self.assertEqual(response["Location"], self.url)
        task.refresh_from_db()
        self.assertEqual(task.due_at.astimezone(zoneinfo.ZoneInfo(task.due_tz)).weekday(), 0)

    def test_superuser_in_another_tenant_is_not_the_author(self):
        from django.contrib.auth import get_user_model

        root = get_user_model().objects.create_superuser("root2", password="pw")
        UserProfile.objects.update_or_create(user=root, defaults={"company": self.other_company})
        self.client.force_login(root)
        eng = self.eng()
        self.client.post(reverse("erp-crm-engagement-move", args=[eng.pk]), {"stage": self.stage("negotiation").pk})
        change = eng.stage_changes.order_by("-changed_at").first()
        self.assertEqual((change.to_stage.key, change.changed_by), ("negotiation", None))

    def test_superuser_without_profile(self):
        from django.contrib.auth import get_user_model

        root = get_user_model().objects.create_superuser("root", password="pw")
        self.client.force_login(root)
        self.assertContains(self.client.get(self.url), "no staff profile")


class TaskListTests(CrmViewTestBase):
    url = reverse("erp-crm-task-list")

    def test_default_everyone_open_by_due(self):
        eng = self.eng()
        b = self.task(eng, due=self.today() + datetime.timedelta(days=2), title="B")
        a = self.task(eng, due=self.today(), title="A")
        done = self.task(eng, title="Done")
        tasks.complete(done, by=self.me)
        kim = self.task(eng, title="Kim", assignee=self.colleague)
        nobody = self.task(eng, title="Nobody", assignee=None)
        # Default: every open task, unassigned and colleagues' included (a new
        # engagement's intake task is unassigned, and the list looked empty).
        response = self.client.get(self.url)
        self.assertEqual([t.pk for t in response.context["object_list"]], [a.pk, b.pk, kim.pk, nobody.pk])
        response = self.client.get(self.url, {"status": "done"})
        self.assertEqual([t.pk for t in response.context["object_list"]], [done.pk])
        self.assertContains(response, "Reopen")
        response = self.client.get(self.url, {"assignee": "me"})
        self.assertEqual([t.pk for t in response.context["object_list"]], [a.pk, b.pk])
        response = self.client.get(self.url, {"assignee": "none"})
        self.assertEqual([t.pk for t in response.context["object_list"]], [nobody.pk])
        response = self.client.get(self.url, {"assignee": "me", "due": "today"})
        self.assertEqual([t.pk for t in response.context["object_list"]], [a.pk])
        csv = self.client.get(self.url, {"export": "csv"})
        self.assertTrue(b"".join(csv.streaming_content).decode().startswith(
            "title,related,due_at,assignee,priority,status"))

    def test_other_tenant_tasks_never_show(self):
        theirs = tasks.create(engagement=self.other_eng(), title="Theirs")
        response = self.client.get(self.url, {"assignee": "all", "status": "all"})
        self.assertNotIn(theirs.pk, [t.pk for t in response.context["object_list"]])


class LeadListTests(CrmViewTestBase):
    def test_intake_log_links_engagements(self):
        lead = Lead.objects.create(company=self.company, lead_number="LD-90001", first_name="Lea",
                                   last_name="Lead", email="lea@example.com", source="website_form",
                                   request_type="quote")
        Lead.objects.create(company=self.other_company, lead_number="LD-90002", first_name="Other",
                            last_name="Tenant", email="other@example.com")
        response = self.client.get(reverse("erp-crm-lead-list"))
        self.assertContains(response, "Lea Lead")  # 5.1: the lead number is an optional column
        self.assertNotContains(response, "Other Tenant")
        lead.refresh_from_db()
        self.assertContains(response, reverse("erp-crm-engagement-detail", args=[lead.engagement_id]))


class PipelineAdminTests(CrmViewTestBase):
    def grant(self):
        self.user.user_permissions.add(Permission.objects.get(codename="manage_pipelines"))
        self.user = type(self.user).objects.get(pk=self.user.pk)
        self.client.force_login(self.user)

    def test_read_for_staff_write_needs_permission(self):
        response = self.client.get(reverse("erp-crm-pipeline-list"))
        self.assertContains(response, "Negotiation")
        # R10.04: the stages table scrolls in its own, focusable, named box
        # at 360 px (the sweep's rule), and the header's buttons wrap.
        self.assertContains(response, 'class="table-responsive p-0" tabindex="0" role="region" aria-label="Stages of ')
        self.assertNotContains(response, reverse("erp-crm-pipeline-create"))
        stage = self.stage("new")
        for url in (reverse("erp-crm-pipeline-create"), reverse("erp-crm-pipeline-edit", args=[self.pipeline.pk]),
                    reverse("erp-crm-stage-create", args=[self.pipeline.pk]),
                    reverse("erp-crm-stage-edit", args=[stage.pk])):
            self.assertEqual(self.client.get(url).status_code, 403, url)
            self.assertEqual(self.client.post(url, {}).status_code, 403, url)
        self.assertEqual(self.client.post(reverse("erp-crm-stage-reorder", args=[stage.pk]),
                                          {"direction": "down"}).status_code, 403)

    def test_create_edit_default_and_key_immutable(self):
        self.grant()
        data = {"key": "service", "name": "Service", "is_default": "on", "is_active": "on", "sort_order": 1,
                "stale_after_days": 7}
        # A pipeline without an active stage cannot become the default.
        self.assertEqual(self.client.post(reverse("erp-crm-pipeline-create"), data).status_code, 400)
        response = self.client.post(reverse("erp-crm-pipeline-create"), dict(data, is_default=""))
        self.assertEqual(response.status_code, 302)
        service = Pipeline.objects.get(company=self.company, key="service")
        self.assertFalse(service.is_default)
        self.client.post(reverse("erp-crm-pipeline-edit", args=[service.pk]), data)
        service.refresh_from_db()
        self.assertFalse(service.is_default)
        Stage.objects.create(pipeline=service, key="triage", name="Triage", sort_order=10)
        self.client.post(reverse("erp-crm-pipeline-edit", args=[service.pk]), data)
        service.refresh_from_db()
        self.pipeline.refresh_from_db()
        self.assertTrue(service.is_default)
        self.assertFalse(self.pipeline.is_default)
        self.client.post(reverse("erp-crm-pipeline-edit", args=[service.pk]), {
            "key": "changed", "name": "Service desk", "is_default": "on", "is_active": "on", "sort_order": 1,
            "stale_after_days": 9})
        service.refresh_from_db()
        self.assertEqual((service.key, service.name, service.stale_after_days), ("service", "Service desk", 9))
        dup = self.client.post(reverse("erp-crm-pipeline-create"), {
            "key": "sales", "name": "Dup", "is_active": "on", "sort_order": 0, "stale_after_days": 14})
        self.assertEqual(dup.status_code, 400)

    def test_stage_editor(self):
        self.grant()
        url = reverse("erp-crm-stage-create", args=[self.pipeline.pk])
        self.client.post(url, {"key": "closed_won", "name": "Closed won", "default_probability": 100,
                               "outcome_hint": "won", "is_active": "on"})
        stage = self.pipeline.stages.get(key="closed_won")
        self.assertEqual(stage.sort_order, self.pipeline.stages.order_by("-sort_order")[1].sort_order + 10)
        self.assertEqual(self.client.post(url, {"key": "x", "name": "X", "default_probability": 140,
                                                "is_active": "on"}).status_code, 400)
        self.client.post(reverse("erp-crm-stage-edit", args=[stage.pk]), {
            "key": "renamed", "name": "Won", "default_probability": 100, "outcome_hint": "won",
            "stale_after_days": 3})
        stage.refresh_from_db()
        self.assertEqual((stage.key, stage.name, stage.is_active, stage.stale_after_days),
                         ("closed_won", "Won", False, 3))

    def test_reorder_with_buttons(self):
        self.grant()
        keys = lambda: list(self.pipeline.stages.order_by("sort_order").values_list("key", flat=True))  # noqa: E731
        before = keys()
        second = self.pipeline.stages.get(key=before[1])
        self.client.post(reverse("erp-crm-stage-reorder", args=[second.pk]), {"direction": "up"})
        self.assertEqual(keys()[:2], [before[1], before[0]])
        first = self.pipeline.stages.get(key=before[1])
        self.client.post(reverse("erp-crm-stage-reorder", args=[first.pk]), {"direction": "up"})  # no-op
        self.assertEqual(keys()[:2], [before[1], before[0]])

    def test_other_tenant_pipeline_is_404(self):
        self.grant()
        theirs = Pipeline.objects.get(company=self.other_company, key="sales")
        self.assertEqual(self.client.get(reverse("erp-crm-pipeline-edit", args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("erp-crm-stage-reorder", args=[theirs.stages.first().pk]),
                                          {"direction": "down"}).status_code, 404)


# --- seed_crm -------------------------------------------------------------------------


class SeedCrmTests(TestCase):
    def test_idempotent_top_up_and_clean_checks(self):
        from core.plugins import signals as plugin_signals

        company = Company.objects.create(name="Seed Co")
        received = []

        def receiver(sender, **kwargs):
            received.append(sender)

        plugin_signals.engagement_created.connect(receiver)
        try:
            with self.captureOnCommitCallbacks(execute=True):
                call_command("seed_crm", count=45, stdout=StringIO())
        finally:
            plugin_signals.engagement_created.disconnect(receiver)
        self.assertEqual(received, [])
        seeded = Engagement.objects.filter(company=company, metadata__seed="seed_crm")
        self.assertEqual(seeded.count(), 45)
        call_command("seed_crm", count=45, stdout=StringIO())
        self.assertEqual(seeded.count(), 45)
        call_command("seed_crm", count=50, stdout=StringIO())
        self.assertEqual(seeded.count(), 50)
        self.assertEqual(len(set(seeded.values_list("number", flat=True))), 50)
        statuses = set(seeded.values_list("status", flat=True))
        self.assertEqual(statuses, {"open", "won", "lost"})
        # An open deal never sits in Closed/Signed (a stage that proposes closing).
        self.assertFalse(seeded.filter(status="open", stage__outcome_hint="won").exists())
        self.assertTrue(seeded.filter(status="open", next_task__isnull=True).exists())
        self.assertTrue(seeded.filter(next_action_due_at__lt=timezone.now()).exists())
        self.assertTrue(EngagementStageChange.objects.filter(engagement__in=seeded, from_stage__isnull=False).exists())
        self.assertTrue(Communication.objects.filter(engagement__in=seeded).exclude(thread_key="").exists())
        self.assertTrue(TaskEvent.objects.filter(task__engagement__in=seeded, event="completed").exists())
        self.assertTrue(seeded.filter(first_touch__isnull=False).exists())
        for command in ("check_engagements", "check_tenancy"):
            call_command(command, stdout=StringIO())
        out = StringIO()
        call_command("reconcile_engagements", "--check", stdout=out)
        # A new engagement after the seed takes the next free number.
        eng = engagements.create(company=company)
        self.assertNotIn(eng.number, set(seeded.values_list("number", flat=True)))
        self.assertEqual(Task.objects.filter(engagement__in=seeded, status="open").count(),
                         seeded.filter(next_task__isnull=False).count())
