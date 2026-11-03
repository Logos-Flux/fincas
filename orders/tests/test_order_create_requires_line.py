"""Empty order create must not persist a customerless, lineless order.

Regression guard for /qa ISSUE-001 (2026-09-25): clicking Save on a
completely empty New Sales Order form created SO-00001 with customer NULL
and zero lines, then redirected to the list with no error and no message.

The line-item formset deliberately stays valid when empty (see
test_line_item_blank_row.py -- the phantom blank row must not block saves),
so the guard lives in the document view: at least one entered line is
required before the order is saved.

Both tests post exactly what the GET page renders (harvested input values),
so they exercise the real browser round-trip rather than a hand-built
payload that would miss hidden defaults like fulfillment_status.
"""

from __future__ import annotations

import re

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import Company, UserProfile
from customers.models import Customer
from orders.models import Order


def harvest_inputs(html: str) -> dict:
    """Collect the name/value pairs a browser would submit for this HTML."""
    data: dict = {}
    for m in re.finditer(r"<input\b([^>]*)>", html):
        attrs = m.group(1)
        name = re.search(r'name="([^"]+)"', attrs)
        if not name:
            continue
        name = name.group(1)
        kind = re.search(r'type="([^"]+)"', attrs)
        kind = kind.group(1).lower() if kind else "text"
        if kind in ("submit", "button", "reset"):
            continue
        if kind in ("checkbox", "radio") and "checked" not in attrs:
            continue
        value = re.search(r'value="([^"]*)"', attrs)
        data.setdefault(name, value.group(1) if value else "")
    for m in re.finditer(r"<select\b([^>]*)>(.*?)</select>", html, re.S):
        name = re.search(r'name="([^"]+)"', m.group(1))
        if not name:
            continue
        options = re.findall(r'<option\b([^>]*)>', m.group(2))
        picked = ""
        for opt in options:
            val = re.search(r'value="([^"]*)"', opt)
            val = val.group(1) if val else ""
            if "selected" in opt:
                picked = val
                break
            if picked == "" and val:
                picked = val
        data.setdefault(name.group(1), picked)
    for m in re.finditer(
        r'<textarea\b[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>', html, re.S
    ):
        # Line-item names render as textareas (rows=1) — a browser
        # submits their content, so the harvest must too.
        data.setdefault(m.group(1), m.group(2).strip())
    return data


class OrderCreateRequiresLineTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="ada@example.com", company=self.company,
        )
        self.user = User.objects.create_user(
            username="staff", password="pw", is_staff=True,
        )
        self.user.userprofile = UserProfile.objects.create(
            company=self.company,
        )
        self.client.force_login(self.user)
        self.url = reverse("erp-order-create")

    def _post_as_rendered(self, **overrides):
        html = self.client.get(self.url).content.decode()
        data = harvest_inputs(html)
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_empty_submit_saves_nothing_and_names_line_items(self):
        response = self._post_as_rendered()
        # Re-render with errors, not a redirect to the list.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Order.objects.count(), 0)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(
            any("line item" in m.lower() for m in messages),
            f"expected a line-item error, got: {messages}",
        )

    def test_submit_with_one_freeform_line_saves(self):
        response = self._post_as_rendered(
            customer=str(self.customer.pk),
            **{
                "line_items-0-sku": "MISC",
                "line_items-0-item_name": "Custom fabrication, on-site",
                "line_items-0-quantity": "1",
                "line_items-0-unit_price_ex_tax": "450.00",
            },
        )
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertEqual(order.line_items.count(), 1)
        self.assertEqual(order.customer, self.customer)


class OrderEditLineGuardTests(TestCase):
    """The empty-document guard must not veto the edit view's normal work.

    Regression: the guard as first shipped rejected every edit POST whose
    formset rows were untouched (has_changed() is False for kept lines),
    so saving an order without touching lines — and every status
    transition or cancel on such an order — re-rendered with 200.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="ada@example.com", company=self.company,
        )
        self.user = User.objects.create_user(
            username="staff", password="pw", is_staff=True,
        )
        self.user.userprofile = UserProfile.objects.create(
            company=self.company,
        )
        self.client.force_login(self.user)
        self.order = Order.objects.create(
            customer=self.customer, company=self.company,
            status=Order.Status.PENDING,
        )
        from orders.models import OrderLineItem
        OrderLineItem.objects.create(
            order=self.order, sku="MISC",
            item_name="Custom fabrication, on-site",
            quantity=1, unit_price_ex_tax=450,
        )

    def _post_as_rendered(self, **overrides):
        url = reverse("erp-order-edit", args=[self.order.pk])
        html = self.client.get(url).content.decode()
        data = harvest_inputs(html)
        data.update(overrides)
        return self.client.post(url, data)

    def test_unchanged_edit_with_persisted_line_saves(self):
        response = self._post_as_rendered()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.order.line_items.count(), 1)

    def test_edit_deleting_only_line_is_rejected(self):
        response = self._post_as_rendered(**{"line_items-0-DELETE": "on"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.order.line_items.count(), 1)
