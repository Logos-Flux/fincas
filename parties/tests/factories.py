"""Rows for the step-5 tests, written the way legacy data looks.

``legacy_*`` use ``bulk_create``: no ``save()``, so no party sync runs and
the rows look like pre-Phase-2 data for the backfill to pick up.
"""

from __future__ import annotations

import itertools
import uuid

from crm.models import Lead
from customers.models import Customer, CustomerAddress, CustomerContact

_seq = itertools.count(1)



def legacy_customer(company, **kw) -> Customer:
    n = next(_seq)
    kw.setdefault("first_name", "Ada")
    kw.setdefault("last_name", "Lovelace")
    kw.setdefault("email", f"customer{n}@example.com")
    (c,) = Customer.objects.bulk_create([Customer(company=company, **kw)])
    return Customer.objects.get(pk=c.pk)


def legacy_contact(customer, **kw) -> CustomerContact:
    kw.setdefault("first_name", "Grace")
    kw.setdefault("last_name", "Hopper")
    (ct,) = CustomerContact.objects.bulk_create([CustomerContact(customer=customer, **kw)])
    return CustomerContact.objects.get(pk=ct.pk)


def legacy_address(customer, **kw) -> CustomerAddress:
    kw.setdefault("address_line_1", "1 Main St")
    kw.setdefault("city", "Springfield")
    kw.setdefault("state_province", "IL")
    kw.setdefault("postal_code", "62701")
    (a,) = CustomerAddress.objects.bulk_create([CustomerAddress(customer=customer, **kw)])
    return CustomerAddress.objects.get(pk=a.pk)


def legacy_lead(company, **kw) -> Lead:
    n = next(_seq)
    kw.setdefault("first_name", "Lee")
    kw.setdefault("last_name", "Ead")
    kw.setdefault("email", f"lead{n}@example.com")
    kw.setdefault("lead_number", f"LD-T{n:05d}-{uuid.uuid4().hex[:4]}")
    (lead,) = Lead.objects.bulk_create([Lead(company=company, **kw)])
    return Lead.objects.get(pk=lead.pk)
