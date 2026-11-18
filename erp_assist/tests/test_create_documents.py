"""Creating documents through the assistant: orders, invoices, quote lines,
purchase orders and party contact points, against the real DRF API.

Same in-process ASGI bridge as ``test_api_e2e`` (see its docstring for why
this is a TransactionTestCase).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
from django.contrib.auth.models import User
from django.test import TransactionTestCase, override_settings

from config.asgi import application
from core.models import Company
from customers.models import Customer
from erp_assist import mcp_adapter
from erp_assist.core import ERPClient, ERPConfig, ERPValidationError
from erp_assist.core import orders as orders_core
from erp_assist.core import parties as parties_core
from erp_assist.core import purchasing as purchasing_core
from orders.models import Order, OrderLineItem, Quote, QuoteLineItem, SalesInvoice
from parties.models import Party, PartyEmail, PartyRelationship
from products.models import Product, ProductVariant
from purchasing.models import POLineItem, PurchaseOrder, Supplier

API_KEY = "erp-assist-create-key"

FREIGHT = {"item_name": "Freight", "unit_price_ex_tax": "3300.00"}


def run(coro):
    return asyncio.run(coro)


@override_settings(API_KEY=API_KEY)
class _AssistCase(TransactionTestCase):
    def setUp(self):
        User.objects.create_superuser(username="api-service-create", password=None)
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace", email="ada-create@example.com",
            company=self.company,
        )
        product = Product.objects.create(title="Widget", part_number="W-1", slug="widget-1")
        self.variant = ProductVariant.objects.create(
            product=product, sku="W-1-A", name="Widget A", retail_price=Decimal("25.00"),
        )
        config = ERPConfig(base_url="http://testserver", api_key=API_KEY)
        client = ERPClient.__new__(ERPClient)
        client._config = config
        client._http = httpx.AsyncClient(
            transport=httpx.ASGITransport(application),
            base_url="http://testserver",
            follow_redirects=True,
            headers={
                "Authorization": f"Api-Key {API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        client.lookup_cache = ERPClient(config).lookup_cache
        self.client = client

    def tearDown(self):
        run(self.client._http.aclose())

    @property
    def customer_id(self):
        return str(self.customer.pk)


class CreateOrderTests(_AssistCase):
    def test_free_form_line_makes_an_order_with_totals(self):
        order = run(
            orders_core.create_order(self.client, customer=self.customer_id, line_items=[FREIGHT])
        )
        self.assertTrue(order["order_number"])
        self.assertEqual(order["grand_total"], "3300.00")
        self.assertEqual(order["company"], str(self.company.pk))
        (line,) = order["line_items"]
        self.assertEqual(line["sku"], orders_core.FREE_FORM_SKU)
        self.assertIsNone(line["variant"])
        self.assertEqual(Decimal(line["quantity"]), Decimal("1"))
        self.assertEqual(line["line_total_ex_tax"], "3300.00")

    def test_variant_line_defaults_name_sku_and_price_from_the_catalog(self):
        order = run(
            orders_core.create_order(
                self.client, customer=self.customer_id,
                line_items=[{"variant": str(self.variant.pk), "quantity": 4}],
            )
        )
        (line,) = order["line_items"]
        self.assertEqual((line["item_name"], line["sku"]), ("Widget A", "W-1-A"))
        self.assertEqual(Decimal(line["unit_price_ex_tax"]), Decimal("25.00"))
        self.assertEqual(order["grand_total"], "100.00")

    def test_a_bad_line_creates_nothing(self):
        for bad in (
            {"item_name": "No price"},
            {"unit_price_ex_tax": "5.00"},
            {**FREIGHT, "line_total_ex_tax": "1.00"},
        ):
            with self.subTest(line=bad), self.assertRaises(ERPValidationError):
                run(
                    orders_core.create_order(
                        self.client, customer=self.customer_id, line_items=[FREIGHT, bad]
                    )
                )
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderLineItem.objects.count(), 0)

    def test_idempotency_key_replays_the_order_without_doubling_lines(self):
        kwargs = dict(customer=self.customer_id, line_items=[FREIGHT], idempotency_key="blu-1")
        first = run(orders_core.create_order(self.client, **kwargs))
        again = run(orders_core.create_order(self.client, **kwargs))
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderLineItem.objects.count(), 1)
        self.assertEqual(again["grand_total"], "3300.00")

    def test_add_and_delete_a_line_recalculates(self):
        order = run(
            orders_core.create_order(self.client, customer=self.customer_id, line_items=[FREIGHT])
        )
        order = run(
            orders_core.create_order_line(
                self.client, order["id"], {"variant": str(self.variant.pk), "quantity": 2}
            )
        )
        self.assertEqual(order["grand_total"], "3350.00")
        widget = next(li for li in order["line_items"] if li["sku"] == "W-1-A")
        order = run(orders_core.delete_order_line(self.client, widget["id"]))
        self.assertEqual(order["grand_total"], "3300.00")
        self.assertEqual(len(order["line_items"]), 1)


class CreateInvoiceTests(_AssistCase):
    def test_create_invoice_makes_the_order_and_a_draft_invoice(self):
        result = run(
            orders_core.create_invoice(
                self.client, customer=self.customer_id, line_items=[FREIGHT],
                payment_terms="due_on_receipt", notes="Freight for the March shipment",
            )
        )
        invoice, order = result["invoice"], result["order"]
        self.assertEqual(invoice["status"], SalesInvoice.Status.DRAFT)
        self.assertEqual(invoice["grand_total"], "3300.00")
        self.assertEqual(invoice["order"], order["id"])
        self.assertEqual(invoice["notes"], "Freight for the March shipment")
        self.assertTrue(invoice["invoice_number"])
        self.assertEqual(SalesInvoice.objects.count(), 1)

    def test_replay_returns_the_same_pair(self):
        kwargs = dict(customer=self.customer_id, line_items=[FREIGHT], idempotency_key="inv-1")
        first = run(orders_core.create_invoice(self.client, **kwargs))
        again = run(orders_core.create_invoice(self.client, **kwargs))
        self.assertEqual(again["invoice"]["id"], first["invoice"]["id"])
        self.assertEqual((Order.objects.count(), SalesInvoice.objects.count()), (1, 1))

    def test_no_lines_is_refused_before_anything_is_written(self):
        with self.assertRaises(ERPValidationError):
            run(orders_core.create_invoice(self.client, customer=self.customer_id, line_items=[]))
        self.assertEqual(Order.objects.count(), 0)

    def test_list_get_and_update(self):
        made = run(
            orders_core.create_invoice(self.client, customer=self.customer_id, line_items=[FREIGHT])
        )["invoice"]
        listed = run(orders_core.list_invoices(self.client, status="draft"))
        self.assertEqual([i["id"] for i in listed], [made["id"]])
        self.assertEqual(run(orders_core.get_invoice(self.client, made["id"]))["id"], made["id"])
        updated = run(
            orders_core.update_invoice(
                self.client, made["id"], {"due_date": "2031-01-31", "internal_notes": "checked"}
            )
        )
        self.assertEqual(updated["due_date"], "2031-01-31")
        self.assertEqual(updated["internal_notes"], "checked")

    def test_amounts_are_not_writable(self):
        made = run(
            orders_core.create_invoice(self.client, customer=self.customer_id, line_items=[FREIGHT])
        )["invoice"]
        with self.assertRaises(ERPValidationError):
            run(orders_core.update_invoice(self.client, made["id"], {"grand_total": "1.00"}))
        self.assertEqual(SalesInvoice.objects.get().grand_total, Decimal("3300.00"))


class QuoteTests(_AssistCase):
    def test_free_form_quote_line_then_convert_to_order(self):
        quote = run(
            orders_core.create_quote(
                self.client, customer=self.customer_id,
                line_items=[{"item_name": "Site survey", "unit_price_ex_tax": "450.00", "notes": "half day"}],
            )
        )
        self.assertEqual(quote["grand_total"], "450.00")
        self.assertEqual(QuoteLineItem.objects.get().line_notes, "half day")
        order = run(orders_core.convert_quote_to_order(self.client, quote["id"]))
        self.assertTrue(order["created"])
        self.assertEqual(order["quote"], quote["id"])
        self.assertEqual([li["item_name"] for li in order["line_items"]], ["Site survey"])
        again = run(orders_core.convert_quote_to_order(self.client, quote["id"]))
        self.assertFalse(again["created"])
        self.assertEqual(again["id"], order["id"])
        self.assertEqual(Order.objects.count(), 1)

    def test_a_bad_quote_line_leaves_no_quote(self):
        with self.assertRaises(ERPValidationError):
            run(
                orders_core.create_quote(
                    self.client, customer=self.customer_id, line_items=[{"item_name": "No price"}]
                )
            )
        self.assertEqual(Quote.objects.count(), 0)

    def test_add_and_delete_a_quote_line(self):
        quote = run(orders_core.create_quote(self.client, customer=self.customer_id))
        quote = run(
            orders_core.create_quote_line(
                self.client, quote["id"], {"variant": str(self.variant.pk), "quantity": 3}
            )
        )
        self.assertEqual(quote["grand_total"], "75.00")
        quote = run(orders_core.delete_quote_line(self.client, quote["line_items"][0]["id"]))
        self.assertEqual(quote["grand_total"], "0.00")
        self.assertEqual(quote["line_items"], [])


class PurchaseOrderTests(_AssistCase):
    def setUp(self):
        super().setUp()
        self.supplier = Supplier.objects.create(
            company_name="Acme Corp", supplier_code="ACME-CR", company=self.company,
        )

    def _line(self, **extra):
        return {"variant": str(self.variant.pk), "quantity_ordered": 10, "unit_cost": "12.50", **extra}

    def test_create_purchase_order_numbers_itself_and_totals(self):
        po = run(
            purchasing_core.create_purchase_order(
                self.client, supplier=str(self.supplier.pk), line_items=[self._line()],
            )
        )
        self.assertRegex(po["po_number"], r"^PO-\d+$")
        self.assertEqual(po["status"], PurchaseOrder.Status.DRAFT)
        self.assertEqual(po["company"], str(self.company.pk))
        self.assertEqual(po["grand_total"], "125.00")
        (line,) = po["line_items"]
        self.assertEqual((line["item_name"], line["our_sku"]), ("Widget A", "W-1-A"))

    def test_quantity_is_accepted_as_an_alias(self):
        line = {"variant": str(self.variant.pk), "quantity": 2, "unit_cost": "1.00"}
        po = run(
            purchasing_core.create_purchase_order(
                self.client, supplier=str(self.supplier.pk), line_items=[line],
            )
        )
        self.assertEqual(Decimal(po["line_items"][0]["quantity_ordered"]), Decimal("2"))

    def test_a_line_without_a_variant_creates_nothing(self):
        with self.assertRaises(ERPValidationError) as caught:
            run(
                purchasing_core.create_purchase_order(
                    self.client, supplier=str(self.supplier.pk),
                    line_items=[{"item_name": "Freight", "quantity_ordered": 1, "unit_cost": "5"}],
                )
            )
        self.assertIn("variant", str(caught.exception))
        self.assertEqual(PurchaseOrder.objects.count(), 0)

    def test_add_and_delete_a_po_line(self):
        po = run(purchasing_core.create_purchase_order(self.client, supplier=str(self.supplier.pk)))
        po = run(purchasing_core.create_po_line(self.client, po["id"], self._line()))
        self.assertEqual(po["grand_total"], "125.00")
        po = run(purchasing_core.delete_po_line(self.client, po["line_items"][0]["id"]))
        self.assertEqual(po["grand_total"], "0.00")
        self.assertEqual(POLineItem.objects.count(), 0)


class PartyContactTests(_AssistCase):
    def test_billing_email_and_a_titled_relationship(self):
        person = self.customer.party
        org = Party.objects.create(
            company=self.company, kind=Party.Kind.ORGANISATION, display_name="Example Aerospace",
        )
        email = run(
            parties_core.add_party_email(
                self.client, str(org.pk), email="invoices@example.com", purpose="billing"
            )
        )
        self.assertEqual(email["purpose"], "billing")
        self.assertTrue(PartyEmail.objects.filter(party=org, purpose="billing").exists())
        rel = run(
            parties_core.create_party_relationship(
                self.client, from_party=str(person.pk), to_party=str(org.pk),
                kind="employment", title="CEO",
            )
        )
        self.assertEqual(rel["title"], "CEO")
        self.assertEqual(PartyRelationship.objects.get(pk=rel["id"]).kind, "employment")
        listed = run(parties_core.list_party_relationships(self.client, str(org.pk)))
        self.assertEqual([r["id"] for r in listed], [rel["id"]])

    def test_add_phone(self):
        phone = run(
            parties_core.add_party_phone(
                self.client, str(self.customer.party.pk), number="+12025550143", purpose="work"
            )
        )
        self.assertEqual(phone["number_e164"], "+12025550143")


class AdapterToolTests(_AssistCase):
    NEW_TOOLS = (
        "create_order", "create_order_line", "delete_order_line", "create_invoice",
        "list_invoices", "get_invoice", "update_invoice", "convert_quote_to_order",
        "create_quote_line", "delete_quote_line", "create_purchase_order", "create_po_line",
        "delete_po_line", "add_party_email", "add_party_phone", "list_party_relationships",
        "create_party_relationship",
    )

    def test_every_new_tool_is_registered(self):
        registered = {tool.name for tool in run(mcp_adapter.mcp.list_tools())}
        self.assertEqual(sorted(set(self.NEW_TOOLS) - registered), [])

    def test_line_deletes_refuse_without_confirm(self):
        for tool in (
            mcp_adapter.delete_order_line, mcp_adapter.delete_quote_line, mcp_adapter.delete_po_line,
        ):
            with self.subTest(tool=tool.__name__):
                self.assertEqual(run(tool("x"))["error"], "ConfirmationRequired")

    def test_a_refused_create_comes_back_as_an_error_envelope(self):
        async def call():
            mcp_adapter._shared_client = self.client
            try:
                return await mcp_adapter.create_invoice(customer=self.customer_id, line_items=[])
            finally:
                mcp_adapter._shared_client = None

        result = run(call())
        self.assertEqual(result["error"], "ERPValidationError")
        self.assertEqual(Order.objects.count(), 0)
