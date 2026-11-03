"""Pure mapping: request rows, settings validation, partial-failure parsing."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

from django.test import SimpleTestCase

from golden_erp.addons.google_ads import mapping, values

SETTINGS = {"customer_id": "123-456-7890", "conversion_actions": {"sales": "555"}}
CLOSED = dt.datetime(2026, 3, 2, 15, 0, tzinfo=dt.timezone.utc)


class RowTests(SimpleTestCase):
    def row(self, value):
        return mapping.conversion_row(
            click_kind="gclid", click_id="Cj0FIXTURE-1", action=mapping.action_resource(SETTINGS, "sales"),
            closed_at=CLOSED, order_id="EN-00001", currency="EUR", value=value,
            consent_value=mapping.consent(SETTINGS),
        )

    def test_a_known_amount_uploads_as_is_and_a_null_amount_sends_no_value_field(self):
        self.assertEqual(self.row(Decimal("1200.50")), {
            "gclid": "Cj0FIXTURE-1",
            "conversionAction": "customers/1234567890/conversionActions/555",
            "conversionDateTime": "2026-03-02 15:00:00+00:00",
            "orderId": "EN-00001",
            "currencyCode": "EUR",
            "consent": {"adUserData": "UNSPECIFIED"},
            "conversionValue": 1200.5,
        })
        unknown = self.row(None)
        self.assertNotIn("conversionValue", unknown)
        self.assertEqual(self.row(Decimal("0.00"))["conversionValue"], 0.0)  # a real zero stays

    def test_the_row_is_byte_identical_across_calls(self):
        self.assertEqual(json.dumps(self.row(Decimal("10"))), json.dumps(self.row(Decimal("10"))))

    def test_consent_default_and_override(self):
        self.assertEqual(mapping.consent({}), values.DEFAULT_CONSENT)
        self.assertEqual(mapping.consent({"ad_user_data_consent": "GRANTED"}), "GRANTED")

    def test_close_time_is_utc(self):
        local = CLOSED.astimezone(dt.timezone(dt.timedelta(hours=-5)))
        self.assertEqual(mapping.format_datetime(local), "2026-03-02 15:00:00+00:00")

    def test_unmapped_pipeline_has_no_action(self):
        self.assertEqual(mapping.action_resource(SETTINGS, "service"), "")


class SettingsTests(SimpleTestCase):
    def test_valid(self):
        self.assertEqual(mapping.validate_settings({**SETTINGS, "click_window_days": 30,
                                                    "ad_user_data_consent": "DENIED"}), [])

    def test_each_bad_key_is_named(self):
        bad = {"customer_id": "12", "login_customer_id": "x", "conversion_actions": {"sales": "abc"},
               "click_window_days": 0, "ad_user_data_consent": "YES"}
        errors = mapping.validate_settings(bad)
        for key in bad:
            self.assertTrue(any(e.startswith(key) for e in errors), key)


class PartialFailureTests(SimpleTestCase):
    def test_codes_by_row_index_never_the_message(self):
        payload = {"results": [{}, {"gclid": "x"}, {}], "partialFailureError": {
            "code": 3, "message": "conversion for ada@example.com failed",
            "details": [{"errors": [
                {"errorCode": {"conversionUploadError": "CLICK_NOT_FOUND"},
                 "message": "ada@example.com",
                 "location": {"fieldPathElements": [{"fieldName": "conversions", "index": 0}]}},
                {"errorCode": {"databaseError": "CONCURRENT_MODIFICATION"},
                 "location": {"fieldPathElements": [{"fieldName": "conversions", "index": 2},
                                                    {"fieldName": "gclid"}]}},
            ]}]}}
        self.assertEqual(mapping.failed_rows(payload), {0: "CLICK_NOT_FOUND", 2: "CONCURRENT_MODIFICATION"})
        self.assertEqual(mapping.failed_rows({"results": [{}]}), {})
        mixed = {"partialFailureError": {"details": [{"errors": [
            {"errorCode": {"a": "CLICK_NOT_FOUND"}, "location": {"fieldPathElements": [
                {"fieldName": "conversions", "index": 1}]}},
            {"errorCode": {"a": "INTERNAL_ERROR"}}]}]}}
        self.assertEqual(mapping.failed_rows(mixed), {1: "CLICK_NOT_FOUND", -1: "PARTIAL_FAILURE_UNLOCATED"})
        self.assertEqual(mapping.failed_rows({"partialFailureError": {"code": 13}}),
                         {-1: "PARTIAL_FAILURE_UNLOCATED"})
