"""An erased customer shows a label to staff, never ``erased+<uuid>@invalid``
(release harness R8.08, ``R8-raw-tombstone-shown``; docs/release-hardening-plan.md
design block). The API and exports keep the stored value: it is data there.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from customers.models import Customer
from parties.erasure import ERASED_NAME, tombstone_email


class ErasedCustomerDisplayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.staff = make_staff_user("erased-viewer", password="pw")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"company": cls.company})
        cls.customer = Customer.objects.create(
            first_name="Dana", last_name="Whitfield", email="dana@example.com", company=cls.company,
        )
        # Exactly what parties.erasure writes (a QuerySet.update, no sync).
        Customer._base_manager.filter(pk=cls.customer.pk).update(
            first_name=ERASED_NAME, last_name="", email=tombstone_email(cls.customer.pk), phone="",
        )
        cls.customer.refresh_from_db()
        cls.tombstone = tombstone_email(cls.customer.pk)

    def setUp(self):
        self.client.force_login(self.staff)

    def test_model_display(self):
        self.assertTrue(self.customer.is_erased)
        self.assertEqual(self.customer.full_name, "Erased customer")
        self.assertEqual(str(self.customer), "Erased customer")
        self.assertEqual(self.customer.display_email, "")
        self.assertEqual(self.customer.email, self.tombstone)  # the stored value is untouched

    def test_customers_list_shows_the_label_not_the_tombstone(self):
        response = self.client.get(reverse("erp-customer-list") + "?q=Erased")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Erased customer")
        self.assertNotContains(response, self.tombstone)
        self.assertNotContains(response, str(self.customer.pk).replace("-", "")[:8] + "@")

    def test_customer_page_shows_the_label_not_the_tombstone(self):
        response = self.client.get(reverse("erp-customer-detail", args=[self.customer.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Erased customer")
        self.assertContains(response, 'data-testid="erased-customer"')
        self.assertNotContains(response, self.tombstone)

    def test_csv_export_keeps_the_stored_value_out_of_the_name_but_prints_no_label_layer(self):
        response = self.client.get(reverse("erp-customer-list") + "?export=csv")
        body = b"".join(response.streaming_content) if response.streaming else response.content
        self.assertIn(b"Erased customer", body)
        self.assertNotIn(b"erased+", body)  # display_email is blank for an erased row

    def test_api_keeps_the_tombstone(self):
        """Machine-facing output is raw: the serializer reads ``email`` itself."""
        from customers.serializers import CustomerDetailSerializer as CustomerSerializer

        data = CustomerSerializer(self.customer).data
        self.assertEqual(data["email"], self.tombstone)
