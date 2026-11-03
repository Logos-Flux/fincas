"""Tenant-specific settings must fail closed, not default to hardcoded values.

Settings shared across instances must not default to live tenant values:

  SALES_NOTIFICATION_EMAIL -> BCC internal emails
  STOREFRONT_URL           -> print pay-by-link URLs on invoices

Both default to "".
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from core import tripwire_patterns
from core.models import Company
from customers.models import Customer
from documents.services.pdf import render_invoice_html
from orders.models import Order, OrderLineItem, SalesInvoice

#: golden's project settings and the packaged base settings it builds on.
SETTINGS_DIRS = (
    Path(settings.BASE_DIR) / "config" / "settings",
    Path(settings.BASE_DIR) / "golden_erp" / "settings",
)

#: Anything that identifies a specific deployment rather than the software:
#: every isolation tripwire pattern (deployment names and people's mailboxes
#: live in scripts/release/tripwire-*.txt, not in code).
TRIPWIRE_VOCABULARY = tripwire_patterns.compile_all(settings.BASE_DIR)


class SettingsCarryNoInstanceIdentityTests(SimpleTestCase):
    def test_no_settings_module_names_a_real_person_or_domain(self):
        offenders = []
        for path in sorted(p for d in SETTINGS_DIRS for p in d.glob("*.py")):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if TRIPWIRE_VOCABULARY is not None and TRIPWIRE_VOCABULARY.search(line):
                    offenders.append(f"{path.name}:{lineno}: {stripped}")
        self.assertEqual(
            offenders, [],
            "config/settings/ is shared by every deployment cloned from this "
            "repo. Offenders:\n" + "\n".join(offenders),
        )

    def test_the_two_known_leaks_default_to_empty(self):
        self.assertEqual(settings.SALES_NOTIFICATION_EMAIL, "")
        self.assertEqual(settings.STOREFRONT_URL, "")


class PayLinkFailsClosedTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme, Inc")
        customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="pay@example.com", company=self.company,
        )
        order = Order.objects.create(
            order_number="SO-TEST-1", customer=customer, company=self.company,
            grand_total=Decimal("100.00"),
        )
        OrderLineItem.objects.create(
            order=order, sku="X", item_name="Thing", quantity=Decimal("1"),
            unit_price_ex_tax=Decimal("100.0000"), line_total_ex_tax=Decimal("100.00"),
        )
        self.invoice = SalesInvoice.objects.create(
            invoice_number="INV-TEST-1", order=order, company=self.company,
            grand_total=Decimal("100.00"), balance_due=Decimal("100.00"),
        )

    @override_settings(STOREFRONT_URL="")
    def test_no_pay_link_is_printed_when_unset(self):
        html = render_invoice_html(self.invoice)
        self.assertNotIn("/pay/", html)
        self.assertNotIn("Pay Online", html)

    @override_settings(STOREFRONT_URL="")
    def test_never_prints_a_relative_pay_url(self):
        html = render_invoice_html(self.invoice)
        self.assertNotRegex(html, r'href="/pay/')

    @override_settings(STOREFRONT_URL="https://example-tenant.com")
    def test_pay_link_uses_the_configured_host(self):
        html = render_invoice_html(self.invoice)
        self.assertIn("https://example-tenant.com/pay/", html)
        self.assertNotIn("wrong-host.com/pay/", html)
