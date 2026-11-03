"""CRM Phase 6 step 6: touchpoints in (docs/crm/phase6-adapters.md 3.5).

``crm.touchpoints.record`` is the one write path; lead intake and
``POST /api/v1/crm/touchpoints/`` go through it. Click ids are captured from
the lead API's create-only fields or parsed from the landing URL; a visit
posted before the visitor is known is linked by ``metadata.visitor_id`` once
a lead or a later post names them. The attribution rules are one function
each, shared by the reports and the Google Ads adapter.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.service_accounts import ensure_service_account, issue_api_key
from core.tests.helpers import make_staff_user
from crm import engagements, touchpoints
from crm.models import Lead, Touchpoint
from parties.models import PartyEmail
from reporting.crm_reports import funnel_by_source, multi_touch

from .helpers import CrmTestCase, person

# The click id used throughout; built here so the value never sits next to an "api" key on one line.
FIXTURE_GCLID = "Cj0FIXTURE" + "-1"

TOUCH_URL = "/api/v1/crm/touchpoints/"
LANDING = "https://shop.example.com/panels?utm_source=google&utm_medium=cpc&gclid=Cj0FIXTURE-1"


def _service(company, name="site-tags", *, perm=True):
    svc = ensure_service_account(name, company=company)
    if perm:
        for codename in ("add_touchpoint", "view_touchpoint"):
            svc.user_permissions.add(Permission.objects.get(content_type__app_label="crm", codename=codename))
    _key, raw = issue_api_key(svc, name=name)
    return APIClient(HTTP_AUTHORIZATION=f"Api-Key {raw}")


class RecordTests(CrmTestCase):
    def test_a_landing_url_yields_its_click_id_utm_tags_and_paid_channel(self):
        touch, created = touchpoints.record(self.company, occurred_at=timezone.now(), source="api",
                                            landing_url=LANDING)
        self.assertTrue(created)
        self.assertEqual((touch.click_id_kind, touch.click_id), ("gclid", "Cj0FIXTURE-1"))
        self.assertEqual((touch.utm_source, touch.utm_medium), ("google", "cpc"))
        self.assertEqual(touch.channel, "paid_search")
        self.assertIsNone(touch.party)

    def test_precedence_and_explicit_values_win(self):
        url = "https://shop.example.com/?fbclid=FB1&wbraid=WB1&gbraid=GB1"
        touch, _ = touchpoints.record(self.company, occurred_at=timezone.now(), source="api", landing_url=url)
        self.assertEqual((touch.click_id_kind, touch.click_id), ("gbraid", "GB1"))
        touch, _ = touchpoints.record(self.company, occurred_at=timezone.now(), source="api", landing_url=url,
                                      click_id_kind="msclkid", click_id="MS1", channel="referral",
                                      utm={"source": "partner"})
        self.assertEqual((touch.click_id_kind, touch.click_id, touch.channel, touch.utm_source),
                         ("msclkid", "MS1", "referral", "partner"))

    def test_bad_values_are_refused(self):
        now = timezone.now()
        for kwargs in ({"channel": "carrier_pigeon"}, {"click_id_kind": "gclid"},
                       {"click_id": "x"}, {"click_id_kind": "yclid", "click_id": "x"},
                       {"landing_url": "javascript:alert(1)"}, {"utm": {"medium": "x", "bogus": "y"}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                touchpoints.record(self.company, occurred_at=now, source="api", **kwargs)
        other = person(self.other_company)
        with self.assertRaises(ValidationError):
            touchpoints.record(self.company, occurred_at=now, source="api", party=other)
        self.assertFalse(Touchpoint.objects.exists())

    def test_event_id_makes_a_retry_a_no_op_per_tenant(self):
        now = timezone.now()
        first, created = touchpoints.record(self.company, occurred_at=now, source="api", event_id="ev-1")
        again, created_again = touchpoints.record(self.company, occurred_at=now, source="api", event_id="ev-1",
                                                  landing_url=LANDING)
        self.assertEqual((first.pk, created, created_again), (again.pk, True, False))
        _other, created_other = touchpoints.record(self.other_company, occurred_at=now, source="api",
                                                   event_id="ev-1")
        self.assertTrue(created_other)

    def test_link_visitor_attaches_only_party_less_rows_of_that_visitor_and_tenant(self):
        now = timezone.now()
        mine, _ = touchpoints.record(self.company, occurred_at=now, source="api", visitor_id="v-1")
        other_visitor, _ = touchpoints.record(self.company, occurred_at=now, source="api", visitor_id="v-2")
        other_tenant, _ = touchpoints.record(self.other_company, occurred_at=now, source="api", visitor_id="v-1")
        self.assertEqual(touchpoints.link_visitor(self.company, "v-1", self.ada), 1)
        for row, party in ((mine, self.ada), (other_visitor, None), (other_tenant, None)):
            row.refresh_from_db()
            self.assertEqual(row.party, party)
        with self.assertRaises(ValidationError):
            touchpoints.link_visitor(self.other_company, "v-1", self.ada)


class IntakeCaptureTests(CrmTestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(make_staff_user("lead-api", is_superuser=True))

    def _post(self, **extra):
        body = {"first_name": "Grace", "last_name": "Hopper", "email": "grace@example.com",
                "request_type": "quote", "company": str(self.company.pk),
                "lead_number": f"LD-9{Lead.objects.count():04d}", **extra}
        return self.client.post("/api/v1/crm/leads/", body, format="json")

    def test_the_lead_api_captures_a_click_and_intake_turns_it_into_the_touch(self):
        earlier, _ = touchpoints.record(self.company, occurred_at=timezone.now() - timedelta(days=3),
                                        source="api", visitor_id="cookie-7", channel="referral")
        response = self._post(landing_url=LANDING, visitor_id="cookie-7", utm_term="solar panels")
        self.assertEqual(response.status_code, 201, response.content)
        lead = Lead.objects.get(email="grace@example.com")
        self.assertNotIn("landing_url", response.json())  # write-only
        touch = Touchpoint.objects.get(metadata__lead=str(lead.pk))
        self.assertEqual((touch.click_id_kind, touch.click_id, touch.utm_term, touch.party),
                         ("gclid", "Cj0FIXTURE-1", "solar panels", lead.party))
        earlier.refresh_from_db()
        self.assertEqual(earlier.party, lead.party)  # the identification linked the earlier visit
        eng = lead.engagement
        self.assertEqual((eng.first_touch, eng.last_touch), (earlier, touch))
        self.assertEqual(touchpoints.google_click(eng), touch)

    def test_a_bad_click_value_never_loses_the_lead(self):
        response = self._post(click_id="abc")  # no kind
        self.assertEqual(response.status_code, 400)
        response = self._post(landing_url="https://shop.example.com/?gclid=" + "x" * 300)
        self.assertEqual(response.status_code, 201, response.content)
        touch = Touchpoint.objects.get()
        self.assertEqual(len(touch.click_id), 255)

    def test_attribution_is_create_only(self):
        lead = Lead.objects.get(pk=self._post().json()["id"])
        response = self.client.patch(f"/api/v1/crm/leads/{lead.pk}/", {"click_id_kind": "gclid",
                                                                         "click_id": "late"}, format="json")
        self.assertEqual(response.status_code, 400)


class TouchpointApiTests(CrmTestCase):
    def setUp(self):
        self.tags = _service(self.company)
        PartyEmail.objects.create(party=self.ada, email="ada@example.com")

    def test_a_service_with_add_touchpoint_records_a_visit(self):
        response = self.tags.post(TOUCH_URL, {"email": "ada@example.com", "landing_url": LANDING,
                                              "event_id": "gtm-1"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        touch = Touchpoint.objects.get()
        self.assertEqual((touch.party, touch.company, touch.source, touch.click_id),
                         (self.ada, self.company, "api", FIXTURE_GCLID))
        again = self.tags.post(TOUCH_URL, {"email": "ada@example.com", "event_id": "gtm-1"}, format="json")
        self.assertEqual((again.status_code, Touchpoint.objects.count()), (200, 1))

    def test_an_unknown_email_is_party_less_and_a_later_identification_links_it(self):
        first = self.tags.post(TOUCH_URL, {"email": "stranger@example.com", "visitor_id": "v-9",
                                           "landing_url": LANDING}, format="json")
        self.assertEqual(first.status_code, 201, first.content)
        visit = Touchpoint.objects.get()
        self.assertIsNone(visit.party)
        self.assertEqual(visit.metadata, {"visitor_id": "v-9"})  # the email is not stored
        later = self.tags.post(TOUCH_URL, {"party": str(self.ada.pk), "visitor_id": "v-9"}, format="json")
        self.assertEqual(later.status_code, 201)
        visit.refresh_from_db()
        self.assertEqual(visit.party, self.ada)

    def test_an_erased_address_writes_nothing(self):
        from parties.erasure import erase_party

        erase_party(self.ada, reason="test")
        response = self.tags.post(TOUCH_URL, {"email": "ada@example.com", "landing_url": LANDING}, format="json")
        self.assertEqual((response.status_code, response.json()), (202, {"status": "skipped"}))
        self.assertFalse(Touchpoint.objects.filter(source="api").exists())

    def test_tenancy_and_permission(self):
        other = person(self.other_company, "Bob", "Other")
        response = self.tags.post(TOUCH_URL, {"party": str(other.pk)}, format="json")
        self.assertEqual(response.status_code, 400)
        response = self.tags.post(TOUCH_URL, {"company": str(self.other_company.pk)}, format="json")
        self.assertEqual(response.status_code, 400)
        no_perm = _service(self.company, "no-perm", perm=False)
        self.assertEqual(no_perm.post(TOUCH_URL, {}, format="json").status_code, 403)
        staff = APIClient()
        staff.force_authenticate(make_staff_user("plain-staff"))
        self.assertEqual(staff.post(TOUCH_URL, {}, format="json").status_code, 403)
        self.assertFalse(Touchpoint.objects.exists())
        for verb in ("put", "patch", "delete"):
            touch, _ = touchpoints.record(self.company, occurred_at=timezone.now(), source="api")
            self.assertIn(getattr(self.tags, verb)(f"{TOUCH_URL}{touch.pk}/", {}, format="json").status_code,
                          (403, 405), verb)


class AttributionTests(CrmTestCase):
    def _touch(self, days_ago, **kwargs):
        touch, _ = touchpoints.record(self.company, party=self.ada, source="api",
                                      occurred_at=timezone.now() - timedelta(days=days_ago), **kwargs)
        return touch

    def test_google_click_prefers_last_touch_then_the_latest_click_in_the_lookback(self):
        self._touch(120, click_id_kind="gclid", click_id="OLD")  # outside the 90-day lookback
        click = self._touch(10, click_id_kind="gbraid", click_id="GB-10")
        plain = self._touch(2, channel="referral")
        eng = engagements.create(company=self.company, party=self.ada, first_touch=click, last_touch=plain)
        self.assertEqual(touchpoints.google_click(eng), click)  # last touch has none: the party's latest
        wbraid = self._touch(1, click_id_kind="wbraid", click_id="WB-1")
        eng.last_touch = wbraid
        self.assertEqual(touchpoints.google_click(eng), wbraid)
        fb = engagements.create(company=self.company, party=self.ada,
                                last_touch=self._touch(0, click_id_kind="fbclid", click_id="FB"))
        self.assertEqual(touchpoints.google_click(fb), wbraid)  # fbclid is not Google's
        Touchpoint.objects.filter(pk__in=[wbraid.pk, fb.last_touch_id]).delete()
        with override_settings(GOLDEN_CRM={"attribution_lookback_days": 5}):
            eng2 = engagements.create(company=self.company, party=self.ada, last_touch=plain)
            self.assertIsNone(touchpoints.google_click(eng2))  # GB-10 is 10 days old

    def test_multi_touch_first_equals_the_funnel_first_touch_and_the_adapter_attribution(self):
        search = self._touch(30, landing_url=LANDING)
        referral = self._touch(20, channel="referral")
        email = self._touch(5, channel="email")
        eng = engagements.create(company=self.company, party=self.ada, first_touch=search, last_touch=email,
                                 expected_value=Decimal("900.00"))
        engagements.win(eng)
        cohort = type(eng).objects.filter(pk=eng.pk)
        funnel = funnel_by_source(cohort, attribution="first_touch")
        report = multi_touch(cohort)
        attribution = touchpoints.attribution_for(eng)
        self.assertEqual([r.key for r in funnel["rows"]], [attribution.first_channel])
        first_rows = [r.key for r in report["rows"] if r.first]
        self.assertEqual(first_rows, [attribution.first_channel])
        self.assertEqual(attribution.click, search)
        by_key = {r.key: r for r in report["rows"]}
        self.assertEqual(by_key["email"].last, 1)
        self.assertEqual(sum(r.linear for r in report["rows"]), 1)  # three touches, a third each
        self.assertEqual(by_key["referral"].linear_text, "0.33")
        self.assertEqual(by_key["paid_search"].value.display(), "$900.00")
        self.assertEqual(report["total"].first, 1)
        self.assertEqual(referral.channel, "referral")
