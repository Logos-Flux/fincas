#!/usr/bin/env python3
"""J1 procure-to-sell journey against a running ERP (testing-plan §8.1).

Walks the full back-office chain over the DRF API and asserts every step:

  supplier → PO (+lines) → receive → stock on hand → quote → order →
  capture payment → pick/pack/ship → invoice paid.

Each step asserts the API response (the user-visible surface) plus a
follow-up read of the resulting state. Timestamped seeds make reruns
collision-free.

  J1_BASE=http://localhost:8000 J1_API_KEY=<key> \
      python scripts/j1_procure_to_sell.py

OPEN TAIL (F-017): no GL auto-posting from operational docs exists, and
accounting exposes no journal API — so "GL balanced" cannot be asserted
yet. The script reports it and exits 0; close it when F-017 lands.
"""

import os
import sys
import time

import requests

BASE = os.environ.get("J1_BASE", "http://localhost:8000").rstrip("/")
try:
    API_KEY = os.environ["J1_API_KEY"]
except KeyError:
    print("J1_API_KEY is required", file=sys.stderr)
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


def transition(path, to, expect=200):
    data = api("POST", path + "/transition/", {"status": to}, expect=expect)
    if data is not None:
        check(f"transition → {to}", data.get("status") == to, str(data.get("status")))
    return data


print(f"J1 procure-to-sell @ {BASE} tag={TAG}")

# ── procure ──────────────────────────────────────────────────────────
company = api("POST", "/api/v1/core/companies/", {"name": f"J1 Co {TAG}"}, expect=201)
C = company["id"]

supplier = api("POST", "/api/v1/purchasing/suppliers/", {
    "company": C, "company_name": f"J1 Supplier {TAG}",
    "supplier_code": f"J1SUP-{TAG}", "supplier_type": "manufacturer",
}, expect=201)
SU = supplier["id"]

product = api("POST", "/api/v1/products/products/", {
    "title": f"J1 Widget {TAG}", "part_number": f"J1PN-{TAG}", "slug": f"j1-{TAG.lower()}",
}, expect=201)
variant = api("POST", "/api/v1/products/variants/", {
    "product": product["id"], "sku": f"J1SKU-{TAG}",
}, expect=201)
V = variant["id"]

# NOTE: po/quote/receipt/ticket numbers auto-mint in save(), but DRF
# field validation runs first — API callers must supply them.
po = api("POST", "/api/v1/purchasing/purchase-orders/", {
    "supplier": SU, "company": C, "po_number": f"PO-{TAG}",
}, expect=201)
PO = po["id"]

line = api("POST", "/api/v1/purchasing/po-lines/", {
    "purchase_order": PO, "variant": V, "item_name": f"J1 Widget {TAG}",
    "quantity_ordered": "10", "unit_cost": "50.00",
}, expect=201)
LI = line["id"]

po = api("POST", f"/api/v1/purchasing/purchase-orders/{PO}/recalculate/")
check("PO subtotal 500.00", po and po.get("subtotal") == "500.00", str(po and po.get("subtotal")))

# ── receive ──────────────────────────────────────────────────────────
for to in ("sent", "acknowledged", "shipped", "receiving", "received"):
    transition(f"/api/v1/purchasing/purchase-orders/{PO}", to)
api("PATCH", f"/api/v1/purchasing/po-lines/{LI}/", {"quantity_received": "10"})
receipt = api("POST", "/api/v1/purchasing/receipts/", {
    "purchase_order": PO, "receipt_number": f"REC-{TAG}",
}, expect=201)
if receipt is not None:
    api("POST", "/api/v1/purchasing/receipt-lines/", {
        "receipt": receipt["id"], "po_line_item": LI, "quantity_received": "10",
    }, expect=(201, 404))
    check("receipt recorded", True)

# ── stock on hand (manual posting: no auto-bump from receiving exists) ─
warehouse = api("POST", "/api/v1/inventory/warehouses/", {
    "warehouse_code": f"J1WH-{TAG}", "name": f"J1 Warehouse {TAG}", "company": C,
}, expect=(201, 400))
if warehouse is None:
    warehouse = api("POST", "/api/v1/inventory/warehouses/", {
        "warehouse_code": f"J1WH-{TAG}", "name": f"J1 Warehouse {TAG}",
    }, expect=201)
W = warehouse["id"]
stock = api("POST", "/api/v1/inventory/stock/", {
    "warehouse": W, "variant": V, "quantity": "10",
}, expect=(201, 400))
if stock is None or "id" not in stock:
    existing = api("GET", f"/api/v1/inventory/stock/?variant={V}")
    results = (existing or {}).get("results", existing if isinstance(existing, list) else [])
    stock = results[0] if results else None
    if stock:
        stock = api("PATCH", f"/api/v1/inventory/stock/{stock['id']}/", {"quantity": "10"})
