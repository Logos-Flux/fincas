#!/usr/bin/env python3
"""J2 returns/refund journey against a running ERP (testing-plan §8.1).

Paid order → refund → stock restored. Each step asserts the API response
plus a follow-up read of the resulting state. Timestamped seeds make
reruns collision-free.

  J2_BASE=http://localhost:8000 J2_API_KEY=<key> \
      python scripts/j2_returns_refund.py

NOTES (current behavior, pinned here):
- Stock moves are manual: nothing decrements on ship or restores on
  refund — the journey posts both explicitly, like receiving in J1.
- SalesInvoice has no refund handling: it stays PAID after a full refund.
- Order has no full-refund state: shipped → partially_refunded is the
  terminal refund state; payment_status/refunded_amount are staff-written.
- OPEN TAIL (F-017): "GL balanced" unassertable — no GL auto-posting.
"""

import os
import sys
import time

import requests

BASE = os.environ.get("J2_BASE", "http://localhost:8000").rstrip("/")
try:
    API_KEY = os.environ["J2_API_KEY"]
except KeyError:
    print("J2_API_KEY is required", file=sys.stderr)
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


def transition(path, to):
    data = api("POST", path + "/transition/", {"status": to})
    if data is not None:
        check(f"transition → {to}", data.get("status") == to, str(data.get("status")))
    return data


print(f"J2 returns/refund @ {BASE} tag={TAG}")

# ── build a paid, shipped order (condensed J1) ───────────────────────
company = api("POST", "/api/v1/core/companies/", {"name": f"J2 Co {TAG}"}, expect=201)
C = company["id"]
customer = api("POST", "/api/v1/customers/customers/", {
    "first_name": "J2", "last_name": "Buyer", "email": f"j2-{TAG}@example.com",
    "company": C,
}, expect=201)
product = api("POST", "/api/v1/products/products/", {
    "title": f"J2 Widget {TAG}", "part_number": f"J2PN-{TAG}", "slug": f"j2-{TAG.lower()}",
}, expect=201)
variant = api("POST", "/api/v1/products/variants/", {
    "product": product["id"], "sku": f"J2SKU-{TAG}",
}, expect=201)
V = variant["id"]
warehouse = api("POST", "/api/v1/inventory/warehouses/", {
    "warehouse_code": f"J2WH-{TAG}", "name": f"J2 Warehouse {TAG}", "company": C,
}, expect=(201, 400))
if warehouse is None:
    warehouse = api("POST", "/api/v1/inventory/warehouses/", {
        "warehouse_code": f"J2WH-{TAG}", "name": f"J2 Warehouse {TAG}",
    }, expect=201)
W = warehouse["id"]
stock = api("POST", "/api/v1/inventory/stock/", {
    "warehouse": W, "variant": V, "quantity": "10",
}, expect=201)
ST = stock["id"]

quote = api("POST", "/api/v1/orders/quotes/", {
    "customer": customer["id"], "company": C, "quote_number": f"Q-{TAG}",
}, expect=201)
api("POST", "/api/v1/orders/quote-lines/", {
    "quote": quote["id"], "variant": V, "item_name": f"J2 Widget {TAG}",
    "sku": f"J2SKU-{TAG}", "quantity": "2", "unit_price_ex_tax": "100.0000",
}, expect=201)
converted = api("POST", f"/api/v1/orders/quotes/{quote['id']}/convert-to-order/", {}, expect=(200, 201))
order_id = (converted or {}).get("id")
transition(f"/api/v1/orders/orders/{order_id}", "awaiting_payment")
invoiced = api("POST", f"/api/v1/orders/orders/{order_id}/convert-to-invoice/", {}, expect=(200, 201))
inv = api("GET", f"/api/v1/orders/sales-invoices/{invoiced['id']}/")
cents = int(float(inv["grand_total"]) * 100)
pi = f"pi_j2_{TAG}"
rec = api("POST", f"/api/v1/orders/sales-invoices/by-token/{inv['payment_token']}/reconcile/", {
    "event": "paid", "amount_cents": cents, "convenience_fee_cents": 0,
    "stripe_payment_intent_id": pi, "method": "card", "transaction_id": pi,
})
check("order paid in full", rec and rec.get("status") == "paid", str(rec))

lines = api("GET", f"/api/v1/orders/order-lines/?order={order_id}")
olines = (lines or {}).get("results", [])
ticket = api("POST", "/api/v1/fulfillment/pick-tickets/", {
    "order": order_id, "warehouse": W, "ticket_number": f"PT-{TAG}",
}, expect=201)
for ol in olines:
    api("POST", "/api/v1/fulfillment/pick-ticket-lines/", {
        "pick_ticket": ticket["id"], "order_line_item": ol["id"],
        "variant": V, "quantity": ol.get("quantity", "2"),
    }, expect=201)
for to in ("assigned", "picking", "picked", "packing", "packed", "shipped"):
    transition(f"/api/v1/fulfillment/pick-tickets/{ticket['id']}", to)
# Shipment moves stock manually (no automation exists).
api("PATCH", f"/api/v1/inventory/stock/{ST}/", {"quantity": "8"})
for to in ("awaiting_shipment", "shipped"):
    transition(f"/api/v1/orders/orders/{order_id}", to)

# ── refund ───────────────────────────────────────────────────────────
order = api("GET", f"/api/v1/orders/orders/{order_id}/")
full = order["grand_total"]
refund = api("POST", "/api/v1/payments/payments/", {
    "payment_number": f"PAY-R-{TAG}", "order": order_id,
    "invoice": inv["id"], "company": C,
    "payment_method": "credit_card", "payment_provider": "stripe",
    "amount": full, "payment_type": "refund", "transaction_id": "",
    "status": "refunded",
}, expect=201)
check("refund payment recorded",
      refund and refund.get("status") == "refunded"
      and refund.get("payment_type") == "refund",
      str(refund and (refund.get("status"), refund.get("payment_type"))))

patched = api("PATCH", f"/api/v1/orders/orders/{order_id}/", {
    "payment_status": "refunded", "refunded_amount": full,
})
check("order marked refunded",
      patched and patched.get("payment_status") == "refunded"
      and patched.get("refunded_amount") == full,
      str(patched and (patched.get("payment_status"), patched.get("refunded_amount"))))
transition(f"/api/v1/orders/orders/{order_id}", "partially_refunded")

# Stock restored (manual — mirrors the manual outbound above).
api("PATCH", f"/api/v1/inventory/stock/{ST}/", {"quantity": "10"})
stock = api("GET", f"/api/v1/inventory/stock/{ST}/")
check("stock restored to 10", stock and str(stock.get("quantity", "")).startswith("10"),
      str(stock and stock.get("quantity")))

inv = api("GET", f"/api/v1/orders/sales-invoices/{inv['id']}/")
print(f"NOTE: invoice stays {inv and inv.get('status')} after full refund — "
      "no invoice refund handling exists.")

print("OPEN TAIL (F-017): 'GL balanced' unassertable — no GL auto-posting.")

print(f"\nJ2 {'PASSED' if not failures else 'FAILED: ' + str(failures)}")
sys.exit(1 if failures else 0)
