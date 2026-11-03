"""Fixture tables for phase2-party.md 7.2 (email) and 7.3 (phone), plus the
name rules of 1.1 and 3.3. Each table row is one subTest; a failing row
names its input."""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.test import SimpleTestCase, override_settings

from parties import normalize
from parties.constants import LINE_LANDLINE, LINE_MOBILE, LINE_UNKNOWN
from parties.normalize import (
    default_phone_region,
    normalize_domain,
    normalize_email,
    normalize_name,
    normalize_org_name,
    normalize_phone,
    person_display_name,
    phone_line_type,
)

# (input, expected) — spec 7.2: strip; split at the last @; local part NFKC
# then casefold(); domain lower-cased and IDNA-encoded; no dot/plus folding.
EMAIL_FIXTURES = [
    # case and whitespace
    ("Alice@Example.com", "alice@example.com"),
    ("  bob@example.com \t\n", "bob@example.com"),
    ("ALICE.SMITH@EXAMPLE.COM", "alice.smith@example.com"),
    # no dot or plus folding (3.3)
    ("a.lice+news@example.com", "a.lice+news@example.com"),
    ("first.last@gmail.com", "first.last@gmail.com"),
    ("firstlast+x@gmail.com", "firstlast+x@gmail.com"),
    # ß: casefold() turns the local part's ß into ss; IDNA 2003 nameprep
    # does the same to the domain
    ("Straße@example.com", "strasse@example.com"),
    ("STRASSE@example.com", "strasse@example.com"),
    ("hans@straße.de", "hans@strasse.de"),
    # Turkish dotless i (U+0131) stays distinct from i; dotted capital İ
    # (U+0130) casefolds to i + combining dot above (U+0307), not plain i
    ("ılker@example.com", "ılker@example.com"),
    ("ILKER@example.com", "ilker@example.com"),
    ("İlker@example.com", "i̇lker@example.com"),
    # full-width characters: NFKC folds them to ASCII (local part and domain)
    ("ｊｏｈｎ@example.com", "john@example.com"),
    ("john@ｅｘａｍｐｌｅ.com", "john@example.com"),
    ("ＪＯＨＮ@example.com", "john@example.com"),
    # compatibility ligature (NFKC)
    ("ﬁona@example.com", "fiona@example.com"),
    # IDN domains: Unicode and punycode spellings agree
    ("user@ÜBER.example", "user@xn--ber-goa.example"),
    ("user@über.example", "user@xn--ber-goa.example"),
    ("user@xn--ber-goa.example", "user@xn--ber-goa.example"),
    ("user@XN--BER-GOA.EXAMPLE", "user@xn--ber-goa.example"),
    # a trailing root dot names the same domain
    ("a@example.com.", "a@example.com"),
    ("kontakt@bücher.example", "kontakt@xn--bcher-kva.example"),
    ("info@ıstanbul.tr", "info@xn--stanbul-qfb.tr"),
    # Unicode local part keeps its script (mixed-script look-alikes are not folded; 7.1)
    ("Зоя@example.com", "зоя@example.com"),
    ("zоя@example.com", "zоя@example.com"),  # Latin z + Cyrillic о, я
]

# Inputs rejected with ValidationError (7.2 step 1, plus an unusable domain).
EMAIL_REJECTS = [
    "",
    "   ",
    "no-at-sign.example.com",
    "two@@example.com",
    "a@b@example.com",
    "@example.com",
    "user@",
    "user@ ",
    "user@exa..mple.com",
    "ｊｏｈｎ＠example.com",
    "a b@example.com",  # whitespace inside the address
    " a @example.com",
    "a@exa mple.com",
    "x" * 250 + "@ab.com",  # 257 characters: longer than email_normalized
    "ß" * 125 + "@example.com",  # fits as entered (137), not once ß -> ss (262)  # full-width @ is not an @ until NFKC, which runs after the split
    None,
]


