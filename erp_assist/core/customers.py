"""Customers and customer addresses.

CustomerContact isn't exposed in the DRF API yet, so this module covers the
two resources we actually have endpoints for. Customer list/detail serializers
differ — list view is compact (7 fields), detail view returns the full model.
"""

from __future__ import annotations

from typing import Any

from ._writable_fields import (
    CUSTOMER_ADDRESS_WRITABLE,
    CUSTOMER_WRITABLE,
    validate_fields,
)
from .client import ERPClient
from .parties import TARGET_CUSTOMER, duplicate_check, match, raise_if_ambiguous, resolve_ref

CUSTOMERS_URL = "/api/v1/customers/customers/"
ADDRESSES_URL = "/api/v1/customers/addresses/"


# ---- Customers -------------------------------------------------------------


async def list_customers(
    client: ERPClient,
    *,
    search: str | None = None,
    customer_type: str | None = None,
    pricing_tier: str | None = None,
    is_active: bool | None = None,
    company: str | None = None,
    ordering: str | None = None,
    limit: int | None = 20,
) -> list[dict[str, Any]]:
    """List customers.

    `search` matches first_name, last_name, and email.
    `customer_type`, `pricing_tier`, `is_active`, and `company` are filter fields.
    `ordering` forwards to DRF (e.g. "last_name", "-created_at", "-last_order_at").
    """
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if search:
        params["search"] = search
    if customer_type:
        params["customer_type"] = customer_type
    if pricing_tier:
        params["pricing_tier"] = pricing_tier
    if is_active is not None:
        params["is_active"] = str(is_active).lower()
    if company:
        params["company"] = company
    if ordering:
        params["ordering"] = ordering
    return await client.list(CUSTOMERS_URL, params=params, max_items=limit)


async def find_customer(client: ERPClient, query: str) -> dict[str, Any]:
    """Find a customer by email, phone, or name. Never guesses (phase2-party.md 2.7).

    Returns the match envelope ``{match_quality, item, candidates, message}``
    from the server's ``/api/v1/parties/match/`` (``target=customer``):

    - ``exact``: one customer whose normalised email (or, through its party,
      E.164 phone) equals the query.
    - ``fuzzy``: exactly one name/search hit, not exact. Confirm it with the
      user before any write.
    - ``none``: nothing matches; say so and offer to create one.

    Two or more exact matches, or several search hits, raise
    :class:`ERPAmbiguousError` with the candidates: the caller must ask the
    user which one is meant and must not retry with a narrower query on its
    own. (Before step 8 this returned the first of up to ten fuzzy hits.)
    """
    return raise_if_ambiguous(await match(client, query=query, target=TARGET_CUSTOMER))


async def get_customer(client: ERPClient, customer_id: str) -> dict[str, Any]:
    """Fetch full customer detail by UUID (includes nested addresses)."""
    return await client.get(f"{CUSTOMERS_URL}{customer_id}/")


async def create_customer(
    client: ERPClient,
    *,
    first_name: str,
    last_name: str,
    email: str,
    customer_type: str = "individual",
    pricing_tier: str | None = None,
    phone: str | None = None,
    company_name: str | None = None,
    is_active: bool = True,
    accepts_marketing: bool = False,
    notes: str | None = None,
    extra_fields: dict[str, Any] | None = None,
    confirm_new: bool = False,
) -> dict[str, Any]:
    """Create a new customer. Only the commonly-used fields are typed; anything
    else (tax exemption, sales rep assignment, campaign attribution, etc.) can
    be passed via `extra_fields`.

    `customer_type` defaults to `"individual"` (the model default); the valid
    values are `individual` and `business`. Pricing tier is left to the model
    default (`retail`) unless given.

    Checks the match envelope first (phase2-party.md 2.7): when a party
    already holds this email, returns ``{"created": false, match_quality,
    candidates, message}`` and creates nothing, unless ``confirm_new=True``.
    """
    if not confirm_new:
        duplicate = await duplicate_check(client, email=email)
        if duplicate is not None:
            return duplicate
    payload: dict[str, Any] = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "customer_type": customer_type,
        "is_active": is_active,
        "accepts_marketing": accepts_marketing,
    }
    if pricing_tier is not None:
        payload["pricing_tier"] = pricing_tier
    if phone is not None:
        payload["phone"] = phone
    if company_name is not None:
        payload["company_name"] = company_name
    if notes is not None:
        payload["notes"] = notes
    if extra_fields:
        payload.update(extra_fields)
    return await client.post(CUSTOMERS_URL, json=payload)


