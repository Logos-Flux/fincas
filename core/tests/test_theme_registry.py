"""Theme registry (core/theme_registry.py) + its context processor.

The invariant worth pinning: the registry is *config*, so a broken entry has
to fail loudly at import. A theme that silently vanishes from the picker is
indistinguishable from one that was never configured — the same reasoning as
the chat model registry's tests.
"""

from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, SimpleTestCase, override_settings

from core import theme_registry
from core.context_processors import erp_themes

VALID = [
    {"name": "light", "label": "Light", "css": "assets/css/themes/eva-01.css", "mode": "light"},
    {
        "name": "eva-01",
        "label": "EVA-01",
        "css": "assets/css/themes/eva-01.css",
        "mode": "dark",
        "bg": "#030c1d",
    },
]


class BuildThemesTests(SimpleTestCase):
    def test_ships_the_configured_themes_in_order(self):
        with override_settings(ERP_THEMES=VALID, ERP_DEFAULT_THEME="light"):
            self.assertEqual(list(theme_registry.THEMES), ["light", "eva-01"])
            self.assertEqual(theme_registry.DEFAULT_THEME, "light")
            self.assertEqual(theme_registry.default_theme().bg, "")

    def test_css_none_is_allowed(self):
        entries = [{"name": "stock", "label": "Stock", "css": None, "mode": "light"}]
        with override_settings(ERP_THEMES=entries, ERP_DEFAULT_THEME="stock"):
            self.assertIsNone(theme_registry.THEMES["stock"].css)

    def test_unknown_default_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(ERP_THEMES=VALID, ERP_DEFAULT_THEME="nope"):
                pass

    def test_empty_registry_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(ERP_THEMES=[], ERP_DEFAULT_THEME="light"):
                pass

    def test_bad_mode_raises(self):
        entries = [{"name": "x", "label": "X", "css": None, "mode": "sepia"}]
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(ERP_THEMES=entries, ERP_DEFAULT_THEME="x"):
                pass

    def test_duplicate_name_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(ERP_THEMES=VALID + VALID[:1], ERP_DEFAULT_THEME="light"):
                pass

    def test_unknown_key_raises(self):
        entries = [{"name": "x", "label": "X", "mode": "light", "colour": "blue"}]
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(ERP_THEMES=entries, ERP_DEFAULT_THEME="x"):
                pass


class ContextProcessorTests(SimpleTestCase):
    def test_exposes_entries_with_resolved_css_urls(self):
        request = RequestFactory().get("/erp/")
        with override_settings(ERP_THEMES=VALID, ERP_DEFAULT_THEME="light"):
            ctx = erp_themes(request)
            self.assertEqual([t["name"] for t in ctx["erp_themes"]], ["light", "eva-01"])
            self.assertEqual(ctx["erp_default_theme"], "light")
            self.assertEqual(ctx["erp_default_theme_entry"]["name"], "light")
            # static() resolved, not the bare registry path
            self.assertTrue(ctx["erp_themes"][0]["css_url"].endswith("themes/eva-01.css"))
            self.assertTrue(ctx["erp_themes"][0]["css_url"].startswith("/"))

    def test_css_none_serializes_to_none(self):
        entries = [{"name": "stock", "label": "Stock", "css": None, "mode": "light"}]
        request = RequestFactory().get("/erp/")
        with override_settings(ERP_THEMES=entries, ERP_DEFAULT_THEME="stock"):
            self.assertIsNone(erp_themes(request)["erp_themes"][0]["css_url"])
