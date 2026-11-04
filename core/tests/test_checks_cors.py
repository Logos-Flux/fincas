"""core.W115: the storefront's Idempotency-Key must pass the CORS preflight (OP #3431)."""

from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from core.checks import cors_idempotency_header_check

ORIGINS = ["https://shop.example.com"]


class CorsIdempotencyHeaderCheckTests(SimpleTestCase):
    @override_settings(CORS_ALLOWED_ORIGINS=ORIGINS)
    def test_golden_default_settings_pass(self):
        self.assertEqual(cors_idempotency_header_check(None), [])

    @override_settings(CORS_ALLOWED_ORIGINS=ORIGINS,
                       CORS_ALLOW_HEADERS=("accept", "authorization", "content-type"))
    def test_an_override_without_the_header_warns(self):
        [warning] = cors_idempotency_header_check(None)
        self.assertEqual(warning.id, "core.W115")

    @override_settings(CORS_ALLOWED_ORIGINS=ORIGINS,
                       CORS_ALLOW_HEADERS=("authorization", "Idempotency-Key"))
    def test_header_names_compare_case_insensitively(self):
        self.assertEqual(cors_idempotency_header_check(None), [])

    @override_settings(CORS_ALLOWED_ORIGINS=[], CORS_ALLOW_ALL_ORIGINS=False,
                       CORS_ALLOWED_ORIGIN_REGEXES=[],
                       CORS_ALLOW_HEADERS=("authorization",))
    def test_no_cross_origin_clients_no_warning(self):
        self.assertEqual(cors_idempotency_header_check(None), [])
