"""Plugin engine boot contract: app composition and URL wiring.

- GOLDEN_PLUGIN_APPS composes into INSTALLED_APPS; a BasePlugin class there,
  or a Django app in GOLDEN_PLUGINS, fails startup with ImproperlyConfigured.
- Plugin URLs: a path claimed by two plugins fails startup naming both;
  default plugin URLs sit behind the /erp/ login gate plus is_staff; a plugin
  that declares url_auth = "public" is reachable anonymously.
"""

from __future__ import annotations

import sys
from types import ModuleType

from django.apps import apps
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import include, path

from core.plugin_apps import compose_installed_apps, is_base_plugin_path
from core.plugins import plugin_registry
from core.plugins.registry import check_plugin_classification

EXAMPLE = "golden_erp.addons.example_audit_notifier.plugin.ExampleAuditNotifierPlugin"
FIX = "core.tests.plugin_fixtures.glue"
UNIMPORTED = "core.tests.plugin_fixtures.unimported_glue"


class ComposeInstalledAppsTests(SimpleTestCase):
    def test_appends_apps_and_is_idempotent(self):
        composed = compose_installed_apps(["core"], ["inventory", "core"])
        self.assertEqual(composed, ["core", "inventory"])
        self.assertEqual(compose_installed_apps(composed, ["inventory"]), composed)

    def test_rejects_duplicate_and_non_string_entries(self):
        with self.assertRaises(ImproperlyConfigured):
            compose_installed_apps([], ["inventory", "inventory"])
        with self.assertRaises(ImproperlyConfigured):
            compose_installed_apps([], [None])
        with self.assertRaises(ImproperlyConfigured):
            compose_installed_apps([], "inventory")

    def test_baseplugin_class_path_fails(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "List it in GOLDEN_PLUGINS"):
            compose_installed_apps(["core"], [EXAMPLE])

    def test_baseplugin_class_object_fails(self):
        from core.tests.plugin_fixtures.glue import AlphaPlugin

        with self.assertRaisesMessage(ImproperlyConfigured, "BasePlugin glue class"):
            compose_installed_apps(["core"], [AlphaPlugin])

    def test_detects_plugin_from_source_without_importing(self):
        sys.modules.pop(UNIMPORTED, None)
        self.assertTrue(is_base_plugin_path(f"{UNIMPORTED}.IndirectPlugin"))
        self.assertFalse(is_base_plugin_path(f"{UNIMPORTED}.NotAPlugin"))
        self.assertNotIn(UNIMPORTED, sys.modules)  # read, never executed
        with self.assertRaises(ImproperlyConfigured):
            compose_installed_apps([], [f"{UNIMPORTED}.IndirectPlugin"])

    def test_app_module_and_appconfig_paths_are_not_plugins(self):
        self.assertFalse(is_base_plugin_path("inventory"))
        self.assertFalse(is_base_plugin_path("inventory.apps.InventoryConfig"))


class PluginClassificationTests(SimpleTestCase):
    def test_default_settings_pass(self):
        check_plugin_classification()

    def test_valid_split_passes(self):
        with override_settings(GOLDEN_PLUGINS=[EXAMPLE], GOLDEN_PLUGIN_APPS=["inventory"]):
            check_plugin_classification()

    def test_django_app_in_golden_plugins_fails(self):
        for entry in ("inventory", "inventory.apps.InventoryConfig"):
            with self.subTest(entry=entry), override_settings(GOLDEN_PLUGINS=[entry]):
                with self.assertRaisesMessage(ImproperlyConfigured, "GOLDEN_PLUGIN_APPS"):
                    check_plugin_classification()

    def test_appconfig_class_object_in_golden_plugins_fails(self):
        config_cls = type(apps.get_app_config("inventory"))
        with override_settings(GOLDEN_PLUGINS=[config_cls]):
            with self.assertRaises(ImproperlyConfigured):
                check_plugin_classification()

    def test_baseplugin_in_golden_plugin_apps_fails(self):
        with override_settings(GOLDEN_PLUGIN_APPS=[EXAMPLE]):
            with self.assertRaisesMessage(ImproperlyConfigured, "List it in GOLDEN_PLUGINS"):
                check_plugin_classification()

    def test_plugin_app_missing_from_installed_apps_fails(self):
        with override_settings(GOLDEN_PLUGIN_APPS=["not_composed_app"]):
            with self.assertRaisesMessage(ImproperlyConfigured, "compose_installed_apps"):
                check_plugin_classification()

    def test_startup_hook_raises_on_misclassification(self):
        with override_settings(GOLDEN_PLUGINS=["inventory"]):
            with self.assertRaises(ImproperlyConfigured):
                apps.get_app_config("core").ready()


