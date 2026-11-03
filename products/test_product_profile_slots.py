"""Plugin panels on the product profile page (TemplateExtension slots).

A plugin adds panels, modals and scripts to /erp/products/<pk>/ by returning
``TemplateExtension``s for ``product_profile_panels`` / ``_modals`` /
``_scripts`` (docs/plugins.md, "Template slots"); the page renders them with
``{% plugin_slot %}``, so no deployment has to shadow the whole template.
"""

import copy
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from core.plugins import BasePlugin, TemplateExtension
from products.models import Product


class ProfilePanelPlugin(BasePlugin):
    name = "profile_panel_plugin"

    def get_template_extensions(self):
        return [
            TemplateExtension("product_profile_panels", "slot_test/second.html", order=20),
            TemplateExtension("product_profile_panels", "slot_test/first.html", order=10),
            TemplateExtension("product_profile_modals", "slot_test/modal.html"),
            TemplateExtension("product_profile_scripts", "slot_test/script.html"),
            TemplateExtension("some_other_page", "slot_test/elsewhere.html"),
        ]


TEMPLATES_ON_DISK = {
    "slot_test/first.html": '<div id="panel-first">first {{ product.title }} {{ plugin_extension.order }}</div>',
    "slot_test/second.html": '<div id="panel-second">second</div>',
    "slot_test/modal.html": '<div class="modal" id="slot-test-modal"><form id="slot-test-form"></form></div>',
    "slot_test/script.html": "<script>window.slotTest = '{{ product.pk }}';</script>",
    "slot_test/elsewhere.html": '<div id="panel-elsewhere"></div>',
}


class ProductProfilePluginSlotTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.template_dir = tempfile.mkdtemp()
        for name, body in TEMPLATES_ON_DISK.items():
            path = Path(cls.template_dir) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        templates = copy.deepcopy(settings.TEMPLATES)
        templates[0]["DIRS"] = [cls.template_dir, *templates[0].get("DIRS", [])]
        cls._templates_override = override_settings(TEMPLATES=templates)
        cls._templates_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._templates_override.disable()
        shutil.rmtree(cls.template_dir, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.staff = User.objects.create_user("profile-staff", password="pw", is_staff=True)
        self.client.force_login(self.staff)
        self.product = Product.objects.create(title="Slot Panel", slug="slot-panel")
        self.url = reverse("erp-product-detail", kwargs={"pk": self.product.pk})

    def test_plugin_panels_modals_and_scripts_render_in_order(self):
        with override_settings(GOLDEN_PLUGINS=[ProfilePanelPlugin]):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('<div id="panel-first">first Slot Panel 10</div>', html)
        self.assertLess(html.index('id="panel-first"'), html.index('id="panel-second"'))
        # Panels sit inside the profile form; modals after it (they may hold
        # forms); scripts after the page's own script.
        form_end = html.index("</form>", html.index('id="profile-form"'))
        self.assertLess(html.index('id="panel-second"'), form_end)
        self.assertGreater(html.index('id="slot-test-modal"'), form_end)
        self.assertIn(f"window.slotTest = '{self.product.pk}';", html)
        self.assertNotIn("panel-elsewhere", html)

    def test_slot_output_is_autoescaped(self):
        self.product.title = "<script>alert(1)</script>"
        self.product.save(update_fields=["title"])
        with override_settings(GOLDEN_PLUGINS=[ProfilePanelPlugin]):
            response = self.client.get(self.url)
        self.assertContains(response, "first &lt;script&gt;alert(1)&lt;/script&gt; 10")
        self.assertNotContains(response, "first <script>alert(1)</script>")

    def test_no_plugin_renders_the_page_unchanged(self):
        with override_settings(GOLDEN_PLUGINS=[]):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "panel-first")

    def test_new_product_page_renders_no_panels(self):
        with override_settings(GOLDEN_PLUGINS=[ProfilePanelPlugin]):
            response = self.client.get(reverse("erp-product-create"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "panel-first")
        self.assertNotContains(response, "slot-test-modal")
        self.assertNotContains(response, "window.slotTest")
