"""Category.body_copy: model field, draft guard, ERP form, Django admin, API.

`body_copy` is the long-form page body rendered below the storefront product
grid. It must stay separate from `description` (the meta description), and the
ERP edit page must actually render it: CategoryForm is built with `exclude`, so
body_copy is a form field whether or not a CATEGORY_SECTIONS entry lists it,
and a form field the template never renders is submitted empty, which would
silently wipe the copy on every unrelated save.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from products.erp_views import CATEGORY_SECTIONS
from products.forms import CategoryForm
from products.models import Category, body_copy_is_draft

BODY = "## About\n\nHardware for **real** installs. See [ground mounts](/ground-mounts/)."
CASES = json.loads((Path(__file__).parent / "test_data" / "body_copy_draft_cases.json").read_text())
DRAFT = "slug: ground-mounts\nstatus: DRAFT - pending human review, not approved for publication\n\n" + BODY


class CategoryBodyCopyFieldTests(TestCase):
    def test_defaults_to_null_and_is_independent_of_description(self):
        cat = Category.objects.create(title="Metal Roof", description="Short meta text.")
        cat.refresh_from_db()
        self.assertIsNone(cat.body_copy)
        self.assertEqual(cat.description, "Short meta text.")

        cat.body_copy = BODY
        cat.save()
        cat.refresh_from_db()
        self.assertEqual(cat.body_copy, BODY)
        self.assertEqual(cat.description, "Short meta text.")

    def test_whitespace_only_copy_is_stored_as_null(self):
        cat = Category.objects.create(title="Racking", body_copy="  \n ")
        cat.refresh_from_db()
        self.assertIsNone(cat.body_copy)

    def test_form_saves_blank_body_copy_as_null(self):
        # null (not "") is what the storefront treats as "render nothing".
        form = CategoryForm(data={"title": "Breakers", "sort_order": 0, "body_copy": ""})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.save().body_copy)

    def test_model_clean_refuses_draft_copy(self):
        cat = Category(title="Ground Mounts", body_copy=DRAFT)
        with self.assertRaises(ValidationError) as cm:
            cat.full_clean()
        self.assertIn("body_copy", cm.exception.message_dict)


class CategoryBodyCopyDraftGuardTests(TestCase):
    """Only approved copy saves: the storefront renders nothing for anything else."""

    def test_detects_every_shared_draft_case(self):
        for text in CASES["draft"]:
            self.assertTrue(body_copy_is_draft(text), repr(text))
        for text in [None, "", *CASES["not_draft"], *CASES["renders_visibly"]]:
            self.assertFalse(body_copy_is_draft(text), repr(text))

    def test_form_rejects_every_shared_draft_case(self):
        for text in CASES["draft"]:
            form = CategoryForm(data={"title": "Ground Mounts", "sort_order": 0, "body_copy": text})
            self.assertFalse(form.is_valid(), repr(text))
            self.assertIn("DRAFT", str(form.errors["body_copy"]))

    def test_form_accepts_prose_that_mentions_draft(self):
        for text in CASES["not_draft"]:
            form = CategoryForm(data={"title": "Draft Stoppers", "sort_order": 0, "body_copy": text})
            self.assertTrue(form.is_valid(), (repr(text), form.errors))
            # forms.CharField strips surrounding whitespace (NBSP included).
            self.assertEqual(form.save().body_copy, text.strip())

    def test_form_rejects_draft_copy(self):
        form = CategoryForm(data={"title": "Ground Mounts", "sort_order": 0, "body_copy": DRAFT})
        self.assertFalse(form.is_valid())
        self.assertIn("DRAFT", str(form.errors["body_copy"]))

    def test_form_accepts_copy_once_metadata_is_removed(self):
        form = CategoryForm(data={"title": "Ground Mounts", "sort_order": 0, "body_copy": BODY})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().body_copy, BODY)


class CategoryBodyCopyErpFormTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        self.client.force_login(user)
        self.cat = Category.objects.create(
            title="Ground Mounts", description="Short meta text.", body_copy=BODY,
        )

    def test_body_copy_is_listed_in_a_form_section(self):
        listed = {f for section in CATEGORY_SECTIONS for f in section["fields"]}
        self.assertIn("body_copy", listed)

    def test_every_category_form_field_is_rendered_somewhere(self):
        # Guards the whole class of bug, not just body_copy: an unlisted form
        # field is posted empty and overwrites whatever the row held.
        listed = {f for section in CATEGORY_SECTIONS for f in section["fields"]}
        self.assertEqual(set(CategoryForm().fields) - listed, set())

    def test_edit_page_renders_existing_body_copy(self):
        html = self.client.get(reverse("erp-category-detail", args=[self.cat.pk])).content.decode()
        self.assertIn('name="body_copy"', html)
        self.assertIn("Hardware for **real** installs.", html)

    def test_edit_page_refuses_draft_copy_and_keeps_the_saved_copy(self):
        resp = self.client.post(
            reverse("erp-category-detail", args=[self.cat.pk]),
            {"title": "Ground Mounts", "sort_order": 0, "visible_on_website": "on",
             "description": "Short meta text.", "body_copy": DRAFT},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("not approved for publication", resp.content.decode())
        self.cat.refresh_from_db()
        self.assertEqual(self.cat.body_copy, BODY)

    def test_edit_page_post_updates_body_copy_without_touching_description(self):
        resp = self.client.post(
            reverse("erp-category-detail", args=[self.cat.pk]),
            {"title": "Ground Mounts", "sort_order": 0, "visible_on_website": "on",
             "description": "Short meta text.", "body_copy": "New body."},
        )
        self.assertEqual(resp.status_code, 302)
        self.cat.refresh_from_db()
        self.assertEqual(self.cat.body_copy, "New body.")
        self.assertEqual(self.cat.description, "Short meta text.")


class CategoryBodyCopyDjangoAdminTests(TestCase):
    def test_admin_change_form_exposes_body_copy(self):
        admin_user = User.objects.create_superuser("admin", "admin@example.com", "pw")
        self.client.force_login(admin_user)
        cat = Category.objects.create(title="Lithium Batteries")
        html = self.client.get(reverse("admin:products_category_change", args=[cat.pk])).content.decode()
        self.assertIn('name="body_copy"', html)


class CategoryBodyCopyApiTests(TestCase):
    """The DRF category API skips Model.clean(); the serializer guards it."""

    def setUp(self):
        from rest_framework.test import APIClient

        self.client = APIClient()
        self.client.force_authenticate(User.objects.create_user(username="api", password="pw", is_staff=True))
        self.cat = Category.objects.create(title="Ground Mounts", body_copy=BODY)

    def test_serializer_exposes_body_copy(self):
        from products.serializers import CategorySerializer

        self.assertEqual(CategorySerializer(self.cat).data["body_copy"], BODY)

    def test_serializer_rejects_every_shared_draft_case(self):
        from products.serializers import CategorySerializer

        for text in CASES["draft"]:
            s = CategorySerializer(self.cat, data={"body_copy": text}, partial=True)
            self.assertFalse(s.is_valid(), repr(text))
            self.assertIn("DRAFT", str(s.errors["body_copy"]))
        for text in CASES["not_draft"]:
            s = CategorySerializer(self.cat, data={"body_copy": text}, partial=True)
            self.assertTrue(s.is_valid(), (repr(text), s.errors))

    def test_patch_with_draft_copy_is_400_and_keeps_the_saved_copy(self):
        url = f"/api/v1/products/categories/{self.cat.pk}/"
        resp = self.client.patch(url, {"body_copy": DRAFT}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("body_copy", json.dumps(resp.json()))
        self.cat.refresh_from_db()
        self.assertEqual(self.cat.body_copy, BODY)

        resp = self.client.patch(url, {"body_copy": "Reviewed copy."}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.cat.refresh_from_db()
        self.assertEqual(self.cat.body_copy, "Reviewed copy.")
