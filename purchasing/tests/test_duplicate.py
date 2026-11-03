from decimal import Decimal

from django.test import TestCase

from core.models import Company
from products.models import Product, ProductVariant
from purchasing.duplicate import duplicate_purchase_order
from purchasing.models import POLineItem, PurchaseOrder, Supplier


class DuplicatePurchaseOrderTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.supplier = Supplier.objects.create(
            company_name="Acme Corp", supplier_code="ACME", company=self.company,
        )
        product = Product.objects.create(title="Panel", part_number="PNL-1", slug="pnl-1")
        self.variant = ProductVariant.objects.create(product=product, sku="PNL-1-A")

        self.source = PurchaseOrder.objects.create(
            company=self.company,
            supplier=self.supplier,
            status=PurchaseOrder.Status.RECEIVED,
            receiving_status=PurchaseOrder.ReceivingStatus.RECEIVED,
            payment_status=PurchaseOrder.PaymentStatus.PAID,
            amount_paid=Decimal("500.00"),
            grand_total=Decimal("500.00"),
            tracking_number="1Z999",
            vendor_invoice_number="INV-X",
            received_qty_total=Decimal("10"),
            internal_notes="copy me",
            currency="USD",
        )
        POLineItem.objects.create(
            purchase_order=self.source,
            variant=self.variant,
            item_name="Panel",
            our_sku="PNL-1-A",
            supplier_sku="ACME-PNL",
            quantity_ordered=Decimal("10"),
            quantity_received=Decimal("10"),
            unit_cost=Decimal("50.00"),
            line_total=Decimal("500.00"),
            receiving_status=POLineItem.ReceivingStatus.RECEIVED,
        )

    def test_duplicate_resets_status_and_receiving(self):
        new_po = duplicate_purchase_order(self.source)

        self.assertNotEqual(new_po.id, self.source.id)
        self.assertNotEqual(new_po.po_number, self.source.po_number)
        self.assertTrue(new_po.po_number.startswith("PO-"))

        self.assertEqual(new_po.status, PurchaseOrder.Status.DRAFT)
        self.assertEqual(new_po.receiving_status, PurchaseOrder.ReceivingStatus.NOT_RECEIVED)
        self.assertEqual(new_po.payment_status, PurchaseOrder.PaymentStatus.UNPAID)
        self.assertEqual(new_po.amount_paid, Decimal("0"))
        self.assertEqual(new_po.received_qty_total, Decimal("0"))
        self.assertEqual(new_po.tracking_number, "")
        self.assertEqual(new_po.vendor_invoice_number, "")

        # Copied
        self.assertEqual(new_po.supplier, self.supplier)
        self.assertEqual(new_po.internal_notes, "copy me")
        self.assertEqual(new_po.grand_total, Decimal("500.00"))
        self.assertEqual(new_po.amended_from_id, self.source.id)

    def test_duplicate_copies_lines_with_receiving_zeroed(self):
        new_po = duplicate_purchase_order(self.source)
        lines = list(new_po.line_items.all())
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line.our_sku, "PNL-1-A")
        self.assertEqual(line.quantity_ordered, Decimal("10.000"))
        self.assertEqual(line.unit_cost, Decimal("50.0000"))
        self.assertEqual(line.quantity_received, Decimal("0"))
        self.assertEqual(line.receiving_status, POLineItem.ReceivingStatus.NOT_RECEIVED)

    def test_idempotent_and_source_untouched(self):
        a = duplicate_purchase_order(self.source)
        b = duplicate_purchase_order(self.source)
        self.assertNotEqual(a.id, b.id)
        self.assertNotEqual(a.po_number, b.po_number)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, PurchaseOrder.Status.RECEIVED)
        self.assertEqual(self.source.tracking_number, "1Z999")
