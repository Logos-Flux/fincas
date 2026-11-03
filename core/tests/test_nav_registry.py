"""Sidebar nav registry (core/nav_registry.py) + its context processor.

Same invariant as the theme registry: this is config, so a broken entry has
to fail loudly at import rather than silently vanishing from the sidebar.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, TestCase, override_settings

from core import nav_registry
from core.context_processors import erp_nav

VALID = [
    {"type": "header", "label": "Sales"},
    {"type": "link", "section": "orders", "url_name": "erp-order-list",
     "icon": "ti ti-shopping-cart", "color": "text-info", "label": "Sales Orders"},
    {"type": "header", "label": "Admin", "staff_only": True},
    {"type": "link", "section": "users", "url_name": "erp-user-list",
     "icon": "ti ti-users", "color": "text-dark", "label": "Users", "staff_only": True},
]


class BuildNavTests(TestCase):
    def test_ships_the_configured_items_in_order(self):
        with override_settings(ERP_NAV=VALID):
            self.assertEqual([i.label for i in nav_registry.NAV], ["Sales", "Sales Orders", "Admin", "Users"])

    def test_empty_registry_is_allowed(self):
        with override_settings(ERP_NAV=[]):
            self.assertEqual(nav_registry.NAV, [])

    def test_bad_type_raises(self):
        entries = [{"type": "banner", "label": "X"}]
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(ERP_NAV=entries):
                pass

    def test_header_without_label_raises(self):
        entries = [{"type": "header", "label": ""}]
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(ERP_NAV=entries):
                pass

    def test_link_without_section_raises(self):
        entries = [{"type": "link", "label": "X", "url_name": "erp-dashboard"}]
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(ERP_NAV=entries):
                pass

    def test_link_without_url_name_raises(self):
        entries = [{"type": "link", "label": "X", "section": "x"}]
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(ERP_NAV=entries):
                pass

    def test_unknown_key_raises(self):
        entries = [{"type": "header", "label": "X", "colour": "blue"}]
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(ERP_NAV=entries):
                pass


class ContextProcessorTests(TestCase):
    def test_staff_only_items_hidden_from_anonymous(self):
        request = RequestFactory().get("/erp/")
        request.user = get_user_model()()  # AnonymousUser-shaped: is_staff=False
        with override_settings(ERP_NAV=VALID):
            ctx = erp_nav(request)
            labels = [i["label"] for i in ctx["erp_nav"]]
            self.assertEqual(labels, ["Sales", "Sales Orders"])

    def test_staff_only_items_visible_to_staff(self):
        request = RequestFactory().get("/erp/")
        request.user = get_user_model()(is_staff=True)
        with override_settings(ERP_NAV=VALID):
            ctx = erp_nav(request)
            labels = [i["label"] for i in ctx["erp_nav"]]
            self.assertEqual(labels, ["Sales", "Sales Orders", "Admin", "Users"])

    def test_serialized_item_shape(self):
        request = RequestFactory().get("/erp/")
        request.user = get_user_model()(is_staff=True)
        with override_settings(ERP_NAV=VALID):
            item = erp_nav(request)["erp_nav"][1]
            self.assertEqual(
                item,
                {
                    "type": "link",
                    "label": "Sales Orders",
                    "section": "orders",
                    "url_name": "erp-order-list",
                    "icon": "ti ti-shopping-cart",
                    "color": "text-info",
                },
            )
