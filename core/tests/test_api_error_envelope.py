"""Contract pins for testing-plan §4 decisions (do not change lightly).

Decision 1 — error envelope: the API uses DRF's default exception handling.
``core.api_exceptions.exception_handler`` (the EXCEPTION_HANDLER since the
v0.4.0 hardening) delegates to DRF's handler and adds exactly one mapping:
a DELETE refused by an ``on_delete=PROTECT`` FK is a 409 ``code: protected``
(``core/tests/test_api_protected_delete.py``). Pinned shape:
- 404 unknown PK → ``{"detail": "..."}``
- 400 validation → ``{field: [messages]}`` dict, never an HTML page.
- 401/403 → ``{"detail": "..."}`` (JSON, not a login redirect).
- 409 protected delete → ``{"code": "protected", "detail": ..., "protected_by": [...]}``.

KNOWN GOTCHA (filed for Milestone 1): DRF binds
``APIView.permission_classes`` from settings at import time, so
``override_settings(REST_FRAMEWORK=...)`` cannot tighten permissions on
already-imported views — and test settings inherit dev's AllowAny. The 401
test below therefore patches the view class directly. The full
viewset-by-viewset permission audit under prod settings is Milestone 1
Agent A work (testing-plan §4 auth matrix).

Public catalog endpoints (products list) are intentionally anonymous-readable:
ProductViewSet.get_queryset() has an explicit anonymous branch filtering to
visible_on_website + is_active. Pinned below as deliberate.

Decision 2 — unknown filter params: DjangoFilterBackend ignores undeclared
query params (200, unfiltered) rather than 400ing. Changing either decision
requires updating docs/testing-plan.md §4 first.
"""

from unittest.mock import patch

from django.test import TestCase
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIClient

from core.tests.helpers import make_staff_user
from products.views import ProductViewSet


class ApiErrorEnvelopeTests(TestCase):
    def setUp(self):
        self.user = make_staff_user("envelope", "e@x.com", "pass")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_unknown_pk_returns_json_detail_not_html(self):
        response = self.client.get(
            "/api/v1/products/products/00000000-0000-0000-0000-000000000000/"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("detail", response.json())

    def test_validation_error_returns_field_dict(self):
        response = self.client.post(
            "/api/v1/products/categories/", {}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertIsInstance(payload, dict)
        self.assertNotIn("detail", payload)

    def test_unauthenticated_returns_json_401(self):
        # Patch the view class directly: override_settings cannot rebind
        # DRF's import-time permission_classes (see module docstring).
        anon = APIClient()
        with patch.object(
            ProductViewSet, "permission_classes", [IsAuthenticated]
        ):
            response = anon.get("/api/v1/products/products/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("detail", response.json())

    def test_public_catalog_readable_anonymously_by_design(self):
        anon = APIClient()
        response = anon.get("/api/v1/products/products/")
        self.assertEqual(response.status_code, 200)


class UnknownFilterParamTests(TestCase):
    def setUp(self):
        self.user = make_staff_user("filter", "f@x.com", "pass")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_undeclared_param_is_ignored(self):
        plain = self.client.get("/api/v1/products/products/")
        filtered = self.client.get(
            "/api/v1/products/products/?not_a_real_filter=xyz"
        )
        self.assertEqual(plain.status_code, 200)
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.json(), plain.json())
