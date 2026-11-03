"""`/erp/` is staff-only (StaffOnlyERPMiddleware).

`ERPViewMixin` only requires login, so before this middleware any signed-in
account reached the back office. These tests pin: non-staff sessions get 403
on every core `/erp/` route, anonymous users still get the login redirect,
and the login/logout/password-reset routes stay reachable for everyone.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.urls.resolvers import RoutePattern

from core import erp_urls
from core.middleware import StaffOnlyERPMiddleware


def _argless_erp_paths():
    """Every `/erp/` route with no URL arguments, straight from the URLconf."""
    paths = []
    for pattern in erp_urls.urlpatterns:
        if not isinstance(pattern.pattern, RoutePattern):
            continue
        route = str(pattern.pattern)
        if "<" in route:
            continue
        paths.append("/erp/" + route)
    return paths


class StaffOnlyERPTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username="staffer", password="pw", is_staff=True)
        self.civilian = User.objects.create_user(username="civilian", password="pw")
        self.dashboard = reverse("erp-dashboard")

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get(self.dashboard)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/erp/login/", resp["Location"])

    def test_non_staff_session_is_forbidden(self):
        self.client.force_login(self.civilian)
        self.assertEqual(self.client.get(self.dashboard).status_code, 403)

    def test_staff_reaches_the_dashboard(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.dashboard).status_code, 200)

    def test_non_staff_is_forbidden_on_every_argless_erp_route(self):
        """The guard is a path prefix, so new routes are covered without opting in."""
        self.client.force_login(self.civilian)
        paths = _argless_erp_paths()
        self.assertGreater(len(paths), 20, paths)
        leaked = []
        for path in paths:
            if path.startswith(StaffOnlyERPMiddleware.EXEMPT_PREFIXES):
                continue
            resp = self.client.get(path)
            if resp.status_code != 403:
                leaked.append((path, resp.status_code))
        self.assertEqual(leaked, [])

    def test_auth_routes_stay_open_to_non_staff(self):
        self.client.force_login(self.civilian)
        self.assertEqual(self.client.get("/erp/password-reset/").status_code, 200)
        resp = self.client.post("/erp/logout/")
        self.assertEqual(resp.status_code, 302)
        # Logged out now: the login page renders for the anonymous visitor.
        self.assertEqual(self.client.get("/erp/login/").status_code, 200)

    def test_prefix_is_exact(self):
        """`/erp-ish/` or `/api/` paths are not the back office."""
        self.client.force_login(self.civilian)
        # `/api/` is governed by DRF permissions, not this middleware: whatever it
        # answers, it must not be the middleware's 403 HTML.
        resp = self.client.get("/api/v1/core/companies/")
        self.assertNotEqual(resp.get("Content-Type", "").split(";")[0], "text/html")