async def update_customer(
    client: ERPClient, customer_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on an existing customer. Unknown fields raise."""
    validate_fields(fields, CUSTOMER_WRITABLE, "customer")
    return await client.patch(f"{CUSTOMERS_URL}{customer_id}/", json=fields)


# ---- Addresses -------------------------------------------------------------


async def list_customer_addresses(
    client: ERPClient,
    *,
    customer: str | None = None,
    address_type: str | None = None,
    country: str | None = None,
    search: str | None = None,
    limit: int | None = 50,
) -> list[dict[str, Any]]:
    """List customer addresses, optionally scoped to a specific customer.

    `search` matches city and postal_code. `address_type` is the DRF filter.
    """
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if customer:
        params["customer"] = customer
    if address_type:
        params["address_type"] = address_type
    if country:
        params["country"] = country
    if search:
        params["search"] = search
    return await client.list(ADDRESSES_URL, params=params, max_items=limit)


async def get_customer_address(client: ERPClient, address_id: str) -> dict[str, Any]:
    """Fetch a single address by UUID."""
    return await client.get(f"{ADDRESSES_URL}{address_id}/")


async def create_customer_address(
    client: ERPClient,
    *,
    customer: str,
    address_line_1: str,
    city: str,
    state_province: str,
    postal_code: str,
    country: str,
    address_type: str = "shipping",
    first_name: str | None = None,
    last_name: str | None = None,
    company_name: str | None = None,
    address_line_2: str | None = None,
    phone: str | None = None,
    is_default: bool = False,
    is_residential: bool | None = None,
    has_loading_dock: bool | None = None,
    has_forklift: bool | None = None,
    requires_liftgate: bool | None = None,
    delivery_instructions: str | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new address on a customer.

    `customer` is a UUID, or free text (email, phone, name) resolved through
    the match envelope: only an exact match proceeds; anything else raises
    ERPAmbiguousError / ERPNotFoundError before any write.
    """
    customer = await resolve_ref(client, customer, target=TARGET_CUSTOMER, what="customer")
    payload: dict[str, Any] = {
        "customer": customer,
        "address_type": address_type,
        "address_line_1": address_line_1,
        "city": city,
        "state_province": state_province,
        "postal_code": postal_code,
        "country": country,
        "is_default": is_default,
    }
    optional = {
        "first_name": first_name,
        "last_name": last_name,
        "company_name": company_name,
        "address_line_2": address_line_2,
        "phone": phone,
        "is_residential": is_residential,
        "has_loading_dock": has_loading_dock,
        "has_forklift": has_forklift,
        "requires_liftgate": requires_liftgate,
        "delivery_instructions": delivery_instructions,
    }
    for k, v in optional.items():
        if v is not None:
            payload[k] = v
    if extra_fields:
        payload.update(extra_fields)
    return await client.post(ADDRESSES_URL, json=payload)


async def update_customer_address(
    client: ERPClient, address_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on an existing address. Unknown fields raise."""
    validate_fields(fields, CUSTOMER_ADDRESS_WRITABLE, "customer address")
    return await client.patch(f"{ADDRESSES_URL}{address_id}/", json=fields)


async def delete_customer_address(
    client: ERPClient, address_id: str
) -> dict[str, Any]:
    """Permanently delete a customer address row.

    Addresses are the most common thing to need clean-up (duplicates from
    mis-typed zip codes, old billing addresses after a move, etc.) and
    deletion here doesn't cascade into orders — historical orders keep
    their own snapshotted ship-to fields.
    """
    await client.delete(f"{ADDRESSES_URL}{address_id}/")
    return {"deleted": True, "resource": "customer_address", "id": address_id}