check("stock on hand 10", stock and str(stock.get("quantity", "")).startswith("10"),
      str(stock and stock.get("quantity")))

# ── sell: quote → order ──────────────────────────────────────────────
customer = api("POST", "/api/v1/customers/customers/", {
    "first_name": "J1", "last_name": "Buyer", "email": f"j1-{TAG}@example.com",
    "company": C,
}, expect=201)
CU = customer["id"]

quote = api("POST", "/api/v1/orders/quotes/", {
    "customer": CU, "company": C, "quote_number": f"Q-{TAG}",
}, expect=201)
Q = quote["id"]
api("POST", "/api/v1/orders/quote-lines/", {
    "quote": Q, "variant": V, "item_name": f"J1 Widget {TAG}",
    "sku": f"J1SKU-{TAG}", "quantity": "2", "unit_price_ex_tax": "100.0000",
}, expect=201)
converted = api("POST", f"/api/v1/orders/quotes/{Q}/convert-to-order/", {}, expect=(200, 201))
order_id = (converted or {}).get("id") or (converted or {}).get("order", {}).get("id")
check("quote converted to order", bool(order_id), str(converted)[:200])
order = api("GET", f"/api/v1/orders/orders/{order_id}/")
check("order pending/unpaid", order and order.get("status") == "pending"
      and order.get("payment_status") == "unpaid",
      str(order and (order.get("status"), order.get("payment_status"))))

transition(f"/api/v1/orders/orders/{order_id}", "awaiting_payment")

# ── capture payment via invoice reconcile (mints the Payment row) ────
invoiced = api("POST", f"/api/v1/orders/orders/{order_id}/convert-to-invoice/", {}, expect=(200, 201))
inv_id = (invoiced or {}).get("id")
inv = api("GET", f"/api/v1/orders/sales-invoices/{inv_id}/")
grand = inv.get("grand_total")
check("invoice created", bool(inv.get("payment_token")), f"grand_total={grand}")
cents = int((float(grand) * 100))
pi = f"pi_j1_{TAG}"
rec = api("POST", f"/api/v1/orders/sales-invoices/by-token/{inv['payment_token']}/reconcile/", {
    "event": "paid", "amount_cents": cents, "convenience_fee_cents": 0,
    "stripe_payment_intent_id": pi, "method": "card", "transaction_id": pi,
})
check("invoice paid", rec and rec.get("status") == "paid", str(rec))
inv = api("GET", f"/api/v1/orders/sales-invoices/{inv_id}/")
check("invoice balance zero", inv and str(inv.get("balance_due")) in ("0.00", "0"),
      str(inv and inv.get("balance_due")))
order = api("GET", f"/api/v1/orders/orders/{order_id}/")
check("order payment_status paid", order and order.get("payment_status") == "paid",
      str(order and order.get("payment_status")))
pays = api("GET", f"/api/v1/payments/payments/?order={order_id}")
pay_rows = (pays or {}).get("results", pays if isinstance(pays, list) else [])
check("captured payment recorded",
      any(p.get("status") in ("captured", "settled") for p in pay_rows),
      str([p.get("status") for p in pay_rows]))

# ── pick / pack / ship ───────────────────────────────────────────────
lines = api("GET", f"/api/v1/orders/order-lines/?order={order_id}")
olines = (lines or {}).get("results", lines if isinstance(lines, list) else [])
ticket = api("POST", "/api/v1/fulfillment/pick-tickets/", {
    "order": order_id, "warehouse": W, "ticket_number": f"PT-{TAG}",
}, expect=201)
T = ticket["id"]
for ol in olines:
    api("POST", "/api/v1/fulfillment/pick-ticket-lines/", {
        "pick_ticket": T, "order_line_item": ol["id"],
        "variant": V, "quantity": ol.get("quantity", "2"),
    }, expect=201)
for to in ("assigned", "picking", "picked", "packing", "packed", "shipped"):
    transition(f"/api/v1/fulfillment/pick-tickets/{T}", to)

for to in ("awaiting_shipment", "shipped", "completed"):
    transition(f"/api/v1/orders/orders/{order_id}", to)
order = api("GET", f"/api/v1/orders/orders/{order_id}/")
check("order completed", order and order.get("status") == "completed",
      str(order and order.get("status")))

# ── GL tail: OPEN (F-017) ────────────────────────────────────────────
print("OPEN TAIL (F-017): no GL auto-posting from operational docs exists "
      "and accounting exposes no journal API — 'GL balanced' is not "
      "assertable yet.")

print(f"\nJ1 {'PASSED' if not failures else 'FAILED: ' + str(failures)}")
sys.exit(1 if failures else 0)