class PluginUrlCollisionTests(SimpleTestCase):
    def test_two_plugins_claiming_one_path_fail_naming_both(self):
        with override_settings(GOLDEN_PLUGINS=[f"{FIX}.AlphaPlugin", f"{FIX}.BetaPlugin"]):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                plugin_registry.get_urls()
        message = str(ctx.exception)
        for fragment in ("alpha_fixture", "beta_fixture", "plugin-alpha/"):
            self.assertIn(fragment, message)

    def test_startup_hook_raises_on_collision(self):
        with override_settings(GOLDEN_PLUGINS=[f"{FIX}.AlphaPlugin", f"{FIX}.BetaPlugin"]):
            with self.assertRaises(ImproperlyConfigured):
                apps.get_app_config("core").ready()

    def test_unknown_url_auth_value_fails_closed(self):
        with override_settings(GOLDEN_PLUGINS=[f"{FIX}.TypoAuthPlugin"]):
            with self.assertRaisesMessage(ImproperlyConfigured, "url_auth='none'"):
                plugin_registry.get_urls()

    def test_plugin_url_name_taken_from_core_fails(self):
        # reverse() prefers the last-listed pattern, so a plugin reusing a
        # core URL name would silently capture every {% url %} to it.
        from golden_erp.urls import urlpatterns as core_patterns

        reserved = [path("erp/", include([path("x/", lambda r: None, name="plugin-alpha")]))]
        with override_settings(GOLDEN_PLUGINS=[f"{FIX}.AlphaPlugin"]):
            with self.assertRaisesMessage(ImproperlyConfigured, "'plugin-alpha'"):
                plugin_registry.get_urls(reserved=reserved)
            # The real core URLconf reserves names AlphaPlugin doesn't use.
            self.assertTrue(plugin_registry.get_urls(reserved=core_patterns))

    def test_no_plugins_no_urls(self):
        with override_settings(GOLDEN_PLUGINS=[]):
            self.assertEqual(plugin_registry.get_urls(), [])


def _urlconf():
    """The root URLconf shape: core /erp/ first, plugin URLs last."""
    module = ModuleType("plugin_test_urlconf")
    module.urlpatterns = [path("erp/", include("core.erp_urls"))] + plugin_registry.get_urls()
    return module


class PluginUrlAuthTests(TestCase):
    plugins = [f"{FIX}.AlphaPlugin", f"{FIX}.PublicPlugin"]

    def setUp(self):
        settings_ctx = override_settings(GOLDEN_PLUGINS=self.plugins)
        settings_ctx.enable()
        self.addCleanup(settings_ctx.disable)
        url_ctx = override_settings(ROOT_URLCONF=_urlconf())
        url_ctx.enable()
        self.addCleanup(url_ctx.disable)

    def test_anonymous_gets_same_response_as_erp(self):
        erp = self.client.get("/erp/")
        for url in ("/plugin-alpha/", "/plugin-alpha/nested/deep/"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, erp.status_code)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], f"/erp/login/?next={url}")
                self.assertTrue(erp["Location"].startswith("/erp/login/?next="))

    def test_anonymous_redirects_to_erp_login_with_next(self):
        response = self.client.get("/plugin-alpha/")
        self.assertEqual(response["Location"], "/erp/login/?next=/plugin-alpha/")
        self.assertEqual(self.client.get("/erp/")["Location"], "/erp/login/?next=/erp/")

    def test_non_staff_user_is_forbidden(self):
        user = User.objects.create_user(username="shopper", password="pw")
        self.client.force_login(user)
        self.assertEqual(self.client.get("/plugin-alpha/").status_code, 403)
        self.assertEqual(self.client.get("/plugin-alpha/nested/deep/").status_code, 403)

    def test_staff_user_reaches_plugin_view(self):
        user = User.objects.create_user(username="staffer", password="pw", is_staff=True)
        self.client.force_login(user)
        response = self.client.get("/plugin-alpha/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"plugin ok")

    def test_public_opt_out_is_reachable_anonymously(self):
        response = self.client.get("/plugin-public/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"plugin ok")
