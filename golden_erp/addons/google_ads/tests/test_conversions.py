"""Rules 1-5 of 3.5 on real rows: the click, the window, the action, the amount, reopen."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.utils import timezone

from crm import engagements, touchpoints
from crm.tests.helpers import CrmTestCase
from golden_erp.addons.google_ads import conversions, values

SETTINGS = {"customer_id": "1234567890", "conversion_actions": {"sales": "555"}}


class PlanTests(CrmTestCase):
    def won(self, *, click_days=10, kind="gclid", value=Decimal("500.00"), currency="USD"):
        touch = None
        if click_days is not None:
            touch, _ = touchpoints.record(self.company, party=self.ada, source="api", click_id_kind=kind,
                                          click_id=f"{kind.upper()}-1",
                                          occurred_at=timezone.now() - timedelta(days=click_days))
        eng = engagements.create(company=self.company, party=self.ada, first_touch=touch, last_touch=touch,
                                 expected_value=value, currency=currency)
        engagements.win(eng)
        eng.refresh_from_db()
        return eng

    def plan(self, eng, settings=SETTINGS):
        return conversions.plan_upload(SimpleNamespace(object_id=eng.pk, company_id=eng.company_id), settings)

    def test_a_won_engagement_with_a_click_plans_one_row(self):
        eng = self.won()
        planned = self.plan(eng)
        self.assertEqual(planned.row["gclid"], "GCLID-1")
        self.assertEqual((planned.row["orderId"], planned.row["conversionValue"]), (eng.number, 500.0))
        self.assertFalse(planned.value_unknown)

    def test_skips(self):
        self.assertEqual(self.plan(self.won(click_days=None)).skip, values.NO_CLICK)
        self.assertEqual(self.plan(self.won(kind="fbclid")).skip, values.NO_CLICK)
        self.assertEqual(self.plan(self.won(), {**SETTINGS, "click_window_days": 5}).skip, values.CLICK_TOO_OLD)
        self.assertEqual(self.plan(self.won(), {**SETTINGS, "conversion_actions": {}}).skip,
                         values.PIPELINE_UNMAPPED)
        open_eng = engagements.create(company=self.company, party=self.ada)
        self.assertEqual(self.plan(open_eng).skip, values.NOT_WON)

    def test_null_amount_has_no_value_and_currencies_are_not_converted(self):
        planned = self.plan(self.won(value=None))
        self.assertNotIn("conversionValue", planned.row)
        self.assertTrue(planned.value_unknown)
        self.assertEqual(self.plan(self.won(currency="EUR")).row["currencyCode"], "EUR")

    def test_consent_override(self):
        self.assertEqual(self.plan(self.won()).row["consent"],
                         {"adUserData": "UNSPECIFIED"})
        planned = self.plan(self.won(), {**SETTINGS, "ad_user_data_consent": "DENIED"})
        self.assertEqual(planned.row["consent"], {"adUserData": "DENIED"})
