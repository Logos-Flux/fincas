"""ProductVariant.seo_title / seo_meta_description: per-variant SEO overrides.

Blank means "use the parent product's", through the same cascade as every
other variant override (``get_effective_field``). The variant cards must
render both fields: ``ProductVariantForm`` uses ``exclude``, so a form field
missing from ``VARIANT_SECTIONS`` would be posted empty and wipe the value on
every product save.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from products.erp_views import VARIANT_SECTIONS
from products.forms import ProductVariantForm
from products.models import Product, ProductVariant
from products.serializers import ProductVariantDetailSerializer


class VariantSeoFieldTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(
            title="Hybrid Inverter", has_variants=True,
            seo_title="Hybrid Inverters | Example Store",
            seo_meta_description="Hybrid inverters for home backup.",
        )

    def test_blank_by_default_and_inherits_the_parent(self):
        variant = ProductVariant.objects.create(product=self.product, sku="HI-12K")
        variant.refresh_from_db()
        self.assertEqual((variant.seo_title, variant.seo_meta_description), ("", ""))
        self.assertEqual(variant.get_effective_field("seo_title"), "Hybrid Inverters | Example Store")
        self.assertEqual(variant.get_effective_field("seo_meta_description"), "Hybrid inverters for home backup.")

    def test_own_value_wins(self):
        variant = ProductVariant.objects.create(
            product=self.product, sku="HI-15K",
            seo_title="15kW Hybrid Inverter | Example Store",
            seo_meta_description="A 15kW hybrid inverter for whole-home backup.",
        )
        variant.refresh_from_db()
        self.assertEqual(variant.get_effective_field("seo_title"), "15kW Hybrid Inverter | Example Store")
        self.assertEqual(variant.get_effective_field("seo_meta_description"),
                         "A 15kW hybrid inverter for whole-home backup.")

    def test_detail_serializer_exposes_them(self):
        variant = ProductVariant.objects.create(product=self.product, sku="HI-8K", seo_title="Own title")
        data = ProductVariantDetailSerializer(variant).data
        self.assertEqual(data["seo_title"], "Own title")
        self.assertEqual(data["seo_meta_description"], "")


class VariantSectionsCoverFormTests(TestCase):
    def test_every_variant_form_field_is_in_a_section(self):
        listed = {f for section in VARIANT_SECTIONS for f in section["fields"]}
        self.assertEqual(set(ProductVariantForm().fields) - listed, set())
        self.assertTrue({"seo_title", "seo_meta_description"} <= listed)

    def test_profile_page_renders_the_variant_seo_inputs(self):
        product = Product.objects.create(title="Inverter", has_variants=True)
        ProductVariant.objects.create(product=product, sku="INV-1", seo_title="Inverter One | Store")
        user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        self.client.force_login(user)
        html = self.client.get(reverse("erp-product-detail", kwargs={"pk": product.pk})).content.decode()
        self.assertIn('name="variants-0-seo_title"', html)
        self.assertIn('name="variants-0-seo_meta_description"', html)
        self.assertIn("Inverter One | Store", html)
