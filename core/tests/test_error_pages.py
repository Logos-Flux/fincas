"""Styled 403, 404 and 500 pages (release-hardening-plan.md 9.7).

The back office never shows Django's bare "Forbidden" / "Not Found": the
staff gate and the demo guard return the styled 403, unknown ``/erp/`` URLs
get the styled 404, the 500 handler renders with no database and no
middleware state, and ``/api/`` keeps JSON for all three.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from core import error_views
from core.tests.helpers import make_staff_user

LOGOUT = reverse("erp-logout")
DEMO = {"DEMO_USERNAMES": ["demo"], "DEMO_LOGIN_USERNAME": "demo", "DEMO_LOGIN_PASSWORD": "try-it"}


def assert_styled(test, response, status, template):
    test.assertEqual(response.status_code, status)
    test.assertEqual(response["Content-Type"].split(";")[0], "text/html")
    test.assertTemplateUsed(response, template)
    test.assertTemplateUsed(response, "errors/base.html")
    body = response.content.decode()
    test.assertIn(f'action="{LOGOUT}"', body)  # sign-out (a POST form: LogoutView refuses GET)
    test.assertIn("Sign out", body)
    test.assertIn('class="btn-login" href="', body)  # the back link
    test.assertIn("auth.css", body)  # the sign-in shell's stylesheet: styled, not bare


class HandlersAreRegisteredTests(TestCase):
    def test_both_urlconfs_name_the_handlers(self):
        import config.urls
        import golden_erp.urls

        for module in (golden_erp.urls, config.urls):
            self.assertEqual(module.handler403, "core.error_views.permission_denied")
            self.assertEqual(module.handler404, "core.error_views.page_not_found")
            self.assertEqual(module.handler500, "core.error_views.server_error")


class ForbiddenPageTests(TestCase):
    def setUp(self):
        self.civilian = User.objects.create_user(username="civilian", password="pw")

    def test_non_staff_login_gets_the_styled_403(self):
        self.client.force_login(self.civilian)
        response = self.client.get(reverse("erp-dashboard"))
        assert_styled(self, response, 403, "403.html")
        self.assertContains(response, "A staff account is required for the back office.", status_code=403)
        self.assertContains(response, f'href="{reverse("erp-login")}"', status_code=403)

    def test_handler403_renders_the_reason(self):
        request = RequestFactory().get("/erp/orders/")
        response = error_views.permission_denied(request, PermissionDenied("Not for you."))
        self.assertEqual(response.status_code, 403)
        self.assertIn("Not for you.", response.content.decode())
        # A bare PermissionDenied() has no reason: the page shows no empty alert.
        response = error_views.permission_denied(request, PermissionDenied())
        self.assertNotIn('role="alert"', response.content.decode())

    def test_api_paths_stay_json(self):
        request = RequestFactory().get("/api/v1/core/companies/")
        response = error_views.permission_denied(request, PermissionDenied("Not for you."))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(json.loads(response.content), {"detail": "Not for you."})
        # Through the stack: DRF answers its own 403 as JSON for a non-staff session.
        self.client.force_login(self.civilian)
        response = self.client.get("/api/v1/core/companies/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["Content-Type"].split(";")[0], "application/json")

    @override_settings(**DEMO)
    def test_demo_guard_refusal_is_styled(self):
        demo = User.objects.create_user("demo", "demo@example.com", "try-it", is_staff=True)
        self.client.force_login(demo)
        response = self.client.get("/admin/")
        assert_styled(self, response, 403, "403.html")
        self.assertContains(response, "That action is turned off in the public demo.", status_code=403)


class NotFoundPageTests(TestCase):
    def setUp(self):
        self.staff = make_staff_user("staffer", password="pw")
        self.client.force_login(self.staff)

    def test_bogus_erp_url_gets_the_styled_404(self):
        response = self.client.get("/erp/no-such-page/")
        assert_styled(self, response, 404, "404.html")
        self.assertContains(response, "/erp/no-such-page/", status_code=404)
        self.assertContains(response, 'href="/erp/"', status_code=404)

    def test_api_404_stays_json(self):
        response = self.client.get("/api/v1/no-such-endpoint/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"detail": error_views.NOT_FOUND_TEXT})


class ServerErrorPageTests(TestCase):
    def test_handler500_renders_without_request_state_or_queries(self):
        request = RequestFactory().get("/erp/orders/")  # no user, no session, no middleware ran
        with self.assertNumQueries(0):
            response = error_views.server_error(request)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response["Content-Type"].split(";")[0], "text/html")
        body = response.content.decode()
        self.assertIn("500", body)
        self.assertIn("Server error", body)
        self.assertIn(f'action="{LOGOUT}"', body)
        self.assertIn('href="/erp/"', body)
        self.assertIn("auth.css", body)

    def test_api_500_stays_json(self):
        response = error_views.server_error(RequestFactory().get("/api/v1/orders/"))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(json.loads(response.content), {"detail": error_views.SERVER_ERROR_TEXT})
