"""Back-office create screens: New task (task list) and New lead (lead list).

Two tenants throughout: staff never see, pick or create into the other
tenant. With two Companies a staff login does not auto-attach, so every
user here has an explicit ``UserProfile(company=...)`` (``CrmViewTestBase``).
The party picker on the task form is ``test_party_picker``'s.
"""

from __future__ import annotations

from django.urls import reverse

from crm import engagements
from crm.models import Engagement, Lead, Task
from parties.models import Party

from .helpers import organisation
from .test_erp_views import CrmViewTestBase
from .test_party_picker import email

# --- New task ----------------------------------------------------------------------


class NewTaskTests(CrmViewTestBase):
    url = reverse("erp-crm-task-create")

    def post(self, **data):
        data.setdefault("title", "Send the brochure")
        data.setdefault("kind", "follow_up")
        data.setdefault("priority", "normal")
        return self.client.post(self.url, data)

    def test_the_task_list_links_to_the_create_screen(self):
        response = self.client.get(reverse("erp-crm-task-list"))
        self.assertContains(response, self.url + "?next=")

    def test_renders_with_my_open_engagements_only(self):
        mine = self.eng(title="Roof job")
        closed = self.eng(title="Old job")
        engagements.lose(closed, "price", by=self.me)
        theirs = self.other_eng()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        choices = {str(value) for value, _label in response.context["form"].fields["engagement"].choices}
        self.assertIn(str(mine.pk), choices)
        self.assertNotIn(str(closed.pk), choices)
        self.assertNotIn(str(theirs.pk), choices)
        self.assertEqual(response.context["form"].initial["assignee"], self.me.pk)
        self.assertContains(response, 'data-party-search="%s"' % reverse("erp-party-search"))
        self.assertContains(response, 'target="_blank" rel="noopener"')

    def test_a_company_only_task(self):
        response = self.post(assignee=self.me.pk, due_date="2030-01-02")
        self.assertRedirects(response, reverse("erp-crm-task-list"), fetch_redirect_response=False)
        task = Task.objects.get(title="Send the brochure")
        self.assertEqual((task.company_id, task.engagement_id, task.party_id), (self.company.pk, None, None))
        self.assertEqual((task.assignee, task.created_by, task.source), (self.me, self.me, "erp"))
        self.assertTrue(task.events.filter(event="created").exists())
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertIn("Task added: Send the brochure.", messages)

    def test_on_an_engagement_takes_its_counterparty(self):
        eng = self.eng()
        self.post(engagement=eng.pk)
        task = Task.objects.get(title="Send the brochure")
        self.assertEqual((task.engagement, task.party), (eng, self.org))
        eng.refresh_from_db()
        self.assertEqual(eng.next_task, task)

    def test_a_party_by_exact_email_or_by_id(self):
        email(self.ada, "ada@example.com")
        self.post(party="ADA@example.com")
        self.assertEqual(Task.objects.get(title="Send the brochure").party, self.ada)
        self.post(title="Second", party=str(self.org.pk))
        self.assertEqual(Task.objects.get(title="Second").party, self.org)

    def test_back_to_the_filtered_list(self):
        back = reverse("erp-crm-task-list") + "?assignee=all"
        response = self.post(next=back)
        self.assertRedirects(response, back, fetch_redirect_response=False)

    def test_prefills_an_engagement_of_my_tenant_only(self):
        eng = self.eng()
        form = self.client.get(self.url, {"engagement": eng.pk}).context["form"]
        self.assertEqual(form.initial["engagement"], eng.pk)
        form = self.client.get(self.url, {"engagement": self.other_eng().pk}).context["form"]
        self.assertIsNone(form.initial["engagement"])

    def test_another_tenants_engagement_or_party_is_refused(self):
        response = self.post(engagement=self.other_eng().pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("engagement", response.context["form"].errors)
        rival = organisation(self.other_company, "Rival Party")
        response = self.post(party=str(rival.pk))
        self.assertEqual(response.status_code, 400)
        self.assertIn("party", response.context["form"].errors)
        self.assertFalse(Task.objects.filter(title="Send the brochure").exists())

    def test_the_other_tenant_creates_into_its_own(self):
        self.client.force_login(self.other_user)
        self.post()
        self.assertEqual(Task.objects.get(title="Send the brochure").company, self.other_company)

    def test_invalid_input_re_renders(self):
        response = self.post(title="", due_time="10:00")
        self.assertEqual(response.status_code, 400)
        self.assertIn("title", response.context["form"].errors)
        self.assertIn("due_date", response.context["form"].errors)


# --- New lead ----------------------------------------------------------------------


class NewLeadTests(CrmViewTestBase):
    url = reverse("erp-crm-lead-create")

    def post(self, **data):
        data.setdefault("first_name", "Grace")
        data.setdefault("last_name", "Hopper")
        data.setdefault("email", "grace@navy.example")
        data.setdefault("source", "phone")
        data.setdefault("request_type", "quote")
        data.setdefault("country", "us")
        return self.client.post(self.url, data)

    def test_the_lead_list_links_to_the_create_screen(self):
        self.assertContains(self.client.get(reverse("erp-crm-lead-list")), self.url)

    def test_renders(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["source"].value(), "phone")
        self.assertContains(response, "Saving runs intake")

    def test_intake_opens_an_engagement_and_lands_there(self):
        response = self.post(message="Need 40 panels", assigned_to=self.colleague.pk, estimated_value="900")
        lead = Lead.objects.get(email="grace@navy.example")
        self.assertEqual((lead.company, lead.country, lead.first_name), (self.company, "US", "Grace"))
        self.assertIsNotNone(lead.party)
        eng = lead.engagement
        self.assertIsNotNone(eng)
        self.assertRedirects(response, reverse("erp-crm-engagement-detail", args=[eng.pk]) + "#next-action",
                             fetch_redirect_response=False)
        self.assertEqual((eng.owner, eng.party, eng.company), (self.colleague, lead.party, self.company))
        self.assertTrue(Task.objects.filter(engagement=eng, title=f"Respond to inquiry {lead.lead_number}",
                                            status="open").exists())
        self.assertEqual(lead.party.kind, Party.Kind.PERSON)

    def test_a_support_request_opens_nothing(self):
        response = self.post(request_type="support", message="My inverter beeps")
        self.assertRedirects(response, reverse("erp-crm-lead-list"), fetch_redirect_response=False)
        lead = Lead.objects.get(email="grace@navy.example")
        self.assertIsNone(lead.engagement_id)
        self.assertFalse(Engagement.objects.filter(company=self.company, party=lead.party).exists())

    def test_invalid_input_re_renders(self):
        response = self.post(email="not-an-email", country="USA")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.context["form"].errors), {"email", "country"})
        self.assertFalse(Lead.objects.exists())

    def test_tenancy(self):
        # Another tenant's person is not an owner choice here...
        response = self.post(assigned_to=self.other_me.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("assigned_to", response.context["form"].errors)
        # ...and the other tenant's staff create into their own company.
        self.client.force_login(self.other_user)
        self.post()
        lead = Lead.objects.get(email="grace@navy.example")
        self.assertEqual((lead.company, lead.engagement.company), (self.other_company, self.other_company))


class NoCompanyTests(CrmViewTestBase):
    def test_staff_without_a_company_get_403_and_create_nothing(self):
        from core.tests.helpers import make_staff_user

        nobody = make_staff_user("nobody", password="pw")  # two Companies: no auto-attach
        self.client.force_login(nobody)
        for url in (reverse("erp-crm-task-create"), reverse("erp-crm-lead-create")):
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, {"title": "Orphan", "kind": "follow_up",
                                                    "priority": "normal", "first_name": "O",
                                                    "last_name": "Rphan", "email": "o@example.com",
                                                    "source": "phone", "country": "US"}).status_code, 403)
        self.assertFalse(Task.objects.filter(title="Orphan").exists())
        self.assertFalse(Lead.objects.filter(email="o@example.com").exists())
