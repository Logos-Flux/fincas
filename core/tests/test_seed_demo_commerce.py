"""``manage.py seed_demo_commerce``: the demo's catalog and 90 days of trading.

The seed runs once per class (``setUpTestData``) on top of ``seed_demo_data``,
with the clock pinned to a New York morning (the settings zone), so nothing
here depends on the real date.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import Sum
from django.test import TestCase, override_settings

from accounting.models import JournalEntry, JournalLine
from core import demo_clock
from core.management.commands.seed_demo_commerce import CUSTOMERS, PRODUCTS, build_plan, variant_rows
from core.models import Company, OutboxEvent
from crm.models import Engagement
from customers.models import Customer
from fulfillment.models import PickTicket
from inventory.models import WarehouseStock
from orders.models import Order, Quote, SalesInvoice
from payments.models import Payment
from pricing.models.price_book import PriceListItem
from products.models import Brand, Category, Product, ProductVariant
from purchasing.models import POReceipt, PurchaseOrder, Supplier

NY = ZoneInfo("America/New_York")
SEED_DAY = datetime.date(2026, 10, 5)
URL = "/api/products/products/"
BASE_SEED_PARTS = ("ACT-500W", "MLC-CORE-08", "BRK-90-HD", "SNR-LASER-30")
PUBLIC = URL + "?visible_on_website=true&is_active=true"
COUNTED = (Brand, Category, Product, ProductVariant, PriceListItem, WarehouseStock, Supplier, Customer, Quote,
           Order, SalesInvoice, Payment, PurchaseOrder, POReceipt, PickTicket, JournalEntry, Engagement)


def frozen(day, hhmm="10:00"):
    hour, minute = map(int, hhmm.split(":"))
    at = datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=NY).astimezone(datetime.UTC)
    return mock.patch("django.utils.timezone.now", return_value=at)


def seed(*args):
    out = StringIO()
    with frozen(SEED_DAY):
        call_command("seed_demo_commerce", *args, stdout=out)
    return out.getvalue()


def counts():
    return {model.__name__: model._base_manager.count() for model in COUNTED}


class RefusalTests(TestCase):
    @override_settings(DEMO_USERNAMES=[])
    def test_refuses_outside_a_demo_without_force(self):
        with self.assertRaisesMessage(CommandError, "DEMO_USERNAMES is not set"):
            call_command("seed_demo_commerce", stdout=StringIO())

    @override_settings(DEMO_USERNAMES=["demo"])
    def test_refuses_without_the_base_seed(self):
        Company.objects.create(name="Bare Co")
        with self.assertRaisesMessage(CommandError, "run seed_demo_data first"):
            call_command("seed_demo_commerce", stdout=StringIO())


@override_settings(DEMO_USERNAMES=["demo"], DEMO_LOGIN_USERNAME="demo")
class SeedDemoCommerceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with frozen(SEED_DAY):
            call_command("seed_demo_data", stdout=StringIO())
        cls.output = seed()
        cls.company = Company.objects.get(name="Golden Commerce Corp")

    def test_catalog_customers_and_documents(self):
        self.assertIn("Commerce: seeded", self.output)
        self.assertEqual(Product.objects.count(), len(PRODUCTS))
        self.assertEqual(ProductVariant.objects.count(), sum(len(variant_rows(p)) for p in PRODUCTS))
        self.assertEqual(Brand.objects.count(), 8)
        self.assertEqual(Category.objects.count(), 10)
        self.assertTrue(Category.objects.filter(parent__isnull=False).exists())
        self.assertGreaterEqual(Customer.objects.filter(company=self.company).count(), len(CUSTOMERS))
        self.assertGreaterEqual(Supplier.objects.filter(company=self.company).count(), 6)
        plan = build_plan()
        self.assertEqual(Quote.objects.count(), len(plan["quotes"]))
        converted = sum(1 for q in plan["quotes"] if q["outcome"] == "accepted")
        self.assertEqual(Order.objects.count(), len(plan["orders"]) + converted)
        self.assertEqual(PurchaseOrder.objects.count(), len(plan["pos"]))
        # Every lifecycle state the back office shows has rows.
        self.assertTrue({"draft", "sent", "negotiating", "accepted", "lost", "expired"}
                        <= set(Quote.objects.values_list("status", flat=True)))
        self.assertTrue({"pending", "awaiting_fulfillment", "awaiting_shipment", "partially_shipped", "shipped",
                         "completed", "on_hold", "backordered", "cancelled"}
                        <= set(Order.objects.values_list("status", flat=True)))
        self.assertTrue({"sent", "overdue", "partial", "paid"}
                        <= set(SalesInvoice.objects.values_list("status", flat=True)))
        self.assertTrue({"draft", "sent", "acknowledged", "receiving", "received", "closed"}
                        <= set(PurchaseOrder.objects.values_list("status", flat=True)))
        self.assertTrue(PickTicket.objects.filter(status="shipped").exists())
        self.assertTrue(PickTicket.objects.exclude(status="shipped").exists())
        # Stock: in stock, below the reorder level, out of stock.
        self.assertTrue(ProductVariant.objects.filter(stock_status="out_of_stock", stock=0).exists())
        self.assertTrue(WarehouseStock.objects.filter(reorder_level__gt=0, quantity__lte=0).exists())
        self.assertTrue(ProductVariant.objects.filter(stock__gt=50).exists())

    def test_quotes_sit_on_engagements_and_conversions_win_them(self):
        for quote in Quote.objects.select_related("engagement"):
            self.assertIsNotNone(quote.engagement_id, quote.quote_number)
            if quote.status == "accepted":
                self.assertEqual(quote.engagement.status, "won")
                self.assertIsNotNone(quote.converted_to_order_id)
            elif quote.status in ("lost", "expired"):
                self.assertEqual(quote.engagement.status, "lost")
        call_command("check_engagements", stdout=StringIO())
        call_command("reconcile_engagements", "--check", stdout=StringIO())

    def test_second_run_adds_nothing(self):
        before = counts()
        out = seed()
        self.assertIn("nothing to add", out)
        self.assertEqual(counts(), before)
        out = seed("--force")
        self.assertEqual(counts(), before)

    def test_the_books_come_from_the_posting_rules_and_balance(self):
        posted = set(JournalEntry.objects.values_list("posting_key", flat=True))
        for invoice in SalesInvoice.objects.exclude(status="draft"):
            self.assertIn(f"order:{invoice.invoice_number}", posted)
            entry = JournalEntry.objects.get(posting_key=f"order:{invoice.invoice_number}")
            self.assertEqual(entry.entry_date, invoice.invoice_date)
        for payment in Payment.objects.all():
            self.assertIn(f"payment:{payment.payment_number}", posted)
        for receipt in POReceipt.objects.all():
            self.assertIn(f"purchase:{receipt.receipt_number}", posted)
        totals = JournalLine.objects.aggregate(d=Sum("debit_amount"), c=Sum("credit_amount"))
        self.assertEqual(totals["d"], totals["c"])
        out = StringIO()
        call_command("check_money", stdout=out)
        self.assertIn("STATUS: OK", out.getvalue())

    def test_everything_is_dated_before_the_anchor_day(self):
        self.assertEqual(demo_clock.get_anchor(), SEED_DAY)
        start_of_anchor_day = datetime.datetime.combine(SEED_DAY, datetime.time(0), tzinfo=NY)
        oldest = start_of_anchor_day - datetime.timedelta(days=125)
        for model, field in ((Order, "order_date"), (Order, "created_at"), (Quote, "created_at"),
                             (Payment, "payment_date"), (PurchaseOrder, "po_date"), (POReceipt, "received_at"),
                             (Engagement, "created_at"), (Product, "created_at")):
            rows = model._base_manager.filter(**{f"{field}__isnull": False})
            if model is Engagement:
                rows = rows.filter(origin="quote")
            if model is Product:  # seed_demo_data's four were written "now"
                rows = rows.exclude(part_number__in=BASE_SEED_PARTS)
            values = list(rows.values_list(field, flat=True))
            self.assertTrue(values, f"{model.__name__}.{field}")
            self.assertLess(max(values), start_of_anchor_day, f"{model.__name__}.{field}")
            self.assertGreater(min(values), oldest, f"{model.__name__}.{field}")
        dates = list(SalesInvoice.objects.values_list("invoice_date", flat=True))
        self.assertLess(max(dates), SEED_DAY)
        self.assertGreater(min(dates), SEED_DAY - datetime.timedelta(days=95))
        # No lifecycle signal escaped into the outbox.
        self.assertFalse(OutboxEvent.objects.exists())

    def test_receipts_raised_the_stock(self):
        received = POReceipt.objects.values_list("lines__po_line_item__variant__sku", flat=True)
        self.assertTrue(received)
        for sku in set(received):
            variant = ProductVariant.objects.get(sku=sku)
            total = WarehouseStock.objects.filter(variant=variant).aggregate(q=Sum("quantity"))["q"]
            self.assertEqual(variant.stock, int(total))

    def test_the_dates_roll_with_the_demo_clock(self):
        order = Order.objects.order_by("order_date").first()
        before = order.order_date
        with frozen(SEED_DAY + datetime.timedelta(days=3), "00:10"):
            call_command("demo_roll_dates", stdout=StringIO())
        order.refresh_from_db()
        self.assertEqual(order.order_date - before, datetime.timedelta(days=3))


@override_settings(DEMO_USERNAMES=["demo"], DEMO_LOGIN_USERNAME="demo")
class StorefrontCatalogTests(TestCase):
    """What the Qwik storefront reads, anonymously, from the seeded catalog."""

    @classmethod
    def setUpTestData(cls):
        with frozen(SEED_DAY):
            call_command("seed_demo_data", stdout=StringIO())
        seed()

    def rows(self, url):
        data = self.client.get(url).json()
        rows, nxt = data["results"], data.get("next")
        while nxt:
            data = self.client.get(nxt).json()
            rows, nxt = rows + data["results"], data.get("next")
        return rows

    def test_catalog_list_carries_price_stock_brand_and_category(self):
        rows = self.rows(PUBLIC)
        self.assertEqual(len(rows), len(PRODUCTS))
        self.assertTrue(all(r["price"] for r in rows))
        row = next(r for r in rows if r["title"] == "LED High Bay Fixture")
        self.assertEqual(row["price"], "189.00")  # the lowest of its two variants
        self.assertEqual(row["stock_status"], "in_stock")
        self.assertGreater(row["stock_quantity"], 0)
        self.assertEqual(row["brand_slug"], "lumen-bay-lighting")
        self.assertEqual(row["category_slug"], "industrial-lighting")
        self.assertTrue(row["summary"])
        statuses = {r["title"]: r["stock_status"] for r in rows}
        self.assertEqual(statuses["S-Type Load Cell 500 lb"], "out_of_stock")
        self.assertEqual(statuses["Single-Axis Servo Drive 1 kW"], "backorder")
        sale = next(r for r in rows if r["title"] == "Precision Machine Vise 6-inch")
        self.assertEqual(sale["price"], "299.00")  # the sale price wins

    def test_product_detail_has_variants_specs_and_seo(self):
        pk = Product.objects.get(title="DIN-Rail Power Supply 24 V").pk
        data = self.client.get(f"{URL}{pk}/").json()
        self.assertEqual([v["name"] for v in data["variants"]], ["120 W", "240 W", "480 W"])
        self.assertEqual([v["price"] for v in data["variants"]], ["88.00", "132.50", "236.00"])
        self.assertNotIn("wholesale_price", data["variants"][0])  # no costs for the public
        self.assertEqual(data["specifications"]["Output"], "24 VDC, adjustable")
        self.assertEqual(data["seo_title"], "DIN-Rail Power Supply 24 V | Cobalt Ridge Electric")
        self.assertTrue(data["seo_meta_description"])
        self.assertIsNone(data["image_url"])  # no BUNNY_IMAGES_BASE in tests: the placeholder
        product = Product.objects.get(pk=pk)
        self.assertEqual(product.primary_image_id, "demo/products/din-rail-power-supply-24-v.jpg")
        with override_settings(BUNNY_IMAGES_BASE="https://images.example.com"):
            data = self.client.get(f"{URL}{pk}/").json()
        self.assertTrue(data["image_url"].startswith(
            "https://images.example.com/demo__products__din-rail-power-supply-24-v.jpg?"))
        brand = Brand.objects.get(title="Cobalt Ridge Electric")
        self.assertEqual(brand.logo_image_id, "demo/brands/cobalt-ridge-electric-logo.png")
        self.assertEqual(brand.banner_image_id, "demo/brands/cobalt-ridge-electric-banner.jpg")
        self.assertEqual(Category.objects.get(title="Power Distribution").primary_image_id,
                         "demo/categories/power-distribution.jpg")
        self.assertEqual(data["options"][0]["name"], "Power")
        self.assertEqual(len(data["options"][0]["values"]), 3)

    def test_price_breaks_are_on_the_price_lists(self):
        variant = ProductVariant.objects.get(sku="PSU-24-120")
        retail = list(PriceListItem.objects.filter(variant=variant, price_list__name="Standard Retail")
                      .order_by("min_qty").values_list("min_qty", "price"))
        self.assertEqual([q for q, _ in retail], [Decimal("1"), Decimal("10"), Decimal("25")])
        self.assertEqual(retail[0][1], Decimal("88.00"))
        self.assertLess(retail[-1][1], retail[0][1])

    def test_brands_and_categories(self):
        brands = self.rows("/api/products/brands/")
        self.assertEqual(len(brands), 8)
        self.assertTrue(all(b["seo_title"] and b["description"] for b in brands))
        apex = self.rows(f"{PUBLIC}&brand_slug=apex-dynamics")
        self.assertEqual(len(apex), 7)
        categories = {c["slug"]: c for c in self.rows("/api/products/categories/")}
        control = categories["control-electronics"]
        self.assertEqual({c["slug"] for c in control["children"]}, {"motion-control", "sensors-measurement"})
        self.assertTrue(control["seo_title"])
        self.assertEqual(categories["motion-control"]["parent_slug"], "control-electronics")
        # A parent's page lists its subtree.
        subtree = {r["title"] for r in self.rows(f"{PUBLIC}&category_slug=control-electronics")}
        self.assertIn("Compact Stepper Motor NEMA 23", subtree)  # in Motion Control
        self.assertIn("Inductive Proximity Sensor M18", subtree)  # in Sensors & Measurement
