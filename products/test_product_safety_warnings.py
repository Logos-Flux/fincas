"""Product.safety_warnings: accepted safety warnings (Prop 65 and similar).

A JSON list, NOT NULL, default ``[]``; each entry carries its evidence. It is
never a certification. Exposed on the product detail API, the ERP item
profile and the Django admin.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from products.erp_views import PARENT_SECTIONS
from products.forms import ProductForm
from products.models import Product
from products.serializers import ProductDetailSerializer

WARNING = {
    "type": "prop65",
    "text": "WARNING: This product can expose you to lead.",
    "evidence_url": "https://example.com/spec-sheet.pdf",
    "reviewed_by": "reviewer",
    "reviewed_at": "2026-10-01T12:00:00Z",
}


class SafetyWarningsFieldTests(TestCase):
    def test_defaults_to_empty_list(self):
        product = Product.objects.create(title="Inverter")
        product.refresh_from_db()
        self.assertEqual(product.safety_warnings, [])

    def test_round_trips_a_warning_list(self):
        product = Product.objects.create(title="Inverter", safety_warnings=[WARNING])
        product.refresh_from_db()
        self.assertEqual(product.safety_warnings, [WARNING])

    def test_default_is_a_fresh_list_per_instance(self):
        a, b = Product(title="A"), Product(title="B")
        a.safety_warnings.append(WARNING)
        self.assertEqual(b.safety_warnings, [])


class SafetyWarningsExposureTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(title="Inverter", safety_warnings=[WARNING])

    def test_detail_serializer_exposes_it(self):
        self.assertEqual(ProductDetailSerializer(self.product).data["safety_warnings"], [WARNING])

    def test_product_form_and_profile_section_list_it(self):
        self.assertIn("safety_warnings", ProductForm().fields)
        listed = {f for section in PARENT_SECTIONS for f in section["fields"]}
        self.assertIn("safety_warnings", listed)

    def test_profile_page_renders_it(self):
        user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        self.client.force_login(user)
        html = self.client.get(reverse("erp-product-detail", kwargs={"pk": self.product.pk})).content.decode()
        self.assertIn('name="safety_warnings"', html)
        self.assertIn("This product can expose you to lead.", html)

    def test_admin_change_form_exposes_it(self):
        admin_user = User.objects.create_superuser("admin", "admin@example.com", "pw")
        self.client.force_login(admin_user)
        html = self.client.get(
            reverse("admin:products_product_change", args=[self.product.pk])
        ).content.decode()
        self.assertIn('name="safety_warnings"', html)

    def _form_data(self, **overrides):
        form = ProductForm(instance=self.product)
        data = {name: form.initial.get(name) for name in form.fields}
        data = {k: ("" if v is None else v) for k, v in data.items()}
        for name, field in form.fields.items():
            if field.__class__.__name__ == "JSONField":
                data[name] = json.dumps(form.initial.get(name))
        data.update(overrides)
        return data

    def test_emptied_json_boxes_save_as_their_defaults_not_null(self):
        # An emptied textarea binds as None; the columns are NOT NULL.
        data = self._form_data(safety_warnings="", specifications="", seo_faqs="")
        bound = ProductForm(data=data, instance=self.product)
        self.assertTrue(bound.is_valid(), bound.errors)
        saved = bound.save()
        saved.refresh_from_db()
        self.assertEqual(saved.safety_warnings, [])
        self.assertEqual(saved.specifications, Product._meta.get_field("specifications").get_default())
        self.assertEqual(saved.seo_faqs, Product._meta.get_field("seo_faqs").get_default())

    def test_form_rejects_a_non_list(self):
        for bad in ('"just text"', "7", '{"type": "prop65"}', '["text"]'):
            bound = ProductForm(data=self._form_data(safety_warnings=bad), instance=self.product)
            self.assertFalse(bound.is_valid(), bad)
            self.assertIn("safety_warnings", bound.errors)

    def test_api_rejects_a_non_list(self):
        from rest_framework.test import APIClient

        from core.models import Company, UserProfile

        company = Company.objects.create(name="Warnings Co")
        self.product.company = company
        self.product.save(update_fields=["company"])
        user = User.objects.create_user(username="api", password="pw", is_staff=True)
        UserProfile.objects.create(company=company, user=user)
        client = APIClient()
        client.force_authenticate(user)
        url = f"/api/v1/products/products/{self.product.pk}/"
        for bad in ("text", 7, {"type": "prop65"}, ["text"]):
            resp = client.patch(url, {"safety_warnings": bad}, format="json")
            self.assertEqual(resp.status_code, 400, (bad, resp.content))
        resp = client.patch(url, {"safety_warnings": []}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.product.refresh_from_db()
        self.assertEqual(self.product.safety_warnings, [])

    def test_form_parses_json_and_keeps_a_list(self):
        data = self._form_data(safety_warnings=json.dumps([WARNING, {**WARNING, "type": "other"}]))
        bound = ProductForm(data=data, instance=self.product)
        self.assertTrue(bound.is_valid(), bound.errors)
        saved = bound.save()
        saved.refresh_from_db()
        self.assertEqual([w["type"] for w in saved.safety_warnings], ["prop65", "other"])
