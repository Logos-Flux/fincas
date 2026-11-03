"""Lead numbering (release harness R8.01, ``R8-lead-number-collision``).

``Lead.save`` mints ``LD-<n>`` as one past the highest ``LD-`` number stored,
never from the most recently created lead: a lead that arrived with its own
number (the API, an import) is not the sequence. Found 2026-10-17: after an
API lead numbered ``L-R8-…`` the staff lead form minted ``LD-00001`` again
and answered 500.
"""

from __future__ import annotations

from django.db import IntegrityError
from django.urls import reverse

from crm.models import Lead

from .test_erp_views import CrmViewTestBase


def _lead(company, email, **kwargs):
    kwargs.setdefault("first_name", "Ada")
    kwargs.setdefault("last_name", "Lovelace")
    kwargs.setdefault("request_type", Lead.RequestType.QUOTE)
    return Lead.objects.create(company=company, email=email, **kwargs)


class LeadNumberTests(CrmViewTestBase):
    def test_a_foreign_number_does_not_reset_the_sequence(self):
        first = _lead(self.company, "one@example.com")
        self.assertEqual(first.lead_number, "LD-00001")
        api = _lead(self.company, "api@example.com", lead_number="X-1")
        self.assertEqual(api.lead_number, "X-1")  # a caller's number is kept as given
        after = _lead(self.company, "two@example.com")
        self.assertEqual(after.lead_number, "LD-00002")

    def test_only_ld_numbers_count_and_width_grows(self):
        _lead(self.company, "big@example.com", lead_number="LD-123456")
        _lead(self.company, "look@example.com", lead_number="LD-999999x")  # not a sequence number
        self.assertEqual(Lead.next_lead_number(), "LD-123457")
        self.assertEqual(_lead(self.company, "next@example.com").lead_number, "LD-123457")

    def test_a_collision_on_a_minted_number_is_retried(self):
        _lead(self.company, "one@example.com")
        taken = iter(["LD-00001", "LD-00002"])  # the first mint races a lead saved in between
        original = Lead.next_lead_number

        def racing():
            try:
                return next(taken)
            except StopIteration:
                return original()

        Lead.next_lead_number = staticmethod(racing)
        try:
            lead = _lead(self.company, "race@example.com")
        finally:
            Lead.next_lead_number = original
        self.assertEqual(lead.lead_number, "LD-00002")
        self.assertEqual(Lead.objects.filter(email="race@example.com").count(), 1)

    def test_a_caller_supplied_duplicate_is_not_rewritten(self):
        _lead(self.company, "one@example.com", lead_number="X-1")
        with self.assertRaises(IntegrityError):
            _lead(self.company, "two@example.com", lead_number="X-1")

    def test_the_lead_form_numbers_after_an_api_lead(self):
        """R8.01 as the harness drives it: an API lead with its own number, then the form."""
        _lead(self.company, "first@example.com")
        _lead(self.company, "api@example.com", lead_number="L-R8-ABC", source=Lead.Source.WEBSITE_FORM)
        response = self.client.post(reverse("erp-crm-lead-create"), {
            "first_name": "Grace", "last_name": "Hopper", "email": "grace@navy.example",
            "source": "phone", "request_type": "quote", "country": "us",
        })
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context.get("form"))
        lead = Lead.objects.get(email="grace@navy.example")
        self.assertEqual(lead.lead_number, "LD-00002")
        self.assertIsNotNone(lead.engagement_id)
