"""Normalisation for party matching (docs/crm/phase2-party.md 7.2, 7.3, 3.3).

Every matcher, dedupe rule and (from identity step 3) the Customer email
lower-casing reads the output of these functions, so they are the single
definition of "the same address". Pure functions: no database access.

- ``normalize_email`` (alias ``email``): 7.2. No dot or plus folding.
- ``normalize_phone`` (alias ``phone``): 7.3, E.164 plus extension.
- ``phone_line_type``: the PartyPhone.line_type the library can infer.
- ``normalize_domain``: lower-case IDNA, as the email domain and
  ``Party.primary_domain`` store it.
- ``normalize_name`` / ``normalize_org_name``: comparison keys for dedupe
  rules R2, R4 and R5. Never stored as display text.
- ``person_display_name``: the derived ``Party.display_name`` of a person.
"""

from __future__ import annotations

import re
import unicodedata

import phonenumbers
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError

from .constants import LINE_LANDLINE, LINE_MOBILE, LINE_UNKNOWN

DEFAULT_PHONE_REGION = "US"

_WHITESPACE_RE = re.compile(r"\s+")

# Column widths the step-3 models give these values (spec 1.4).
EMAIL_MAX_LENGTH = 254   # PartyEmail.email_normalized
E164_MAX_LENGTH = 16     # PartyPhone.number_e164 ("+" and E.164's 15 digits)
EXTENSION_MAX_LENGTH = 10  # PartyPhone.extension


# --- email (7.2) -----------------------------------------------------------


def normalize_domain(value: str) -> str:
    """Lower-case and IDNA-encode a domain (``ÜBER.example`` -> ``xn--ber-goa.example``).

    Uses Python's ``idna`` codec (IDNA 2003, as 7.2 specifies). Its nameprep
    step applies NFKC and case folding, so full-width letters fold to ASCII
    and ``ß`` becomes ``ss``. Raises ``ValidationError`` on an empty or
    unencodable domain.
    """
    domain = (value or "").strip().lower()
    if not domain:
        raise ValidationError("Enter a domain.", code="invalid_domain")
    try:
        encoded = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValidationError(f"{value!r} is not a valid domain.", code="invalid_domain") from exc
    # A trailing root dot names the same domain: drop it, so "x.com." == "x.com".
    encoded = encoded.rstrip(".")
    if not encoded or not all(encoded.split(".")) or _WHITESPACE_RE.search(encoded):
        raise ValidationError(f"{value!r} is not a valid domain.", code="invalid_domain")
    return encoded


def normalize_email(value: str) -> str:
    """The matching form of an email address (spec 7.2).

    1. Strip whitespace; exactly one ``@`` or ``ValidationError``.
    2. Split at the last ``@``: the local part is NFKC-normalised then
       ``casefold()``-ed; the domain goes through ``normalize_domain``.
    3. No dot or plus folding (3.3): ``a.b+x@`` stays distinct from ``ab@``.

    Also rejects whitespace inside the address and a result longer than
    254 characters (the ``email_normalized`` column), since the backfill
    may call this without a form field's validation in front of it.
    """
    if not isinstance(value, str):
        raise ValidationError("Enter an email address.", code="invalid_email")
    stripped = value.strip()
    if stripped.count("@") != 1:
        raise ValidationError(
            f"{value!r} is not an email address: it needs exactly one '@'.", code="invalid_email"
        )
    local, _, domain = stripped.rpartition("@")
    if not local:
        raise ValidationError(f"{value!r} has nothing before the '@'.", code="invalid_email")
    try:
        domain = normalize_domain(domain)
    except ValidationError as exc:
        raise ValidationError(f"{value!r} has an invalid domain.", code="invalid_email") from exc
    result = f"{unicodedata.normalize('NFKC', local).casefold()}@{domain}"
    if _WHITESPACE_RE.search(result):
        raise ValidationError(f"{value!r} contains whitespace.", code="invalid_email")
    if len(result) > EMAIL_MAX_LENGTH:
        raise ValidationError(
            f"{value!r} is longer than {EMAIL_MAX_LENGTH} characters once normalized.",
            code="invalid_email",
        )
    return result


# --- phone (7.3) -----------------------------------------------------------


def default_phone_region() -> str:
    """``settings.GOLDEN_DEFAULT_PHONE_REGION``, validated (default ``"US"``).

    Raises ``ImproperlyConfigured`` unless it is a region phonenumbers knows.
    Called from ``PartiesConfig.ready()`` so bad config fails at boot.
    """
    region = getattr(settings, "GOLDEN_DEFAULT_PHONE_REGION", DEFAULT_PHONE_REGION)
    if not isinstance(region, str) or region.upper() not in phonenumbers.SUPPORTED_REGIONS:
        raise ImproperlyConfigured(
            f"GOLDEN_DEFAULT_PHONE_REGION={region!r} is not an ISO 3166-1 alpha-2 region "
            "that phonenumbers supports (e.g. 'US', 'GB', 'DE')."
        )
    return region.upper()