class EmailFixtureTests(SimpleTestCase):
    def test_fixture_table(self):
        for raw, expected in EMAIL_FIXTURES:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_email(raw), expected)

    def test_idempotent(self):
        for raw, expected in EMAIL_FIXTURES:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_email(expected), expected)

    def test_rejects(self):
        for raw in EMAIL_REJECTS:
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                normalize_email(raw)

    def test_dot_and_plus_variants_stay_distinct(self):
        forms = {normalize_email(v) for v in ("ab@example.com", "a.b@example.com", "ab+x@example.com")}
        self.assertEqual(len(forms), 3)

    def test_spec_alias(self):
        self.assertIs(normalize.email, normalize_email)

    def test_domain_helper(self):
        self.assertEqual(normalize_domain(" Example.COM "), "example.com")
        self.assertEqual(normalize_domain("ÜBER.example"), "xn--ber-goa.example")
        self.assertEqual(normalize_domain("Example.com."), "example.com")
        for bad in ("", ".", "a..b", "x" * 64 + ".example", "exa mple.com"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                normalize_domain(bad)


# (raw, region, (e164, extension)) — spec 7.3: phonenumbers.parse(raw,
# region); not possible -> blank; extensions split out. region=None means
# GOLDEN_DEFAULT_PHONE_REGION (default "US").
PHONE_FIXTURES = [
    # US national formats
    ("(202) 555-0143", "US", ("+12025550143", "")),
    ("202.555.0143", "US", ("+12025550143", "")),
    ("2025550143", "US", ("+12025550143", "")),
    ("1-202-555-0143", "US", ("+12025550143", "")),
    ("+1 202 555 0143", "US", ("+12025550143", "")),
    # region=None and blank/unknown regions fall back to the default
    ("(202) 555-0143", None, ("+12025550143", "")),
    ("(202) 555-0143", "", ("+12025550143", "")),
    ("(202) 555-0143", "ZZ", ("+12025550143", "")),
    ("(202) 555-0143", "us", ("+12025550143", "")),
    # extensions
    ("(202) 555-0143 x123", "US", ("+12025550143", "123")),
    ("202-555-0143 ext. 45", "US", ("+12025550143", "45")),
    ("202-555-0143 ext 45", "US", ("+12025550143", "45")),
    ("+1 202 555 0143;ext=9", "US", ("+12025550143", "9")),
    # international: the + prefix wins over the region
    ("+44 20 7946 0958", "US", ("+442079460958", "")),
    ("+49 30 123456", "US", ("+4930123456", "")),
    # national formats in their own region
    ("020 7946 0958", "GB", ("+442079460958", "")),
    ("07700 900123", "GB", ("+447700900123", "")),
    ("030 123456", "DE", ("+4930123456", "")),
    ("0049 30 123456", "DE", ("+4930123456", "")),
    # full-width digits
    ("２０２５５５０１４３", "US", ("+12025550143", "")),
    # not possible numbers -> blank
    ("12", "US", ("", "")),
    ("555-0143", "US", ("", "")),  # local-only: no area code, E.164 would be wrong
    ("+1 202 555 0143 0000 0000", "US", ("", "")),
    ("+4930123456789012", "US", ("", "")),  # 17-char E.164: phonenumbers allows it, the column does not
    ("+81 " + "1" * 17, "US", ("", "")),
    # extensions longer than PartyPhone.extension (10) are dropped, the number kept
    ("+1 4155550143 ext 12345678901", "US", ("+14155550143", "")),
    ("+1 4155550143 ext 1234567890", "US", ("+14155550143", "1234567890")),
    # unparseable -> blank
    ("abc", "US", ("", "")),
    ("", "US", ("", "")),
    ("   ", "US", ("", "")),
    (None, "US", ("", "")),
]


class PhoneFixtureTests(SimpleTestCase):
    def test_fixture_table(self):
        for raw, region, expected in PHONE_FIXTURES:
            with self.subTest(raw=raw, region=region):
                self.assertEqual(normalize_phone(raw, region), expected)

    def test_e164_round_trips(self):
        for raw, region, (e164, _ext) in PHONE_FIXTURES:
            if e164:
                with self.subTest(e164=e164):
                    self.assertEqual(normalize_phone(e164, region), (e164, ""))

    def test_e164_fits_the_column(self):
        for raw, region, (e164, _ext) in PHONE_FIXTURES:
            with self.subTest(raw=raw):
                self.assertLessEqual(len(e164), 16)  # PartyPhone.number_e164 is CharField(16)
                self.assertLessEqual(len(_ext), 10)  # PartyPhone.extension is CharField(10)

    def test_spec_alias(self):
        self.assertIs(normalize.phone, normalize_phone)

    @override_settings(GOLDEN_DEFAULT_PHONE_REGION="GB")
    def test_default_region_setting(self):
        self.assertEqual(default_phone_region(), "GB")
        self.assertEqual(normalize_phone("020 7946 0958"), ("+442079460958", ""))
        self.assertEqual(normalize_phone("020 7946 0958", None), ("+442079460958", ""))
        # an explicit region (the address country) still wins
        self.assertEqual(normalize_phone("(202) 555-0143", "US"), ("+12025550143", ""))

    @override_settings(GOLDEN_DEFAULT_PHONE_REGION="gb")
    def test_default_region_is_upper_cased(self):
        self.assertEqual(default_phone_region(), "GB")

    def test_default_region_defaults_to_us(self):
        with self.settings():
            from django.conf import settings

            del settings.GOLDEN_DEFAULT_PHONE_REGION
            self.assertEqual(default_phone_region(), "US")

    def test_bad_default_region(self):
        for bad in ("", "XX", "USA", None, 1):
            with self.subTest(bad=bad), self.settings(GOLDEN_DEFAULT_PHONE_REGION=bad):
                with self.assertRaises(ImproperlyConfigured):
                    default_phone_region()
                # a bad default only matters when the caller has no usable region
                self.assertEqual(normalize_phone("020 7946 0958", "GB"), ("+442079460958", ""))
                with self.assertRaises(ImproperlyConfigured):
                    normalize_phone("020 7946 0958")


# (e164, line_type): mobile/landline where phonenumbers knows, else unknown.
LINE_TYPE_FIXTURES = [
    ("+447911123456", LINE_MOBILE),
    ("+491701234567", LINE_MOBILE),
    ("+447700900123", LINE_UNKNOWN),  # Ofcom drama range: possible, not valid
    ("+442079460958", LINE_LANDLINE),
    ("+4930123456", LINE_LANDLINE),
    ("+12025550143", LINE_UNKNOWN),  # NANP: fixed-line-or-mobile
    ("", LINE_UNKNOWN),
    ("not a number", LINE_UNKNOWN),
]


class LineTypeTests(SimpleTestCase):
    def test_fixture_table(self):
        for e164, expected in LINE_TYPE_FIXTURES:
            with self.subTest(e164=e164):
                self.assertEqual(phone_line_type(e164), expected)


class NameTests(SimpleTestCase):
    def test_person_display_name(self):
        cases = [
            (("Ada", "Lovelace"), "Ada Lovelace"),
            (("  Ada  ", " Lovelace "), "Ada Lovelace"),
            (("Mary  Ann", "van der Berg"), "Mary Ann van der Berg"),
            (("Ada", ""), "Ada"),
            (("", "Lovelace"), "Lovelace"),
            (("", ""), ""),
            ((None, None), ""),
            (("José", "Ñúñez"), "José Ñúñez"),  # display text keeps case and accents
        ]
        for (given, family), expected in cases:
            with self.subTest(given=given, family=family):
                self.assertEqual(person_display_name(given, family), expected)

    def test_normalize_name(self):
        cases = [
            ("Lovelace", "lovelace"),
            ("  LOVE   lace ", "love lace"),
            ("Straße", "strasse"),
            ("ＡＤＡ", "ada"),
            ("José", "josé"),  # accents are kept: José and Jose differ
            ("O'Brien", "o'brien"),
            ("", ""),
            (None, ""),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_name(raw), expected)

    def test_normalize_org_name(self):
        cases = [
            ("Acme Widgets, Inc.", "acme widgets"),
            ("ACME WIDGETS INC", "acme widgets"),
            ("Acme Widgets", "acme widgets"),
            ("Acme Widgets L.L.C.", "acme widgets"),
            ("Acme Widgets, LLC", "acme widgets"),
            ("Acme Widgets Ltd.", "acme widgets"),
            ("Acme Widgets GmbH", "acme widgets"),
            ("Acme Widgets Ltd Inc", "acme widgets"),  # repeated suffixes
            ("Inc Solutions", "inc solutions"),  # only trailing suffixes go
            ("Acme-Widgets & Co.", "acme widgets co"),
            ("Inc.", "inc"),  # never collapses to an empty key
            ("Müller GmbH", "müller"),
            ("", ""),
            (None, ""),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_org_name(raw), expected)
