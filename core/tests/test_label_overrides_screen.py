"""The labels screen and the stored tenant layer (release harness R10.05,
``R10-no-labels-screen``; docs/labels.md).

A rewording saved at ``/erp/settings/labels/`` is a ``core.LabelOverride``
row; ``{% label %}`` on every staff page of that company shows it at once
(the registry's cache is invalidated by the model's receivers), another
company never sees it, and the setting layer still applies underneath.
"""

from __future__ import annotations

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from core import label_registry
from core.labels import label
from core.models import Company, LabelOverride, UserProfile
from core.tests.helpers import make_staff_user

URL = "/erp/settings/labels/"


class LabelOverridesScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other = Company.objects.create(name="Tenant B")
        cls.editor = make_staff_user("labels-editor", password="pw")
        cls.editor.user_permissions.add(Permission.objects.get(codename="change_labeloverride"))
        UserProfile.objects.update_or_create(user=cls.editor, defaults={"company": cls.company})
        cls.reader = make_staff_user("labels-reader", password="pw")
        UserProfile.objects.update_or_create(user=cls.reader, defaults={"company": cls.company})
        cls.outsider = make_staff_user("labels-outsider", password="pw")
        UserProfile.objects.update_or_create(user=cls.outsider, defaults={"company": cls.other})

    def setUp(self):
        cache.clear()
        self.client.force_login(self.editor)

    def tearDown(self):
        cache.clear()

    def test_sidebar_links_the_screen_and_the_page_lists_every_key(self):
        response = self.client.get(reverse("erp-settings-teams"))
        self.assertContains(response, URL)
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        for key in label_registry.DEFAULTS:
            self.assertContains(response, f"<code class=\"text-xs\">{key}</code>")
        self.assertContains(response, 'name="singular"')

    def test_a_rewording_set_on_the_screen_changes_label_output_on_a_staff_page(self):
        page = self.client.get(reverse("erp-crm-engagement-list"))
        self.assertContains(page, "Engagements")
        self.assertNotContains(page, "Ideas")
        response = self.client.post(URL, {"key": "crm.engagement", "singular": "Idea", "plural": "Ideas", "article": "an"})
        self.assertEqual(response.status_code, 302)
        row = LabelOverride.objects.get(company=self.company, key="crm.engagement")
        self.assertEqual((row.singular, row.plural, row.article), ("Idea", "Ideas", "an"))
        self.assertEqual(label("crm.engagement", company=self.company), "Idea")
        self.assertEqual(label("crm.engagement", company=self.company, plural=True), "Ideas")
        page = self.client.get(reverse("erp-crm-engagement-list"))
        self.assertContains(page, "Ideas")
        # The screen shows where the word comes from.
        screen = self.client.get(URL)
        self.assertContains(screen, 'data-testid="label-effective-crm.engagement">Idea<')
        self.assertContains(screen, "tenant")
        # Another tenant is untouched; the default holds without a company.
        self.assertEqual(label("crm.engagement", company=self.other), "Engagement")
        self.assertEqual(label("crm.engagement"), "Engagement")
        self.client.force_login(self.outsider)
        self.assertNotContains(self.client.get(reverse("erp-crm-engagement-list")), "Ideas")

    def test_clearing_the_singular_removes_the_row_and_the_cache(self):
        self.client.post(URL, {"key": "orders.quote", "singular": "Proposal", "plural": "Proposals"})
        self.assertEqual(label("orders.quote", company=self.company), "Proposal")
        self.client.post(URL, {"key": "orders.quote", "singular": ""})
        self.assertFalse(LabelOverride.objects.filter(company=self.company).exists())
        self.assertEqual(label("orders.quote", company=self.company), "Quote")

    def test_plural_and_article_default_from_the_singular(self):
        self.client.post(URL, {"key": "orders.quote", "singular": "Estimate"})
        self.assertEqual(label("orders.quote", company=self.company, plural=True), "Estimates")
        self.assertEqual(label("orders.quote", company=self.company, article=True), "an Estimate")

    @override_settings(GOLDEN_LABEL_TENANT_OVERRIDES=None)
    def test_a_stored_row_wins_over_the_setting_layer(self):
        with override_settings(GOLDEN_LABEL_TENANT_OVERRIDES={str(self.company.pk): {"crm.task": {"singular": "Action"}}}):
            self.assertEqual(label("crm.task", company=self.company), "Action")
            LabelOverride.objects.create(company=self.company, key="crm.task", singular="To-do", plural="To-dos")
            self.assertEqual(label("crm.task", company=self.company), "To-do")
            self.assertEqual(label("crm.lead", company=self.company), "Lead")

    def test_unknown_key_is_refused(self):
        response = self.client.post(URL, {"key": "crm.nope", "singular": "X"}, follow=True)
        self.assertContains(response, "key:")
        self.assertFalse(LabelOverride.objects.exists())

    def test_reader_sees_the_page_without_the_form_and_cannot_post(self):
        self.client.force_login(self.reader)
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="singular"')
        self.assertEqual(self.client.post(URL, {"key": "crm.engagement", "singular": "Idea"}).status_code, 403)
        self.assertFalse(LabelOverride.objects.exists())

    def test_without_rows_no_query_is_spent_per_label(self):
        label_registry.stored_any()  # warm the flag
        with self.assertNumQueries(0):
            self.assertEqual(label("crm.engagement", company=self.company), "Engagement")
