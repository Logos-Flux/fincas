"""CRM Phase 3 step 10: the API the assistant's tools stand on (spec 5.3).

``staff/`` (the staff directory envelope), ``queues/<profile>/`` (``me/``
for a named person), ``engagements/<id>/follow-up-facts/`` and the
``company=`` narrowing. The backend, not the assistant, enforces the record
rules: a tenant user never sees another tenant's people, queues or facts, and
the facts endpoint writes nothing.
"""

from __future__ import annotations

import datetime

from django.utils import timezone
from rest_framework.test import APIClient

from core.authentication import issue_api_jwt
from core.models import UserProfile
from crm import tasks
from crm.models import Engagement, EngagementStageChange, Task, TaskEvent
from parties.consent import address_hash, normalize_address
from parties.models import Party, PartyEmail, Suppression

from .test_api import CRM, _Api, staff_client

STAFF, QUEUES = f"{CRM}staff/", f"{CRM}queues/"


class StaffDirectoryTests(_Api):
    def test_service_key_sees_every_tenant_so_a_shared_email_is_ambiguous(self):
        self.me.user.email = "same@example.com"
        self.me.user.save()
        self.other_me.user.email = "SAME@example.com"
        self.other_me.user.save()
        env = self.ok(self.service.get(STAFF, {"q": "same@example.com"}))
        self.assertEqual((env["match_quality"], env["item"]), ("ambiguous", None))
        self.assertEqual(len(env["candidates"]), 2)
        env = self.ok(self.service.get(STAFF, {"q": "same@example.com", "company": str(self.company.pk)}))
        self.assertEqual((env["match_quality"], env["item"]["id"]), ("exact", str(self.me.pk)))

    def test_tenant_user_sees_only_its_own_staff(self):
        self.other_me.user.email = "rival@example.com"
        self.other_me.user.save()
        env = self.ok(self.client.get(STAFF, {"q": "rival@example.com"}))
        self.assertEqual(env["match_quality"], "none")
        env = self.ok(self.client.get(STAFF, {"q": str(self.other_me.pk)}))
        self.assertEqual(env["match_quality"], "none")
        response = self.client.get(STAFF, {"q": "x", "company": str(self.other_company.pk)})
        self.assertEqual(response.status_code, 400)

    def test_a_name_is_never_exact_and_non_people_are_left_out(self):
        env = self.ok(self.service.get(STAFF, {"q": "staffer"}))
        self.assertEqual(env["match_quality"], "fuzzy")
        # The fixture's ``rep`` has no staff role: not in the directory.
        env = self.ok(self.service.get(STAFF, {"q": str(self.rep.pk)}))
        self.assertEqual(env["match_quality"], "none")

    def test_storefront_and_anonymous_refused(self):
        storefront = APIClient(HTTP_AUTHORIZATION=f"Bearer {issue_api_jwt(sub='storefront')}")
        for url in (STAFF, f"{QUEUES}{self.me.pk}/"):
            with self.subTest(url=url):
                self.assertEqual(storefront.get(url).status_code, 403)
                self.assertEqual(APIClient().get(url).status_code, 401)


class StaffQueueTests(_Api):
    def test_named_queue_matches_me_for_that_person(self):
        eng = self.eng(owner=self.me)
        tasks.create(engagement=eng, assignee=self.me, title="late",
                     due=timezone.now() - datetime.timedelta(hours=1))
        mine = self.ok(self.client.get(f"{CRM}me/"))
        named = self.ok(self.service.get(f"{QUEUES}{self.me.pk}/"))
        self.assertEqual(named, mine)
        self.assertEqual(len(named["overdue"]), 1)

    def test_another_tenants_person_is_404(self):
        self.assertEqual(self.client.get(f"{QUEUES}{self.other_me.pk}/").status_code, 404)
        self.assertEqual(self.service.get(f"{QUEUES}{self.other_me.pk}/",
                                          {"company": str(self.company.pk)}).status_code, 404)
        self.assertEqual(self.service.get(f"{QUEUES}{self.other_me.pk}/").status_code, 200)
        self.assertEqual(self.service.get(f"{QUEUES}not-a-uuid/").status_code, 404)

    def test_a_colleague_in_the_same_tenant_is_readable(self):
        _client, colleague = staff_client(self.company, "colleague")
        self.assertEqual(self.client.get(f"{QUEUES}{colleague.pk}/").status_code, 200)

    def test_service_account_profile_is_not_a_person(self):
        from core.service_accounts import ensure_service_account

        svc = ensure_service_account("crm-queue-bot", company=self.company)
        profile = UserProfile.objects.get(user=svc)
        self.assertEqual(self.service.get(f"{QUEUES}{profile.pk}/").status_code, 404)


class CompanyFilterTests(_Api):
    def test_company_narrows_a_service_read_to_one_tenant(self):
        mine, theirs = self.eng(), self.eng(company=self.other_company,
                                            party=Party.objects.create(
                                                company=self.other_company, kind="organisation",
                                                display_name="Rival", origin="erp"))
        ids = [r["id"] for r in self.rows(self.service.get(f"{CRM}engagements/",
                                                           {"company": str(self.company.pk)}))]
        self.assertEqual(ids, [str(mine.pk)])
        self.assertEqual(self.service.get(f"{CRM}engagements/{theirs.pk}/",
                                          {"company": str(self.company.pk)}).status_code, 404)
        self.assertEqual(self.rows(self.service.get(f"{CRM}tasks/", {"company": "nope"})), [])
        # A tenant user naming another tenant gets nothing, never that tenant's rows.
        self.assertEqual(self.rows(self.client.get(f"{CRM}engagements/",
                                                   {"company": str(self.other_company.pk)})), [])


class FollowUpFactsTests(_Api):
    def url(self, eng, **params):
        return f"{CRM}engagements/{eng.pk}/follow-up-facts/"

    def counts(self):
        return [m._base_manager.count() for m in (Engagement, EngagementStageChange, Task, TaskEvent)]

    def test_facts_write_nothing_and_name_the_contact_point(self):
        PartyEmail.objects.create(party=self.ada, email="ada@example.com", purpose="work", is_primary=True)
        eng = self.eng(party=self.ada)
        before = self.counts()
        facts = self.ok(self.client.get(self.url(eng)))
        self.assertEqual(self.counts(), before)
        self.assertIsNone(facts["refusal"])
        self.assertEqual(facts["to"]["address"], "ada@example.com")
        self.assertEqual(facts["consent"]["reason"], "no_consent")  # reported, not a refusal

    def test_suppressed_address_refuses_and_is_withheld(self):
        PartyEmail.objects.create(party=self.ada, email="ada@example.com", purpose="work", is_primary=True)
        Suppression.objects.create(company=self.company, channel="email", reason="complaint", source="erp",
                                   address_hash=address_hash(normalize_address("email", "ada@example.com")))
        facts = self.ok(self.client.get(self.url(self.eng(party=self.ada))))
        self.assertEqual(facts["refusal"], "suppressed")
        self.assertIsNone(facts["to"]["address"])
        self.assertNotIn("ada@example.com", str(facts))

    def test_other_tenant_is_404_and_bad_channel_400(self):
        eng = self.eng()
        self.assertEqual(self.other_client.get(self.url(eng)).status_code, 404)
        self.assertEqual(self.client.get(self.url(eng), {"channel": "fax"}).status_code, 400)