def _region(region: str | None) -> str:
    """The address country when known (and supported), else the default."""
    if isinstance(region, str) and region.strip().upper() in phonenumbers.SUPPORTED_REGIONS:
        return region.strip().upper()
    return default_phone_region()


def normalize_phone(raw: str | None, region: str | None = None) -> tuple[str, str]:
    """``(e164, extension)`` for a raw phone string (spec 7.3).

    ``region`` is the ISO 3166-1 alpha-2 country used for national-format
    numbers: the address country when known; blank, ``None`` or an
    unsupported code falls back to ``GOLDEN_DEFAULT_PHONE_REGION``.
    Extensions (``x123``, ``ext. 123``, ``;ext=123``) are split out.

    Returns ``("", "")`` when the input cannot be parsed or is not a possible
    number. A number that is possible only as a local number (dialled without
    an area code, e.g. ``555-0143`` in the US) is also blank: its E.164 form
    would be wrong and would match unrelated parties. So is a number whose
    E.164 form exceeds 15 digits (phonenumbers accepts some longer national
    numbers). An extension longer than 10 digits is dropped; the caller
    keeps the raw text in ``number_raw``.
    """
    if raw is None:
        return "", ""
    text = str(raw).strip()
    if not text:
        return "", ""
    try:
        number = phonenumbers.parse(text, _region(region))
    except phonenumbers.NumberParseException:
        return "", ""
    reason = phonenumbers.is_possible_number_with_reason(number)
    if reason != phonenumbers.ValidationResult.IS_POSSIBLE:
        return "", ""
    e164 = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
    if len(e164) > E164_MAX_LENGTH:
        return "", ""
    extension = number.extension or ""
    if len(extension) > EXTENSION_MAX_LENGTH:
        extension = ""
    return e164, extension


_LINE_TYPES = {
    phonenumbers.PhoneNumberType.MOBILE: LINE_MOBILE,
    phonenumbers.PhoneNumberType.FIXED_LINE: LINE_LANDLINE,
}


def phone_line_type(e164: str) -> str:
    """``mobile`` / ``landline`` where phonenumbers knows, else ``unknown``.

    Takes the E.164 form ``normalize_phone`` returned. Numbers the library
    reports as fixed-line-or-mobile (the whole NANP, for one) are ``unknown``.
    ``fax`` is never inferred.
    """
    if not e164:
        return LINE_UNKNOWN
    try:
        number = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return LINE_UNKNOWN
    return _LINE_TYPES.get(phonenumbers.number_type(number), LINE_UNKNOWN)


# --- names (1.1, 3.3) -------------------------------------------------------


def _collapse(value: str | None) -> str:
    return _WHITESPACE_RE.sub(" ", value or "").strip()


def person_display_name(given_name: str | None, family_name: str | None) -> str:
    """The derived ``display_name`` of a person party (1.1).

    ``"<given> <family>"`` with whitespace collapsed; either part may be
    blank. Case and accents are kept: this is display text. Returns ``""``
    when both are blank (the caller then supplies a fallback).
    """
    return _collapse(f"{_collapse(given_name)} {_collapse(family_name)}")


def normalize_name(value: str | None) -> str:
    """Comparison key for person names (dedupe R2, R4).

    NFKC, ``casefold()``, whitespace collapsed. Accents and punctuation are
    kept: ``José`` and ``Jose`` are different names to the matcher.
    """
    return _collapse(unicodedata.normalize("NFKC", value or "").casefold())


# Legal-form suffixes stripped by R5. Compared after punctuation is removed,
# so "Inc." and "L.L.C." arrive as "inc" and "llc".
ORG_LEGAL_SUFFIXES = frozenset({"inc", "llc", "ltd", "gmbh"})
_JOINING_PUNCTUATION = re.compile(r"[.'\u2019]")  # "L.L.C." -> "llc", "O'Brien" -> "obrien"
_PUNCTUATION = re.compile(r"[^\w\s]|_")


def normalize_org_name(value: str | None) -> str:
    """Comparison key for organisation names (dedupe R5).

    ``normalize_name``, then punctuation removed (periods and apostrophes
    join, so ``L.L.C.`` is ``llc``; other marks split words) and trailing legal suffixes
    (``inc``, ``llc``, ``ltd``, ``gmbh``) stripped:
    ``"Acme Widgets, Inc."`` -> ``"acme widgets"``. A name that is only a
    suffix is kept as is, so it never collapses to the empty key.
    """
    joined = _JOINING_PUNCTUATION.sub("", normalize_name(value))
    words = _PUNCTUATION.sub(" ", joined).split()
    while len(words) > 1 and words[-1] in ORG_LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


# The spec's own names for the two contact-point normalisers (7.2, 7.3).
email = normalize_email
phone = normalize_phone

__all__ = [
    "DEFAULT_PHONE_REGION",
    "E164_MAX_LENGTH",
    "EMAIL_MAX_LENGTH",
    "EXTENSION_MAX_LENGTH",
    "ORG_LEGAL_SUFFIXES",
    "default_phone_region",
    "email",
    "normalize_domain",
    "normalize_email",
    "normalize_name",
    "normalize_org_name",
    "normalize_phone",
    "person_display_name",
    "phone",
    "phone_line_type",
]
