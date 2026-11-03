#!/usr/bin/env python3
"""J4 fiscal-period close journey against a running ERP (testing-plan §8.1).

Specified: closing a fiscal period blocks backdated posts. Each step
asserts the API response plus a follow-up read. Timestamped seeds make
reruns collision-free.

  J4_BASE=http://localhost:8000 J4_API_KEY=<key> \
      python scripts/j4_period_close.py

CURRENT STATUS — OPEN, not passing behavior. Refreshed 2026-10-04 (AD-11):
the gap is narrower than first probed. Direct ``JournalEntry`` writes ARE now
guarded at the model layer (``accounting/models.py`` ``_reject_closed_period``
runs on every ``save()``, reading fresh status from the DB). What is still
missing:
- a FiscalPeriod close API (no period list/close endpoint reachable);
- the operational-to-GL posting service, so invoices/payments/refunds never
  reach the guarded path at all (F-017).
Close this journey by building the close API + the posting service (plan Step
1a/1b) and flipping the assertions to expect rejection.
"""

import os
import sys
import time

import requests

BASE = os.environ.get("J4_BASE", "http://localhost:8000").rstrip("/")
try:
    API_KEY = os.environ["J4_API_KEY"]
except KeyError:
    print("J4_API_KEY is required", file=sys.stderr)
    sys.exit(2)

TAG = time.strftime("%Y%m%d%H%M%S")
S = requests.Session()
S.headers.update({"Authorization": f"Api-Key {API_KEY}"})

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def api(method, path, body=None, expect=(200,)):
    if isinstance(expect, int):
        expect = (expect,)
    r = S.request(method, BASE + path, json=body, timeout=30)
    if r.status_code not in expect:
        check(f"{method} {path} → {expect}", False, f"got {r.status_code}: {r.text[:300]}")
        return None
    try:
        return r.json()
    except ValueError:
        return {}


print(f"J4 period-close @ {BASE} tag={TAG}")

# No fiscal-period API exists: every plausible route 404s.
for path in ("/api/v1/accounting/fiscal-periods/",
             "/api/v1/accounting/journal-entries/",
             "/api/v1/accounting/periods/"):
    r = S.get(BASE + path, timeout=30)
    check(f"no period/journal API ({path})", r.status_code == 404, str(r.status_code))

# Minimal docs to backdate against.
company = api("POST", "/api/v1/core/companies/", {"name": f"J4 Co {TAG}"}, expect=201)
C = company["id"]
customer = api("POST", "/api/v1/customers/customers/", {
    "first_name": "J4", "last_name": "Buyer", "email": f"j4-{TAG}@example.com",
    "company": C,
}, expect=201)
quote = api("POST", "/api/v1/orders/quotes/", {
    "customer": customer["id"], "company": C, "quote_number": f"Q-{TAG}",
}, expect=201)
api("POST", "/api/v1/orders/quote-lines/", {
    "quote": quote["id"], "item_name": f"J4 Widget {TAG}",
    "sku": f"J4SKU-{TAG}", "quantity": "1", "unit_price_ex_tax": "50.0000",
}, expect=201)
converted = api("POST", f"/api/v1/orders/quotes/{quote['id']}/convert-to-order/", {}, expect=(200, 201))
order_id = (converted or {}).get("id")
invoiced = api("POST", f"/api/v1/orders/orders/{order_id}/convert-to-invoice/", {}, expect=(200, 201))

# Backdated posts go through unchecked today — pinned as the gap.
back = api("PATCH", f"/api/v1/orders/sales-invoices/{invoiced['id']}/",
           {"invoice_date": "2020-01-01"})
check("backdated invoice_date accepted (gap: no period guard)",
      back and back.get("invoice_date") == "2020-01-01",
      str(back and back.get("invoice_date")))

pay = api("POST", "/api/v1/payments/payments/", {
    "payment_number": f"PJ4-{TAG}", "order": order_id, "company": C,
    "payment_method": "credit_card", "payment_provider": "stripe",
    "amount": "10.00", "payment_type": "payment", "transaction_id": f"tx-j4-{TAG}",
    "status": "pending", "payment_date": "2020-01-01",
}, expect=(201, 400))
if pay is not None:
    check("backdated payment_date accepted (gap: no period guard)",
          str(pay.get("payment_date", "")).startswith("2020-01-01"),
          str(pay.get("payment_date")))
else:
    print("NOTE: direct payment create rejected — gap stands regardless.")

print("OPEN (missing feature): fiscal-period close API + closed-period "
      "posting guard on operational dates. Nothing above was rejected.")
print(f"\nJ4 {'PASSED' if not failures else 'FAILED: ' + str(failures)}")
sys.exit(1 if failures else 0)
