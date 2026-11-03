"""Time zones on Company and UserProfile + core.tz.resolve (phase3 step 2)."""

from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from core import tz
from core.models import Company, UserProfile
from core.serializers import CompanySerializer, UserProfileSerializer

INVALID = ["Mars/Olympus", "europe/berlin", "EST5EDT ", "GMT+2", "../etc/passwd", "x" * 64,
           "localtime", "Factory"]


class ValidateTimezoneTests(SimpleTestCase):
    def test_known_zones_pass(self):
        for name in ["UTC", "Europe/Berlin", "America/New_York", "Asia/Kolkata"]:
            with self.subTest(name=name):
                tz.validate_timezone(name)
                self.assertTrue(tz.is_valid_timezone(name))

    def test_unknown_zones_refused(self):
        for name in INVALID:
            with self.subTest(name=name):
                with self.assertRaises(ValidationError) as ctx:
                    tz.validate_timezone(name)
                self.assertEqual(ctx.exception.code, "invalid_timezone")

    def test_non_strings_are_not_zones(self):
        self.assertFalse(tz.is_valid_timezone(None))
        self.assertFalse(tz.is_valid_timezone(ZoneInfo("UTC")))


class ModelCleanTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")

    def test_company_clean_refuses_invalid_zone(self):
        self.company.timezone = "Mars/Olympus"
        with self.assertRaises(ValidationError) as ctx:
            self.company.full_clean()
        self.assertIn("timezone", ctx.exception.message_dict)

    def test_profile_clean_refuses_invalid_zone(self):
        profile = UserProfile(company=self.company, display_name="A", timezone="Nowhere/City")
        with self.assertRaises(ValidationError) as ctx:
            profile.full_clean()
        self.assertIn("timezone", ctx.exception.message_dict)

    def test_overlong_zone_refused(self):
        self.company.timezone = "Europe/" + "x" * 60
        with self.assertRaises(ValidationError) as ctx:
            self.company.full_clean()
        self.assertIn("timezone", ctx.exception.message_dict)

    def test_valid_and_blank_zones_clean(self):
        self.company.timezone = "Europe/Berlin"
        self.company.full_clean()
        profile = UserProfile(company=self.company, display_name="A")
        profile.full_clean()  # blank is allowed: falls through to the company
        profile.timezone = "Asia/Tokyo"
        profile.full_clean()


class SerializerTests(TestCase):
    def test_company_serializer_refuses_invalid_zone(self):
        s = CompanySerializer(data={"name": "Acme", "timezone": "Mars/Olympus"})
        self.assertFalse(s.is_valid())
        self.assertIn("timezone", s.errors)

    def test_company_serializer_accepts_valid_zone(self):
        s = CompanySerializer(data={"name": "Acme", "timezone": "Europe/Berlin"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.save().timezone, "Europe/Berlin")

    def test_profile_serializer_refuses_invalid_zone(self):
        s = UserProfileSerializer(data={"display_name": "A", "timezone": "europe/berlin"})
        self.assertFalse(s.is_valid())
        self.assertIn("timezone", s.errors)

    def test_profile_serializer_partial_update(self):
        company = Company.objects.create(name="Acme")
        profile = UserProfile.objects.create(company=company, display_name="A")
        bad = UserProfileSerializer(profile, data={"timezone": "Not/AZone"}, partial=True)
        self.assertFalse(bad.is_valid())
        good = UserProfileSerializer(profile, data={"timezone": "America/Chicago"}, partial=True)
        self.assertTrue(good.is_valid(), good.errors)
        good.save()
        profile.refresh_from_db()
        self.assertEqual(profile.timezone, "America/Chicago")


@override_settings(TIME_ZONE="America/New_York")
class ResolveTests(SimpleTestCase):
    """Order: profile (the assignee's), company, settings.TIME_ZONE, UTC."""

    def _company(self, zone=""):
        return Company(name="Acme", timezone=zone)

    def _profile(self, zone="", company=None):
        return UserProfile(display_name="A", timezone=zone, company=company)

    def test_profile_zone_wins(self):
        company = self._company("Europe/Berlin")
        profile = self._profile("Asia/Tokyo", company)
        self.assertEqual(tz.resolve(profile, company), ZoneInfo("Asia/Tokyo"))

    def test_company_zone_when_profile_blank(self):
        company = self._company("Europe/Berlin")
        self.assertEqual(tz.resolve(self._profile("", company), company), ZoneInfo("Europe/Berlin"))

    def test_settings_zone_when_both_blank(self):
        company = self._company("")
        self.assertEqual(tz.resolve(self._profile("", company), company), ZoneInfo("America/New_York"))

    def test_no_profile_uses_company(self):
        self.assertEqual(tz.resolve(None, self._company("Europe/Paris")), ZoneInfo("Europe/Paris"))

    def test_nothing_given_uses_settings(self):
        self.assertEqual(tz.resolve(None, None), ZoneInfo("America/New_York"))

    def test_company_defaults_to_profiles_company(self):
        profile = self._profile("", self._company("Australia/Sydney"))
        self.assertEqual(tz.resolve(profile, None), ZoneInfo("Australia/Sydney"))

    def test_profile_without_company_falls_to_settings(self):
        self.assertEqual(tz.resolve(self._profile(""), None), ZoneInfo("America/New_York"))

    def test_explicit_company_beats_profiles_company(self):
        # The caller names the tenant the task belongs to; the profile's own
        # company only fills in when none is given.
        profile = self._profile("", self._company("Australia/Sydney"))
        self.assertEqual(tz.resolve(profile, self._company("Europe/Rome")), ZoneInfo("Europe/Rome"))

    def test_stale_stored_zone_is_skipped_not_raised(self):
        # Rows written by update() skip validation; resolve must not crash.
        company = self._company("Europe/Berlin")
        self.assertEqual(tz.resolve(self._profile("Mars/Olympus", company), company),
                         ZoneInfo("Europe/Berlin"))
        self.assertEqual(tz.resolve(None, self._company("Nope/Nope")), ZoneInfo("America/New_York"))

    @override_settings(TIME_ZONE="Not/AZone")
    def test_utc_fallback_when_settings_zone_unknown(self):
        self.assertEqual(tz.resolve(None, None), tz.UTC)
        self.assertEqual(tz.resolve(self._profile(""), self._company("")), ZoneInfo("UTC"))

    @override_settings(TIME_ZONE=None)
    def test_utc_fallback_when_settings_zone_unset(self):
        self.assertEqual(tz.resolve(None, None), tz.UTC)
