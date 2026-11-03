"""What a Customer, CustomerContact, CustomerAddress or Lead says about its
party (docs/crm/phase2-party.md 2.1, 2.5, section 4).

Pure functions over plain attribute access, so the data migration (historical
models), the live sync (``parties.sync``) and the drift check
(``parties.reconcile``) derive the same values from the same row and can
never disagree on normalisation.

"Account holder" (decision recorded in the step-5 PR): a Customer has an
account holder only when it has a real portal login, meaning ``Customer.user``
is set (identity step 8 dropped the legacy ``supabase_user_id``, which step 3
had copied into ``Customer.user`` + ``UserIdentity``). The step-4 report on the downstream treated every Customer as a
login holder, and 11 of its 15 clusters were blocked as
``two_account_holders`` for customers nobody can sign in as. A customer
without a login still gets a person party (its "customer person"), linked as
``Customer.party`` for an individual or through an ``employment``
relationship tagged with the customer for a business, but
``Customer.account_holder`` stays NULL until a login exists.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError

from .normalize import normalize_email, normalize_org_name, normalize_phone, person_display_name

# Persisted in PartyRelationship.source / PartyEmail.source / PartyPhone.source:
# never rename.
CUSTOMER_LINK_SOURCE = "customer-sync"  # customer person -> business organisation
CONTACT_LINK_SOURCE = "contact-sync"  # contact person -> customer.party
ADDRESS_SOURCE_BACKFILL = "backfill"
ADDRESS_SOURCE_SYNC = "customer-sync"
LEAD_ROLE_SOURCE = "lead"
STAFF_LINK_SOURCE = "staff-sync"
CUSTOMER_ROLE_SOURCE = "customer-sync"
MARKETABLE_SOURCE = "legacy:accepts_marketing"  # set by the backfill (D9); its reverse resets these
SYNC_MARKETABLE_SOURCE = "customer:accepts_marketing"  # set by the live sync

BUSINESS = "business"
NAME_MAX = 100  # Party.given_name / family_name


def customer_source(customer_pk) -> str:
    """``PartyEmail``/``PartyPhone.source`` of the row mirrored from a Customer."""
    return f"customer:{customer_pk}"


def contact_source(contact_pk) -> str:
    return f"contact:{contact_pk}"


def user_source(user_pk) -> str:
    return f"user:{user_pk}"


def has_login(customer) -> bool:
    """A real portal login: ``Customer.user`` is set.

    The ``getattr`` only matters when ``parties.0002`` replays on a historical
    model from before identity step 8, which still has the legacy column: a
    customer the identity backfill could not link counted as a login then.
    """
    return customer.user_id is not None or getattr(customer, "supabase_user_id", None) is not None


def is_business(customer) -> bool:
    return customer.customer_type == BUSINESS


def safe_email(raw) -> str:
    """Normalised address, or ``""`` when blank or invalid (the backfill
    keeps the raw value on the source row, writes no PartyEmail, reports it)."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return normalize_email(raw)
    except ValidationError:
        return ""


def phone_parts(raw, region) -> tuple[str, str, str]:
    """``(number_raw, e164, extension)``; e164 blank when unparseable (7.3)."""
    raw = (raw or "").strip()[:50]
    if not raw:
        return "", "", ""
    e164, ext = normalize_phone(raw, region)
    return raw, e164, ext


def person_names(first, last) -> tuple[str, str]:
    return (first or "").strip()[:NAME_MAX], (last or "").strip()[:NAME_MAX]


def person_display(first, last, fallback) -> str:
    return (person_display_name(first, last) or (fallback or "").strip() or "Unnamed")[:255]


def organisation_name(customer) -> tuple[str, bool]:
    """``(display_name, name_inferred)`` of a business customer's organisation."""
    name = (customer.company_name or "").strip()
    if name:
        return name[:255], False
    return person_display(customer.first_name, customer.last_name, customer.email), True


def address_purpose(address_type: str) -> str:
    """2.5: ``both`` becomes ``shipping``; the billing use stays implied by
    ``CustomerAddress.address_type``."""
    return "billing" if address_type == "billing" else "shipping"


ADDRESS_FIELDS = ("address_line_1", "address_line_2", "city", "state_province", "postal_code")


def address_values(address) -> dict:
    """The PartyAddress columns mirrored one way from a CustomerAddress."""
    values = {f: (getattr(address, f) or "") for f in ADDRESS_FIELDS}
    values["country"] = (address.country or "").strip().upper()[:2]
    values["label"] = (address.label or "")[:100]
    values["purpose"] = address_purpose(address.address_type)
    return values


CLOSED_LEAD_STATUSES = frozenset({"converted", "closed_lost", "closed_junk"})


def lead_role_dates(lead):
    """``(valid_from, valid_to)`` of the ``lead`` role a Lead implies (D10)."""
    start = lead.created_at.date()
    if lead.status not in CLOSED_LEAD_STATUSES and lead.converted_to_customer_id is None:
        return start, None
    end = lead.converted_at or lead.lost_at or lead.updated_at
    end = end.date() if end else start
    return start, max(start, end)


def org_key(name: str) -> str:
    return normalize_org_name(name)


def configured_portal_company():
    """``settings.GOLDEN_PORTAL_COMPANY`` (a Company pk), or None (2.4)."""
    return getattr(settings, "GOLDEN_PORTAL_COMPANY", None)


def default_company_id(company_model):
    """The tenant a NULL-company customer belongs to (2.4).

    ``GOLDEN_PORTAL_COMPANY`` when set, else the only Company if exactly one
    exists, else None (ambiguous: the caller refuses or skips).
    """
    configured = configured_portal_company()
    if configured:
        return company_model._base_manager.filter(pk=configured).values_list("pk", flat=True).first()
    ids = list(company_model._base_manager.values_list("pk", flat=True)[:2])
    return ids[0] if len(ids) == 1 else None


LEGACY_REF_COLUMNS = (
    # (Customer column, ExternalRef.source, ExternalRef.object_type)   D8
    ("bc_id", "bigcommerce", "customer"),
    ("erpnext_name", "erpnext", "customer"),
    ("ebay_username", "ebay", "buyer"),
    ("stripe_customer_id", "stripe", "customer"),
)
ADDRESS_REF = ("bc_address_id", "bigcommerce", "address")


def legacy_refs(customer) -> list[tuple[str, str, str, str]]:
    """``[(column, source, object_type, source_id)]`` for a Customer's legacy ids."""
    out = []
    for column, source, object_type in LEGACY_REF_COLUMNS:
        value = (getattr(customer, column) or "").strip()
        if value:
            out.append((column, source, object_type, value[:255]))
    return out
