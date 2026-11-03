"""The assistant's CRM tools (docs/crm/phase3-engagement.md 5.3, step 10).

Drives the real ``erp_assist.core.crm`` functions against the in-process DRF
stack (``test_party_ambiguity``'s harness): the assistant authenticates with
the shared service key, which reads every tenant, so each test checks that a
call with tenant A's context never returns tenant B's rows.

Done-when (spec 8, step 10): ``draft_follow_up`` writes nothing (asserted by
row counts), a suppressed address refuses, an ambiguous assignee (and
for_user, owner, engagement) returns the envelope.
"""

from __future__ import annotations

import asyncio
import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from communications import ingest
from communications.models import Communication, CommunicationParticipant, EmailLog
from core.models import AuditLog, Company, UserProfile
from core.tests.helpers import make_staff_user
from crm import engagements as engagement_service
from crm import tasks as task_service
from crm.models import Engagement, EngagementContact, EngagementStageChange, Pipeline, Task, TaskEvent
from erp_assist import mcp_adapter
from erp_assist.core import ERPAmbiguousError, ERPNotFoundError, ERPValidationError
from erp_assist.core import crm as crm_core
from parties.consent import address_hash, normalize_address
from parties.models import ConsentRecord, Party, PartyEmail, PartyPhone, Suppression

from .test_party_ambiguity import API_KEY, asgi_client


def run(coro):
    return asyncio.run(coro)


def staff(company, username, email, *, tz=""):
    user = make_staff_user(username, email=email, password="pw")
    return UserProfile.objects.create(user=user, company=company, display_name=username, timezone=tz)


def person(company, given, family, email=None, phone=None):
    party = Party.objects.create(company=company, kind=Party.Kind.PERSON, given_name=given,
                                 family_name=family, origin=Party.Origin.ERP)
    if email:
        PartyEmail.objects.create(party=party, email=email, purpose="work", is_primary=True)
    if phone:
        PartyPhone.objects.create(party=party, number_raw=phone, number_e164=phone, purpose="work",
                                  line_type="mobile", is_primary=True)
    return party


ROW_MODELS = (Communication, CommunicationParticipant, EmailLog, Task, TaskEvent, Engagement,
              EngagementStageChange, EngagementContact, ConsentRecord, Suppression, AuditLog)


def row_counts():
    return {m.__name__: m._base_manager.count() for m in ROW_MODELS}


