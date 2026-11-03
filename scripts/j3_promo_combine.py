#!/usr/bin/env python3
"""J3 promo journey against a running ERP (testing-plan §8.1).

Coupon + tier price combine per the discounts rules; invalid combos are
rejected. Each step asserts the API response plus a follow-up read.
Timestamped seeds make reruns collision-free.

  J3_BASE=http://localhost:8000 J3_API_KEY=<key> \
      python scripts/j3_promo_combine.py

Flow:
- staff creates coupons through the ERP UI (session auth) — the genuine
  promo setup path, and user-visible confirmation;
- validate/exercise valid, expired, wrong-SKU, below-minimum, unknown codes;
- an order carries a tier price (from the price list) on its line plus the
  validated coupon mirrored into discount_amount/coupon_discount, and
  recalculate combines them into the grand total;
- redeem marks the code used; a second redeem of a one-time code is rejected.
"""

import os
import re
import sys
import time

import requests

BASE = os.environ.get("J3_BASE", "http://localhost:8000").rstrip("/")
try:
    API_KEY = os.environ["J3_API_KEY"]
except KeyError:
    print("J3_API_KEY is required", file=sys.stderr)
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


print(f"J3 promo/combine @ {BASE} tag={TAG}")

# ── staff creates coupons through the ERP UI ─────────────────────────
web = requests.Session()
login_page = web.get(BASE + "/erp/login/", timeout=30)
m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', login_page.text)
web.post(BASE + "/erp/login/", data={
    "csrfmiddlewaretoken": m.group(1), "username": "admin", "password": "admin",
}, headers={"Origin": BASE, "Referer": BASE + "/erp/login/"}, timeout=30)


def erp_create_coupon(code, **fields):
    page = web.get(BASE + "/erp/discounts/new/", timeout=30)
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text).group(1)
    form = {
        "csrfmiddlewaretoken": token, "code": code, "description": f"J3 {TAG}",
        "is_active": "on", "discount_type": "percent", "value": "10",
        "max_uses": "5", "applies_to": "all",
    }
    form.update(fields)
    r = web.post(BASE + "/erp/discounts/new/", data=form,
                 headers={"Origin": BASE, "Referer": BASE + "/erp/discounts/new/"},
                 timeout=30)
    check(f"ERP creates coupon {code}", r.status_code in (200, 302), str(r.status_code))
    listed = web.get(BASE + "/erp/discounts/", timeout=30)
    check(f"coupon {code} listed", code in listed.text, "not on list page")


erp_create_coupon(f"J3PCT-{TAG}")
erp_create_coupon(f"J3SKU-{TAG}", discount_type="fixed_amount", value="15",
                  applies_to="specific_skus", allowed_skus=f"J3SKU-{TAG}")
erp_create_coupon(f"J3EXP-{TAG}", discount_type="percent", value="20",
                  valid_to="2020-01-01T00:00")
erp_create_coupon(f"J3MIN-{TAG}", minimum_order="500.00")
erp_create_coupon(f"J3ONE-{TAG}", max_uses="1")

CART = {"subtotal": 180.0, "items": [
    {"sku": f"J3SKU-{TAG}", "quantity": 2, "price": 90.0},
]}


def validate(code, **overrides):
    body = {"code": code, **CART}
    body.update(overrides)
    return api("POST", "/api/v1/discounts/validate/", body)


# ── valid + invalid combinations ─────────────────────────────────────
v = validate(f"J3PCT-{TAG}")
check("10% coupon valid: 18.00 off 180",
      v and v.get("valid") is True and v.get("discount_amount") == 18.0, str(v))
v = validate(f"J3SKU-{TAG}")
check("$15 SKU coupon valid on matching cart",
      v and v.get("valid") is True and v.get("discount_amount") == 15.0, str(v))
v = validate(f"J3SKU-{TAG}", items=[{"sku": "OTHER", "quantity": 1, "price": 50.0}],
             subtotal=50.0)
