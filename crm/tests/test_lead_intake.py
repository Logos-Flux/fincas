"""CRM Phase 3 step 6: lead intake and the frozen lead (spec 1.7).

A new lead goes onto the engagement thread from ``Lead.save()`` (after the
party sync): support requests open nothing, a repeat enquiry joins the
party's open engagement, anything else opens one at stage ``new`` with an
open "Respond to inquiry" task. Once triaged, the lead's workflow fields are
refused on the API (400 ``lead_triaged``) and ``status`` is derived from the
engagement.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib import admin
from django.core.management import CommandError, call_command
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from communications.models import Communication
from core.models import UserProfile
from core.tests.helpers import make_staff_user
from crm import engagements
from crm.models import Engagement, Lead, Task, Touchpoint
from parties.models import PartyEmail

from .helpers import CrmTestCase, person, profile


def _lead(company, email="ada@example.com", **kwargs):
    kwargs.setdefault("first_name", "Ada")
    kwargs.setdefault("last_name", "Lovelace")
    kwargs.setdefault("request_type", Lead.RequestType.QUOTE)
    return Lead.objects.create(company=company, email=email, **kwargs)


class IntakeTests(CrmTestCase):
    def test_an_ai_chat_lead_keeps_its_source_on_the_engagement(self):
        lead = _lead(self.company, source=Lead.Source.AI_CHAT)
        lead.refresh_from_db()
        self.assertEqual(lead.engagement.source, "ai_chat")
        self.assertEqual(lead.get_source_display(), "AI Chat (Claude, Gemini, Muse)")

    def test_a_new_enquiry_opens_an_engagement_with_a_next_action(self):
        before = timezone.now()
        lead = _lead(self.company, assigned_to=self.rep, estimated_value=Decimal("1200.00"),
                     source=Lead.Source.REFERRAL, message="Need 40 panels")
        lead.refresh_from_db()
        eng = lead.engagement
        self.assertIsNotNone(eng)
        self.assertEqual((eng.status, eng.stage.key, eng.origin), ("open", "new", "lead"))
        self.assertEqual(eng.pipeline, self.pipeline)
        self.assertEqual(eng.owner, self.rep)
        self.assertEqual(eng.expected_value, Decimal("1200.00"))
        self.assertEqual(eng.source, "referral")
        self.assertEqual(eng.party, lead.party)
        self.assertIsNotNone(lead.party)
        task = Task.objects.get(engagement=eng)
        self.assertEqual((task.status, task.kind, task.source), ("open", "follow_up", "intake:lead"))
        self.assertEqual(task.title, f"Respond to inquiry {lead.lead_number}")
        self.assertEqual(task.assignee, self.rep)
        self.assertFalse(task.all_day)
        self.assertGreaterEqual(task.due_at, before + timedelta(hours=24))
        self.assertLessEqual(task.due_at, timezone.now() + timedelta(hours=24))
        eng.refresh_from_db()
        self.assertEqual(eng.next_task, task)
        self.assertEqual(eng.stage_changes.count(), 1)
        note = Communication.objects.get(engagement=eng)
        self.assertEqual((note.kind, note.direction, note.provider, note.is_editable),
                         ("note", "internal", "lead_form", False))
        self.assertEqual(note.body_text, "Need 40 panels")
        self.assertEqual(note.party, lead.party)
        self.assertEqual(note.metadata["lead"], str(lead.pk))
        self.assertEqual(note.metadata["lead_direction"], "inbound")

    def test_every_request_type_but_support_opens_one(self):
        for n, request_type in enumerate(("quote", "info", "partnership", "other", "")):
            with self.subTest(request_type=request_type):
                lead = _lead(self.company, email=f"p{n}@example.com", request_type=request_type)
                lead.refresh_from_db()
                self.assertIsNotNone(lead.engagement_id)
                self.assertTrue(Task.objects.filter(engagement=lead.engagement, status="open").exists())

    def test_every_new_engagement_has_an_open_task(self):
        for n in range(3):
            _lead(self.company, email=f"n{n}@example.com")
        for eng in Engagement.objects.filter(origin="lead"):
            self.assertIsNotNone(eng.next_task_id, eng)

    def test_a_support_request_opens_none_and_lands_on_the_timeline_only(self):
        # Even when the party has exactly one open engagement: a support
        # request is not a deal message.
        first = _lead(self.company, request_type="quote")
        first.refresh_from_db()
        engagements_before = Engagement.objects.count()
        lead = _lead(self.company, request_type=Lead.RequestType.SUPPORT, message="It broke")
        lead.refresh_from_db()
        self.assertIsNone(lead.engagement_id)
        self.assertEqual(Engagement.objects.count(), engagements_before)
        note = Communication.objects.get(metadata__lead=str(lead.pk))
        self.assertIsNone(note.engagement_id)
        self.assertEqual(note.party, first.party)
        self.assertEqual(note.body_text, "It broke")

    def test_a_repeat_enquiry_attaches_to_the_open_engagement(self):
        first = _lead(self.company)
        first.refresh_from_db()
        eng = first.engagement
        Engagement.objects.filter(pk=eng.pk).update(last_activity_at=timezone.now() - timedelta(days=3))
        second = _lead(self.company, request_type="info", message="Any update?")
        second.refresh_from_db()
        self.assertEqual(second.engagement, eng)
        self.assertEqual(Engagement.objects.filter(party=first.party).count(), 1)
        self.assertEqual(Task.objects.filter(engagement=eng).count(), 1)  # no second task
        self.assertEqual(Communication.objects.filter(engagement=eng).count(), 2)
        eng.refresh_from_db()
        self.assertGreater(eng.last_activity_at, timezone.now() - timedelta(minutes=1))

    def test_a_repeat_enquiry_from_a_contact_attaches_too(self):
        lead = _lead(self.company)
        lead.refresh_from_db()
        deal = self.engagement(party=self.org)
        Engagement.objects.filter(pk=lead.engagement_id).update(status="won", closed_at=timezone.now())
        deal.contacts.create(party=lead.party, role="champion")
        again = _lead(self.company, request_type="info")
        again.refresh_from_db()
        self.assertEqual(again.engagement, deal)

    def test_a_closed_engagement_takes_no_repeat_enquiry(self):
        first = _lead(self.company)
        first.refresh_from_db()
        engagements.lose(first.engagement, "price")
        second = _lead(self.company, request_type="info")
        second.refresh_from_db()
        self.assertNotEqual(second.engagement, first.engagement)
        self.assertEqual(second.engagement.status, "open")

    def test_an_unresolved_party_still_gets_an_engagement(self):
        # Two people share the address: the party sync leaves match-N unlinked.
        for given in ("Ann", "Bea"):
            PartyEmail.objects.create(party=person(self.company, given, "Dup"),
                                      email="shared@example.com", purpose="work")
        lead = _lead(self.company, email="shared@example.com")
        lead.refresh_from_db()
        self.assertIsNone(lead.party_id)
        self.assertIsNotNone(lead.engagement)
        self.assertIsNone(lead.engagement.party_id)
        self.assertTrue(lead.engagement.title.startswith("No counterparty"))
        # A second unresolved lead never joins it: there is no party to match.
        other = _lead(self.company, email="shared@example.com")
        other.refresh_from_db()
        self.assertNotEqual(other.engagement, lead.engagement)

    def test_settings_pick_the_owner_and_the_response_time(self):
        owner = profile(self.company, "owner")
        stranger = profile(self.other_company, "stranger")
        with override_settings(GOLDEN_CRM={"lead_default_owner": str(owner.pk), "lead_response_hours": 2}):
            lead = _lead(self.company, email="o@example.com")
        lead.refresh_from_db()
        self.assertEqual(lead.engagement.owner, owner)
        task = lead.engagement.tasks.get()
        self.assertLess(task.due_at, timezone.now() + timedelta(hours=2, minutes=1))
        with override_settings(GOLDEN_CRM={"lead_default_owner": str(stranger.pk)}):
            other = _lead(self.company, email="x@example.com")
        other.refresh_from_db()
        self.assertIsNone(other.engagement.owner)  # another tenant's profile is never the owner

    def test_unknown_settings_keys_fail_loudly(self):
        from django.core.exceptions import ImproperlyConfigured

        from crm.conf import crm_setting

        with override_settings(GOLDEN_CRM={"lead_respons_hours": 2}):
            with self.assertRaises(ImproperlyConfigured):
                crm_setting("lead_response_hours")

    def test_attribution_writes_a_touchpoint_and_fixes_first_and_last_touch(self):
        lead = _lead(self.company, utm_source="google", utm_medium="cpc", utm_campaign="spring",
                     referrer_url="https://search.example/")
        lead.refresh_from_db()
        touch = Touchpoint.objects.get()
        self.assertEqual((touch.source, touch.utm_campaign, touch.party), ("lead_form", "spring", lead.party))
        self.assertEqual(touch.engagement, lead.engagement)
        self.assertEqual(touch.channel, "website_form")
        self.assertEqual((lead.engagement.first_touch, lead.engagement.last_touch), (touch, touch))
        _lead(self.company, email="plain@example.com")
        self.assertEqual(Touchpoint.objects.count(), 1)  # no attribution, no touchpoint

    def test_a_lead_without_a_company_is_left_alone(self):
        lead = _lead(None)
        lead.refresh_from_db()
        self.assertIsNone(lead.engagement_id)
        self.assertFalse(Communication.objects.exists())

    def test_saving_again_never_re_runs_intake(self):
        lead = _lead(self.company)
        lead.refresh_from_db()
        lead.notes = "called back"
        lead.save()
        self.assertEqual(Engagement.objects.filter(origin="lead").count(), 1)
        self.assertEqual(Communication.objects.count(), 1)

    @override_settings(GOLDEN_CRM_INTAKE_STRICT=False)
    def test_a_late_intake_failure_leaves_no_dangling_engagement_on_the_instance(self):
        with mock.patch("crm.intake._log_message", side_effect=RuntimeError("boom")), \
                self.assertLogs("crm.intake", "ERROR"):
            lead = _lead(self.company)
        self.assertIsNone(lead.engagement_id)
        self.assertFalse(Engagement.objects.exists())
        lead.notes = "saved again"
        lead.save()  # would hit a deleted engagement's FK otherwise
        lead.refresh_from_db()
        self.assertIsNone(lead.engagement_id)

    @override_settings(GOLDEN_CRM_INTAKE_STRICT=False)
    def test_an_intake_failure_keeps_the_lead_and_reconcile_repairs_it(self):
        with mock.patch("crm.intake.accept_lead", side_effect=RuntimeError("boom")), \
                self.assertLogs("crm.intake", "ERROR"):
            lead = _lead(self.company)
        lead.refresh_from_db()
        self.assertIsNone(lead.engagement_id)
        with self.assertRaises(CommandError):
            call_command("reconcile_engagements", "--check", stdout=_Sink())
        call_command("reconcile_engagements", "--fix", stdout=_Sink())
        lead.refresh_from_db()
        self.assertIsNotNone(lead.engagement_id)
        self.assertTrue(lead.engagement.tasks.filter(status="open").exists())
        call_command("reconcile_engagements", "--check", stdout=_Sink())


class _Sink:
    def write(self, *_args, **_kwargs):
        pass

    def flush(self):
        pass


class LeadFreezeTests(CrmTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user = make_staff_user("staffer", "s@example.com", "pw")
        UserProfile.objects.create(user=cls.user, company=cls.company)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _url(self, lead):
        return reverse("lead-detail", args=[lead.pk])

    def test_workflow_fields_are_refused_once_triaged(self):
        lead = _lead(self.company)
        lead.refresh_from_db()
        for name, value in (("status", "contacted"), ("assigned_to", str(self.rep.pk)),
                            ("follow_up_date", "2026-11-01"), ("contact_count", 3),
                            ("loss_reason", "price"), ("converted_at", "2026-10-01T00:00:00Z")):
            with self.subTest(field=name):
                response = self.client.patch(self._url(lead), {name: value}, format="json")
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.data["code"], "lead_triaged")
                self.assertEqual(response.data["fields"], [name])
                self.assertEqual(response.data["engagement"], str(lead.engagement_id))
        lead.refresh_from_db()
        self.assertEqual(lead.status, "new")

    def test_a_get_then_put_round_trip_saves(self):
        lead = _lead(self.company)
        lead.refresh_from_db()
        engagements.move(lead.engagement, self.stage("negotiation"))
        payload = self.client.get(self._url(lead)).data
        payload = {k: v for k, v in payload.items()
                   if k not in ("activities", "id", "created_at", "updated_at", "party", "engagement")}
        payload["notes"] = "round trip"
        response = self.client.put(self._url(lead), payload, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        lead.refresh_from_db()
        self.assertEqual((lead.notes, lead.status), ("round trip", "new"))

    def test_the_status_filter_matches_the_derived_status(self):
        moved = _lead(self.company, email="m@example.com")
        moved.refresh_from_db()
        engagements.move(moved.engagement, self.stage("proposal_sent"))
        fresh = _lead(self.company, email="f@example.com")
        support = _lead(self.company, email="s@example.com", request_type="support")
        Lead.objects.filter(pk=support.pk).update(status="contacted")

        def ids(status):
            return {r["id"] for r in self.client.get(f"{reverse('lead-list')}?status={status}").data["results"]}

        self.assertEqual(ids("proposal_sent"), {str(moved.pk)})
        self.assertEqual(ids("new"), {str(fresh.pk)})
        self.assertEqual(ids("contacted"), {str(support.pk)})
        engagements.win(moved.engagement)
        self.assertEqual(ids("converted"), {str(moved.pk)})
        self.assertEqual(self.client.get(f"{reverse('lead-list')}?status=bogus").status_code, 400)

    def test_other_fields_still_save(self):
        lead = _lead(self.company)
        response = self.client.patch(self._url(lead), {"notes": "prefers email"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        lead.refresh_from_db()
        self.assertEqual(lead.notes, "prefers email")

    def test_a_support_lead_keeps_its_workflow_fields(self):
        lead = _lead(self.company, request_type="support")
        response = self.client.patch(self._url(lead), {"status": "contacted"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["status"], "contacted")

    def test_engagement_is_read_only_on_the_api(self):
        lead = _lead(self.company, request_type="support")
        other = self.engagement()
        self.client.patch(self._url(lead), {"engagement": str(other.pk)}, format="json")
        lead.refresh_from_db()
        self.assertIsNone(lead.engagement_id)

    def test_status_is_derived_from_the_engagement(self):
        lead = _lead(self.company)
        lead.refresh_from_db()
        eng = lead.engagement

        def status():
            return self.client.get(self._url(lead)).data["status"]

        self.assertEqual(status(), "new")
        engagements.move(eng, self.stage("negotiation"))
        self.assertEqual(status(), "qualified")
        engagements.move(eng, self.stage("proposal_sent"))
        self.assertEqual(status(), "proposal_sent")
        engagements.win(eng)
        self.assertEqual(status(), "converted")
        listed = self.client.get(reverse("lead-list")).data["results"]
        self.assertEqual(listed[0]["status"], "converted")
        self.assertEqual(listed[0]["engagement"], eng.pk)
        engagements.reopen(eng, source="system")
        engagements.lose(eng, "price")
        self.assertEqual(status(), "closed_lost")
        engagements.reopen(eng, source="system")
        engagements.lose(eng, "disqualified")
        self.assertEqual(status(), "closed_junk")
        lead.refresh_from_db()
        self.assertEqual(lead.status, "new")  # the column is never rewritten

    def test_admin_shows_the_frozen_fields_read_only(self):
        from crm.admin import LeadAdmin

        model_admin = LeadAdmin(Lead, admin.site)
        request = RequestFactory().get("/")
        triaged = _lead(self.company)
        triaged.refresh_from_db()
        support = _lead(self.company, email="s@example.com", request_type="support")
        support.refresh_from_db()
        self.assertIn("status", model_admin.get_readonly_fields(request, triaged))
        self.assertNotIn("status", model_admin.get_readonly_fields(request, support))