@override_settings(API_KEY=API_KEY)
class CrmToolTests(TransactionTestCase):
    def setUp(self):
        User.objects.create_superuser(username="api-service-crm", password=None)
        self.a = Company.objects.create(name="Tenant A")
        self.b = Company.objects.create(name="Tenant B")
        self.rep_a = staff(self.a, "rep-a", "rep.a@example.com", tz="Europe/Berlin")
        self.rep_b = staff(self.b, "rep-b", "rep.b@example.com")
        self.ada = person(self.a, "Ada", "Lovelace", email="ada@example.com", phone="+14155550101")
        self.bob = person(self.b, "Bob", "Builder", email="bob@example.com")
        self.eng_a = engagement_service.create(company=self.a, party=self.ada, owner=self.rep_a,
                                               title="Ada — solar array")
        self.eng_b = engagement_service.create(company=self.b, party=self.bob, owner=self.rep_b,
                                               title="Bob — solar array")
        self.client = asgi_client()

    def tearDown(self):
        run(self.client._http.aclose())

    def task(self, engagement, assignee, due, title="Call back"):
        return task_service.create(engagement=engagement, assignee=assignee, title=title, due=due)

    # ---- for_user: my_overdue / my_day -------------------------------------------------

    def test_my_day_sections_are_the_persons_own_tenant(self):
        now = timezone.now()
        overdue_a = self.task(self.eng_a, self.rep_a, now - datetime.timedelta(hours=2))
        later_a = self.task(self.eng_a, self.rep_a, now + datetime.timedelta(minutes=5), "Later today")
        self.task(self.eng_b, self.rep_b, now - datetime.timedelta(hours=2))
        idle_a = engagement_service.create(company=self.a, party=self.ada, owner=self.rep_a, title="Idle")
        engagement_service.create(company=self.b, party=self.bob, owner=self.rep_b, title="Idle B")
        day = run(crm_core.my_day(self.client, "Rep.A@example.com"))
        self.assertEqual(day["for_user"]["id"], str(self.rep_a.pk))
        self.assertEqual((day["company"], day["timezone"]), (str(self.a.pk), "Europe/Berlin"))
        self.assertEqual([t["id"] for t in day["overdue"]], [str(overdue_a.pk)])
        # "today" is the person's local day (Berlin); 5 minutes from now may cross midnight there.
        berlin = ZoneInfo("Europe/Berlin")
        same_day = later_a.due_at.astimezone(berlin).date() == timezone.now().astimezone(berlin).date()
        self.assertEqual([t["id"] for t in day["today"]], [str(later_a.pk)] if same_day else [])
        self.assertEqual([e["id"] for e in day["needs_next_action"]], [str(idle_a.pk)])
        overdue = run(crm_core.my_overdue(self.client, str(self.rep_a.pk)))
        self.assertEqual([t["id"] for t in overdue["overdue"]], [str(overdue_a.pk)])
        self.assertNotIn("today", overdue)
        every_id = {r["id"] for s in ("overdue", "today", "needs_next_action") for r in day[s]}
        b_ids = {str(pk) for pk in Task.objects.filter(company=self.b).values_list("pk", flat=True)} | \
            {str(pk) for pk in Engagement.objects.filter(company=self.b).values_list("pk", flat=True)}
        self.assertFalse(every_id & b_ids)

    def test_for_user_same_email_in_two_tenants_is_the_envelope(self):
        staff(self.b, "rep-a-in-b", "rep.a@example.com")
        with self.assertRaises(ERPAmbiguousError) as ctx:
            run(crm_core.my_overdue(self.client, "rep.a@example.com"))
        env = ctx.exception.envelope
        self.assertEqual((env["match_quality"], env["item"]), ("ambiguous", None))
        self.assertEqual({c["company"] for c in env["candidates"]}, {str(self.a.pk), str(self.b.pk)})
        # Naming the tenant resolves it.
        result = run(crm_core.my_overdue(self.client, "rep.a@example.com", company=str(self.a.pk)))
        self.assertEqual(result["for_user"]["id"], str(self.rep_a.pk))

    def test_for_user_by_name_is_never_a_guess(self):
        with self.assertRaises(ERPAmbiguousError) as ctx:
            run(crm_core.my_day(self.client, "rep-a"))
        self.assertEqual(ctx.exception.envelope["match_quality"], "fuzzy")
        staff(self.a, "rep-alex", "alex@example.com")
        with self.assertRaises(ERPAmbiguousError) as ctx:
            run(crm_core.my_day(self.client, "rep"))
        self.assertEqual(ctx.exception.envelope["match_quality"], "ambiguous")
        with self.assertRaises(ERPNotFoundError):
            run(crm_core.my_day(self.client, "nobody@example.com"))

    def test_service_accounts_and_non_staff_are_not_people(self):
        from core.service_accounts import ensure_service_account

        svc = ensure_service_account("crm-bot", company=self.a)
        svc.email = "bot@example.com"
        svc.save()
        with self.assertRaises(ERPNotFoundError):
            run(crm_core.my_day(self.client, "bot@example.com"))
        svc_profile = UserProfile.objects.get(user=svc)
        with self.assertRaises(ERPNotFoundError):
            run(crm_core.my_day(self.client, str(svc_profile.pk)))
        plain = User.objects.create_user("customer-ish", "cust@example.com", "pw")
        UserProfile.objects.create(user=plain, company=self.a, display_name="Cust")
        with self.assertRaises(ERPNotFoundError):
            run(crm_core.my_day(self.client, "cust@example.com"))

    def test_for_user_with_another_tenants_company_finds_nobody(self):
        with self.assertRaises(ERPNotFoundError):
            run(crm_core.my_day(self.client, "rep.a@example.com", company=str(self.b.pk)))

    # ---- list / find / get ---------------------------------------------------------------

    def test_list_engagements_stays_in_the_named_tenant(self):
        rows = run(crm_core.list_engagements(self.client, company=str(self.a.pk), limit=None))
        self.assertEqual([r["id"] for r in rows], [str(self.eng_a.pk)])
        rows = run(crm_core.list_engagements(self.client, company=str(self.b.pk), search="solar"))
        self.assertEqual([r["id"] for r in rows], [str(self.eng_b.pk)])

    def test_list_engagements_owner_resolves_and_pins_the_tenant(self):
        rows = run(crm_core.list_engagements(self.client, owner="rep.b@example.com"))
        self.assertEqual([r["id"] for r in rows], [str(self.eng_b.pk)])
        staff(self.a, "rep-b-twin", "rep.b@example.com")
        with self.assertRaises(ERPAmbiguousError):
            run(crm_core.list_engagements(self.client, owner="rep.b@example.com"))
        with self.assertRaises(ERPValidationError):
            run(crm_core.list_engagements(self.client, owner="me"))

    def test_find_engagement_number_exact_title_ambiguous(self):
        env = run(crm_core.find_engagement(self.client, self.eng_a.number))
        self.assertEqual((env["match_quality"], env["item"]["id"]), ("exact", str(self.eng_a.pk)))
        with self.assertRaises(ERPAmbiguousError) as ctx:
            run(crm_core.find_engagement(self.client, "solar array"))
        self.assertEqual(len(ctx.exception.candidates), 2)
        env = run(crm_core.find_engagement(self.client, "solar array", company=str(self.a.pk)))
        self.assertEqual((env["match_quality"], env["item"]["id"]), ("fuzzy", str(self.eng_a.pk)))
        # Fuzzy is not enough to act on.
        with self.assertRaises(ERPAmbiguousError):
            run(crm_core.get_engagement(self.client, "solar array", company=str(self.a.pk)))

    def test_get_engagement_never_crosses_tenants(self):
        with self.assertRaises(ERPNotFoundError):
            run(crm_core.get_engagement(self.client, str(self.eng_b.pk), company=str(self.a.pk)))
        with self.assertRaises(ERPNotFoundError):
            run(crm_core.get_engagement(self.client, self.eng_b.number, company=str(self.a.pk)))

    def test_get_engagement_thread_summary(self):
        due = timezone.now() + datetime.timedelta(days=1)
        nxt = self.task(self.eng_a, self.rep_a, due, "Send revised quote")
        ingest.record(self.a, "call", direction="outbound", occurred_at=timezone.now(),
                      provider=ingest.MANUAL, subject="Intro call", summary="Wants 20 kW",
                      engagement=self.eng_a, party=self.ada, body_text="SECRET BODY")
        gone = person(self.a, "Erased", "Person", email="gone@example.com")
        EngagementContact.objects.create(engagement=self.eng_a, company=self.a, party=gone, role="billing")
        Party.objects.filter(pk=gone.pk).update(erased_at=timezone.now())
        summary = run(crm_core.get_engagement(self.client, self.eng_a.number))
        self.assertEqual(summary["engagement"]["id"], str(self.eng_a.pk))
        self.assertEqual(summary["engagement"]["company"], str(self.a.pk))
        self.assertEqual(summary["next_action"]["id"], str(nxt.pk))
        self.assertFalse(summary["needs_next_action"])
        self.assertEqual(summary["last_communication"]["subject"], "Intro call")
        self.assertEqual([t["id"] for t in summary["open_tasks"]], [str(nxt.pk)])
        self.assertEqual([c["party"] for c in summary["contacts"]], [str(self.ada.pk)])
        self.assertTrue(summary["contacts"][0]["can_contact"]["transactional"]["allowed"])
        self.assertNotIn("SECRET BODY", str(summary))
        self.assertNotIn("gone@example.com", str(summary))

    # ---- draft_follow_up: writes nothing ---------------------------------------------------

    def test_draft_follow_up_writes_nothing(self):
        ingest.record(self.a, "email", direction="inbound", occurred_at=timezone.now(),
                      provider=ingest.MANUAL, subject="Panel options", engagement=self.eng_a,
                      party=self.ada)
        before = row_counts()
        draft = run(crm_core.draft_follow_up(self.client, self.eng_a.number, intent="Share the revised quote."))
        self.assertEqual(row_counts(), before)
        self.assertEqual((draft["proposal"], draft["sent"], draft["written"], draft["refused"]),
                         (True, False, False, None))
        self.assertEqual(draft["to"]["address"], "ada@example.com")
        self.assertEqual(draft["to"]["party"], str(self.ada.pk))
        self.assertEqual(draft["subject"], "Re: Panel options")
        self.assertIn("Hi Ada", draft["body"])
        self.assertIn("Share the revised quote.", draft["body"])
        self.assertEqual(draft["suggested_task"]["due_tz"], "Europe/Berlin")
        self.assertEqual([s["step"] for s in draft["apply"]], ["log_communication", "set_next_action"])
        self.assertEqual(draft["apply"][0]["body"]["company"], str(self.a.pk))
        sms = run(crm_core.draft_follow_up(self.client, str(self.eng_a.pk), channel="sms"))
        self.assertEqual(sms["to"]["address"], "+14155550101")
        self.assertEqual(row_counts(), before)

    def test_draft_follow_up_refuses_a_suppressed_address(self):
        Suppression.objects.create(
            company=self.a, channel="email", reason="opt_out", source="erp",
            address_hash=address_hash(normalize_address("email", "ada@example.com")))
        before = row_counts()
        draft = run(crm_core.draft_follow_up(self.client, self.eng_a.number))
        self.assertEqual(row_counts(), before)
        self.assertEqual(draft["refused"], "suppressed")
        self.assertIsNone(draft["to"]["address"])
        self.assertNotIn("ada@example.com", str(draft))
        self.assertNotIn("body", draft)
        self.assertTrue(draft["consent"]["suppressed"])
        # Another tenant's suppression of the same address does not leak across.
        draft_b = run(crm_core.draft_follow_up(self.client, self.eng_b.number))
        self.assertIsNone(draft_b["refused"])

    def test_draft_follow_up_never_includes_an_erased_party(self):
        Party.objects.filter(pk=self.ada.pk).update(erased_at=timezone.now())
        before = row_counts()
        draft = run(crm_core.draft_follow_up(self.client, self.eng_a.number))
        self.assertEqual(draft["refused"], "no_recipient")
        self.assertNotIn("ada@example.com", str(draft))
        # With a live contact, that contact is the recipient instead.
        grace = person(self.a, "Grace", "Hopper", email="grace@example.com")
        EngagementContact.objects.create(engagement=self.eng_a, company=self.a, party=grace,
                                         role="champion", is_primary=True)
        before = row_counts()
        draft = run(crm_core.draft_follow_up(self.client, self.eng_a.number))
        self.assertEqual(row_counts(), before)
        self.assertEqual(draft["to"]["address"], "grace@example.com")
        self.assertNotIn("ada@example.com", str(draft))

    def test_erased_partys_messages_never_reach_a_draft_or_the_summary(self):
        gone = person(self.a, "Gone", "Person", email="gone@example.com")
        ingest.record(self.a, "email", direction="inbound", occurred_at=timezone.now(),
                      provider=ingest.MANUAL, subject="Private matter", summary="Erased text",
                      engagement=self.eng_a, party=gone)
        Party.objects.filter(pk=gone.pk).update(erased_at=timezone.now())
        draft = run(crm_core.draft_follow_up(self.client, self.eng_a.number))
        self.assertEqual(draft["to"]["address"], "ada@example.com")
        self.assertNotIn("Private matter", str(draft))
        self.assertNotIn("Erased text", str(draft))
        summary = run(crm_core.get_engagement(self.client, self.eng_a.number))
        self.assertNotIn("Private matter", str(summary))
        self.assertNotIn("Erased text", str(summary))

    def test_re_subject_only_on_the_recipients_own_thread(self):
        grace = person(self.a, "Grace", "Hopper", email="grace@example.com")
        ingest.record(self.a, "email", direction="inbound", occurred_at=timezone.now(),
                      provider=ingest.MANUAL, subject="Grace's thread", engagement=self.eng_a, party=grace)
        draft = run(crm_core.draft_follow_up(self.client, self.eng_a.number))
        self.assertEqual(draft["to"]["party"], str(self.ada.pk))
        self.assertEqual(draft["subject"], "Following up")

    def test_sms_needs_a_mobile_number(self):
        PartyPhone.objects.filter(party=self.ada).update(line_type="landline")
        draft = run(crm_core.draft_follow_up(self.client, self.eng_a.number, channel="sms"))
        self.assertEqual(draft["refused"], "no_address")

    def test_company_id_in_any_uuid_spelling(self):
        upper = str(self.a.pk).upper()
        rows = run(crm_core.list_engagements(self.client, company=upper, owner="rep.a@example.com"))
        self.assertEqual([r["id"] for r in rows], [str(self.eng_a.pk)])

    def test_draft_follow_up_other_tenant_is_not_found(self):
        with self.assertRaises(ERPNotFoundError):
            run(crm_core.draft_follow_up(self.client, str(self.eng_b.pk), company=str(self.a.pk)))
        with self.assertRaises(ERPValidationError):
            run(crm_core.draft_follow_up(self.client, str(self.eng_a.pk), channel="fax"))

    # ---- write tools ------------------------------------------------------------------------

    def test_create_task_ambiguous_assignee_returns_envelope_and_writes_nothing(self):
        staff(self.a, "sam-one", "sam1@example.com")
        staff(self.a, "sam-two", "sam2@example.com")
        before = Task.objects.count()
        with self.assertRaises(ERPAmbiguousError) as ctx:
            run(crm_core.create_task(self.client, title="Call", engagement=self.eng_a.number,
                                     assignee="sam"))
        self.assertEqual(ctx.exception.envelope["match_quality"], "ambiguous")
        self.assertEqual(Task.objects.count(), before)

    def test_create_task_through_the_service(self):
        result = run(crm_core.create_task(self.client, title="Send datasheet", engagement=self.eng_a.number,
                                          assignee="rep.a@example.com", due="2030-01-15", kind="follow_up"))
        task = Task.objects.get(pk=result["id"])
        self.assertEqual((task.company_id, task.assignee_id, task.engagement_id, task.source),
                         (self.a.pk, self.rep_a.pk, self.eng_a.pk, "api"))
        self.assertEqual(task.due_tz, "Europe/Berlin")
        self.assertTrue(TaskEvent.objects.filter(task=task, event="created").exists())
        self.eng_a.refresh_from_db()
        self.assertEqual(self.eng_a.next_task_id, task.pk)

    def test_create_task_refuses_an_assignee_of_another_tenant(self):
        before = Task.objects.count()
        with self.assertRaises(ERPNotFoundError):
            run(crm_core.create_task(self.client, title="x", engagement=self.eng_a.number,
                                     assignee="rep.b@example.com"))
        self.assertEqual(Task.objects.count(), before)

    def test_move_stage_requires_confirm(self):
        before = EngagementStageChange.objects.count()
        preview = run(crm_core.move_stage(self.client, engagement=self.eng_a.number, stage="negotiation"))
        self.assertEqual((preview["error"], preview["written"]), ("ConfirmationRequired", False))
        self.assertEqual(preview["preview"]["to_stage"], "negotiation")
        self.assertEqual(EngagementStageChange.objects.count(), before)
        result = run(crm_core.move_stage(self.client, engagement=self.eng_a.number, stage="negotiation",
                                         note="Asked for terms", confirm=True))
        self.assertTrue(result["written"])
        self.eng_a.refresh_from_db()
        self.assertEqual((self.eng_a.stage.key, self.eng_a.status), ("negotiation", "open"))
        self.assertEqual(EngagementStageChange.objects.count(), before + 1)

    def test_move_stage_refuses_another_tenants_stage(self):
        other_stage = Pipeline.objects.get(company=self.b, key="sales").stages.get(key="negotiation")
        before = EngagementStageChange.objects.count()
        with self.assertRaises(ERPNotFoundError):
            run(crm_core.move_stage(self.client, engagement=self.eng_a.number, stage=str(other_stage.pk),
                                    confirm=True))
        self.assertEqual(EngagementStageChange.objects.count(), before)
        self.eng_a.refresh_from_db()
        self.assertNotEqual(self.eng_a.stage.key, "negotiation")

    def test_move_stage_ambiguous_engagement_writes_nothing(self):
        engagement_service.create(company=self.a, party=self.ada, title="Ada — solar array phase 2")
        before = EngagementStageChange.objects.count()
        with self.assertRaises(ERPAmbiguousError):
            run(crm_core.move_stage(self.client, engagement="solar array", stage="negotiation",
                                    company=str(self.a.pk), confirm=True))
        self.assertEqual(EngagementStageChange.objects.count(), before)