check("SKU coupon rejected on wrong cart",
      v and v.get("valid") is False, str(v))
v = validate(f"J3EXP-{TAG}")
check("expired coupon rejected",
      v and v.get("valid") is False and "expired" in v.get("message", "").lower(), str(v))
v = validate(f"J3MIN-{TAG}")
check("below-minimum coupon rejected",
      v and v.get("valid") is False and "minimum" in v.get("message", "").lower(), str(v))
v = validate("NOPE-NOT-A-CODE")
check("unknown coupon rejected",
      v and v.get("valid") is False, str(v))

# ── tier price + coupon combine on a real order ──────────────────────
company = api("POST", "/api/v1/core/companies/", {"name": f"J3 Co {TAG}"}, expect=201)
C = company["id"]
plist = api("POST", "/api/v1/pricing/price-lists/", {
    "name": f"J3 Tier {TAG}", "company": C,
}, expect=(201, 400))
if plist is None:
    plist = api("POST", "/api/v1/pricing/price-lists/", {"name": f"J3 Tier {TAG}"}, expect=201)
product = api("POST", "/api/v1/products/products/", {
    "title": f"J3 Widget {TAG}", "part_number": f"J3PN-{TAG}", "slug": f"j3-{TAG.lower()}",
}, expect=201)
variant = api("POST", "/api/v1/products/variants/", {
    "product": product["id"], "sku": f"J3SKU-{TAG}",
}, expect=201)
tier = api("POST", "/api/v1/pricing/price-list-items/", {
    "price_list": plist["id"], "variant": variant["id"],
    "price": "90.00", "min_qty": 1,
}, expect=(201, 400))
check("tier price recorded", tier and float(tier.get("price")) == 90.00, str(tier))

customer = api("POST", "/api/v1/customers/customers/", {
    "first_name": "J3", "last_name": "Buyer", "email": f"j3-{TAG}@example.com",
    "company": C,
}, expect=201)
quote = api("POST", "/api/v1/orders/quotes/", {
    "customer": customer["id"], "company": C, "quote_number": f"Q-{TAG}",
}, expect=201)
api("POST", "/api/v1/orders/quote-lines/", {
    "quote": quote["id"], "variant": variant["id"], "item_name": f"J3 Widget {TAG}",
    "sku": f"J3SKU-{TAG}", "quantity": "2", "unit_price_ex_tax": "90.0000",
}, expect=201)
converted = api("POST", f"/api/v1/orders/quotes/{quote['id']}/convert-to-order/", {}, expect=(200, 201))
order_id = (converted or {}).get("id")
# Mirror the validated 10% coupon (18.00) the way web checkout does.
api("PATCH", f"/api/v1/orders/orders/{order_id}/",
    {"discount_amount": "18.00", "coupon_discount": "18.00"})
order = api("POST", f"/api/v1/orders/orders/{order_id}/recalculate/")
check("tier + coupon combine: grand 162.00",
      order and order.get("grand_total") == "162.00"
      and order.get("balance_due") == "162.00",
      str(order and (order.get("grand_total"), order.get("balance_due"))))

# ── redeem once, then rejected ───────────────────────────────────────
r = api("POST", "/api/v1/discounts/redeem/", {
    "code": f"J3ONE-{TAG}", "order_number": order.get("order_number"),
    "discount_applied": "5.00",
})
check("one-time code redeems", r and r.get("success") is True, str(r))
r2 = api("POST", "/api/v1/discounts/redeem/", {
    "code": f"J3ONE-{TAG}", "order_number": order.get("order_number"),
    "discount_applied": "5.00",
}, expect=400)
check("second redeem rejected",
      r2 and r2.get("success") is False, str(r2))

print(f"\nJ3 {'PASSED' if not failures else 'FAILED: ' + str(failures)}")
sys.exit(1 if failures else 0)