class McpCrmToolTests(SimpleTestCase):
    def test_crm_tools_registered(self):
        names = {tool.name for tool in run(mcp_adapter.mcp.list_tools())}
        self.assertTrue({"my_overdue", "my_day", "list_engagements", "find_engagement", "get_engagement",
                         "draft_follow_up", "create_task", "move_stage", "find_staff"} <= names)

    def test_ambiguous_for_user_rendered_as_the_envelope(self):
        envelope = {"match_quality": "ambiguous", "item": None, "message": "Two staff.",
                    "candidates": [{"id": "1", "display_name": "Sam"}, {"id": "2", "display_name": "Sam"}]}

        async def boom(client, **kwargs):
            raise ERPAmbiguousError(envelope)

        with mock.patch.object(mcp_adapter, "_get_client", new=mock.AsyncMock(return_value=object())), \
                mock.patch.object(crm_core, "my_overdue", new=boom):
            result = run(mcp_adapter.my_overdue(for_user="sam"))
        self.assertEqual((result["error"], result["match_quality"], result["item"]),
                         ("ERPAmbiguousError", "ambiguous", None))

    def test_tool_docstrings_use_canonical_nouns(self):
        """Machine-facing (spec 3.4): no tenant label appears; the tool text names engagements."""
        tools = {t.name: t for t in run(mcp_adapter.mcp.list_tools())}
        for name in ("get_engagement", "list_engagements", "draft_follow_up", "move_stage"):
            self.assertIn("engagement", tools[name].description.lower())
        self.assertIn("WRITES NOTHING", tools["draft_follow_up"].description)
        self.assertIn("confirm=True", tools["move_stage"].description)
