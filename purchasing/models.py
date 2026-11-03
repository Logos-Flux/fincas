from decimal import ROUND_HALF_UP, Decimal

from django.db import models
from django.db.models import Max
from django.utils import timezone

from core.managers import CompanyScopedManager
from core.models import BaseModel


def _q2(value):
    """Quantize a Decimal to 2 places (matches DB column scale)."""
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _q4(value):
    """Quantize a Decimal to 4 places (landed_cost_per_unit scale)."""
    return Decimal(value).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


class Supplier(BaseModel):
    """Supplier/vendor (61 fields)."""

    class SupplierType(models.TextChoices):
        MANUFACTURER = "manufacturer", "Manufacturer"
        DISTRIBUTOR = "distributor", "Distributor"
        OEM = "oem", "OEM"
        CONTRACT_MFG = "contract_manufacturer", "Contract Manufacturer"
        DROP_SHIP = "drop_ship", "Drop Ship"
        BROKER = "broker", "Broker"
        WAREHOUSE = "warehouse", "Warehouse (Internal)"

    supplier_code = models.CharField(max_length=20, unique=True)
    company_name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True)
    supplier_type = models.CharField(max_length=25, choices=SupplierType.choices)
    is_active = models.BooleanField(default=True)
    is_approved = models.BooleanField(default=False)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        "core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="approved_suppliers",
    )

    # Primary contact
    primary_contact_name = models.CharField(max_length=255, blank=True)
    primary_contact_email = models.EmailField(blank=True)
    primary_contact_phone = models.CharField(max_length=50, blank=True)
    po_email = models.EmailField(blank=True)
    accounts_email = models.EmailField(blank=True)
    website = models.URLField(blank=True)
    dealer_portal_url = models.URLField(blank=True)
    dealer_portal_login = models.CharField(max_length=255, blank=True)

    # Address
    address_line_1 = models.CharField(max_length=255, blank=True)
    address_line_2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state_province = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=2, default="US")

    # Ship-from
    default_ship_from_address = models.ForeignKey(
        "customers.CustomerAddress", on_delete=models.SET_NULL, null=True, blank=True,
    )
    ship_from_locations = models.JSONField(default=list, blank=True)

    # Commercial
    payment_terms = models.CharField(max_length=100, blank=True)
    payment_method_preferred = models.CharField(max_length=50, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    incoterms = models.CharField(max_length=20, blank=True)
    default_lead_time_days = models.IntegerField(null=True, blank=True)
    minimum_order_value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    free_freight_threshold = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    freight_account_number = models.CharField(max_length=100, blank=True)

    # Tax & credit
    tax_id = models.CharField(max_length=50, blank=True)
    w9_on_file = models.BooleanField(default=False)
    w9_document_url = models.URLField(blank=True)
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    credit_terms_notes = models.TextField(blank=True)

    # Catalog
    price_list_url = models.URLField(blank=True)
    price_list_date = models.DateField(null=True, blank=True)
    discount_schedule = models.JSONField(default=dict, blank=True)
    supports_drop_ship = models.BooleanField(default=False)
    drop_ship_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    brands_carried = models.TextField(blank=True)

    # Performance
    on_time_delivery_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    quality_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    total_po_count = models.IntegerField(default=0)
    total_po_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    last_po_date = models.DateField(null=True, blank=True)
    avg_lead_time_actual = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    damage_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    return_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    # Internal
    account_manager = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    tags = models.JSONField(default=list, blank=True)

    # Company scoping
    company = models.ForeignKey(
        "core.Company", on_delete=models.CASCADE, null=True, blank=True,
    )

    # Warehouse link (for "warehouse" supplier type — treats a warehouse as a supplier)
    warehouse = models.OneToOneField(
        "inventory.Warehouse", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="supplier_identity",
    )

    # Migration
    erpnext_name = models.CharField(max_length=255, blank=True, db_index=True)
    bc_supplier_id = models.CharField(max_length=64, blank=True)

    objects = CompanyScopedManager()

    class Meta:
        ordering = ["company_name"]

    def __str__(self):
        return self.display_name or self.company_name

    def save(self, *args, **kwargs):
        if not self.supplier_code:
            last_num = Supplier.objects.aggregate(m=Max("supplier_code"))["m"]
            num = 1
            if last_num:
                try:
                    num = int(last_num.replace("SUP-", "")) + 1
                except ValueError:
                    pass
            self.supplier_code = f"SUP-{num:03d}"
        super().save(*args, **kwargs)


class PurchaseOrder(BaseModel):
    """Purchase order (70 fields)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING_APPROVAL = "pending_approval", "Pending Approval"
        APPROVED = "approved", "Approved"
        SENT = "sent", "Sent"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        IN_PRODUCTION = "in_production", "In Production"
        SHIPPED = "shipped", "Shipped"
        RECEIVING = "receiving", "Receiving"
        RECEIVED = "received", "Received"
        CLOSED = "closed", "Closed"
        CANCELLED = "cancelled", "Canceled"

    class POType(models.TextChoices):
        DROP_SHIP = "drop_ship", "Drop Ship"
        STOCK_REPLENISHMENT = "stock_replenishment", "Stock Replenishment"
        SPECIAL_ORDER = "special_order", "Special Order"
        RETURN_TO_VENDOR = "return_to_vendor", "Return to Vendor"

    class ReceivingStatus(models.TextChoices):
        NOT_RECEIVED = "not_received", "Not Received"
        PARTIAL = "partial", "Partial"
        RECEIVED = "received", "Received"
        OVER_RECEIVED = "over_received", "Over-Received"

    class PaymentStatus(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        PARTIAL = "partial", "Partial"
        PAID = "paid", "Paid"

    # Identification
    po_number = models.CharField(max_length=20, unique=True)
    po_type = models.CharField(max_length=25, choices=POType.choices, default=POType.STOCK_REPLENISHMENT)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    status_note = models.TextField(blank=True)

    # Supplier
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True, related_name="purchase_orders")
    supplier_contact_name = models.CharField(max_length=255, blank=True)
    supplier_contact_email = models.EmailField(blank=True)
    vendor_quote_reference = models.CharField(max_length=100, blank=True)
    vendor_order_number = models.CharField(max_length=100, blank=True)

    # Source
    sub_order = models.ForeignKey("orders.SubOrder", on_delete=models.SET_NULL, null=True, blank=True)
    order = models.ForeignKey("orders.Order", on_delete=models.SET_NULL, null=True, blank=True)
    shipment_group = models.ForeignKey("orders.ShipmentGroup", on_delete=models.SET_NULL, null=True, blank=True)
    customer_name_snapshot = models.CharField(max_length=255, blank=True)

    # Ship-to
    ship_to_type = models.CharField(max_length=50, blank=True)
    ship_to_address = models.TextField(blank=True)
    ship_to_label = models.CharField(max_length=100, blank=True)
    ship_to_attention = models.CharField(max_length=255, blank=True)

    # Commercial
    incoterms = models.CharField(max_length=20, blank=True)
    vendor_payment_terms = models.CharField(max_length=100, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    exchange_rate = models.DecimalField(max_digits=10, decimal_places=6, default=1)
    price_list = models.ForeignKey("pricing.PriceList", on_delete=models.SET_NULL, null=True, blank=True)

    # People
    requested_by = models.ForeignKey("core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="requested_pos")
    approved_by = models.ForeignKey("core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_pos")
    approved_at = models.DateTimeField(null=True, blank=True)
    buyer = models.ForeignKey("core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="buyer_pos")

    # Dates
    po_date = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_via = models.CharField(max_length=50, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    requested_delivery_date = models.DateField(null=True, blank=True)
    vendor_estimated_ship_date = models.DateField(null=True, blank=True)
    actual_ship_date = models.DateTimeField(null=True, blank=True)
    expected_delivery_date = models.DateField(null=True, blank=True)
    actual_delivery_date = models.DateTimeField(null=True, blank=True)

    # Totals
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    other_charges = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    # Receiving
    receiving_status = models.CharField(max_length=20, choices=ReceivingStatus.choices, default=ReceivingStatus.NOT_RECEIVED)
    received_qty_total = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    ordered_qty_total = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    receiving_notes = models.TextField(blank=True)

    # Shipping
    shipping_method = models.CharField(max_length=100, blank=True)
    carrier = models.CharField(max_length=100, blank=True)
    tracking_number = models.CharField(max_length=255, blank=True)
    tracking_url = models.URLField(blank=True)
    bol_number = models.CharField(max_length=100, blank=True)
    package_count = models.IntegerField(default=0)
    total_weight = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Payment
    payment_status = models.CharField(max_length=10, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    balance_due = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_due_date = models.DateField(null=True, blank=True)
    vendor_invoice_number = models.CharField(max_length=100, blank=True)
    vendor_invoice_date = models.DateField(null=True, blank=True)
    vendor_invoice_url = models.URLField(blank=True)

    # Internal
    company = models.ForeignKey("core.Company", on_delete=models.CASCADE)
    warehouse = models.ForeignKey("inventory.Warehouse", on_delete=models.SET_NULL, null=True, blank=True)
    internal_notes = models.TextField(blank=True)
    vendor_notes = models.TextField(blank=True)
    include_standard_terms = models.BooleanField(default=False)
    po_document_url = models.URLField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    tags = models.JSONField(default=list, blank=True)
    # Audit link to the source PO when this row was created via the duplicate
    # action.
    amended_from = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="duplicates")

    objects = CompanyScopedManager()

    class Meta:
        ordering = ["-po_date"]

    def __str__(self):
        return self.po_number

    def save(self, *args, **kwargs):
        if not self.po_number:
            last_num = PurchaseOrder.objects.aggregate(m=Max("po_number"))["m"]
            num = 1
            if last_num:
                try:
                    num = int(last_num.replace("PO-", "")) + 1
                except ValueError:
                    pass
            self.po_number = f"PO-{num:05d}"
        super().save(*args, **kwargs)

    #: Guarded status transitions. Terminal states (closed / cancelled) have
    #: no outbound moves. Direct ORM/ERP-form writes bypass this map; the DRF
    #: ``transition`` action enforces it (illegal moves → 400 with a
    #: field-error envelope), and the detail serializer rejects direct
    #: ``status`` PATCHes outright.
    VALID_TRANSITIONS = {
        "draft": ["pending_approval", "sent", "cancelled"],
        "pending_approval": ["approved", "draft", "cancelled"],
        "approved": ["sent", "cancelled"],
        "sent": ["acknowledged", "cancelled"],
        "acknowledged": ["in_production", "shipped", "cancelled"],
        "in_production": ["shipped", "cancelled"],
        "shipped": ["receiving", "received"],
        "receiving": ["received"],
        "received": ["closed"],
        "closed": [],
        "cancelled": [],
    }

    def transition_to(self, new_status):
        """Validate and apply a status transition."""
        allowed = self.VALID_TRANSITIONS.get(self.status, [])
        if new_status not in allowed:
            from django.core.exceptions import ValidationError
            raise ValidationError(
                f"Cannot transition from '{self.status}' to '{new_status}'. "
                f"Allowed: {allowed}"
            )
        self.status = new_status
        self.save(update_fields=["status", "updated_at"])

    def recalculate_totals(self):
        """Recompute aggregate totals from active line items.

        Per-line writes use `.update()` to bypass post_save signals and
        avoid recursion back into this method.

        Landed cost (F-016) is allocated here, weighted by line value:
        each line absorbs ``shipping_cost`` / ``other_charges`` in
        proportion to its share of subtotal. ``duty_amount`` stays a
        caller-written per-line input (customs actuals); ``freight``,
        ``other`` and ``landed_cost_per_unit`` are server-computed.
        """
        subtotal = Decimal("0")
        tax = Decimal("0")
        rows = []
        for line in self.line_items.all():
            qty = Decimal(line.quantity_ordered or 0)
            unit = Decimal(line.unit_cost or 0)
            disc_pct = Decimal(line.discount_percent or 0)
            disc_amt = Decimal(line.discount_amount or 0)
            if disc_pct > 0:
                disc_amt = _q2(qty * unit * disc_pct / Decimal(100))
            line_total = _q2((qty * unit) - disc_amt)
            updates = {}
            if line_total != Decimal(line.line_total or 0):
                updates["line_total"] = line_total
            if disc_pct > 0 and disc_amt != Decimal(line.discount_amount or 0):
                updates["discount_amount"] = disc_amt
            if updates:
                updates["updated_at"] = timezone.now()
                POLineItem.objects.filter(pk=line.pk).update(**updates)
            subtotal += line_total
            tax += Decimal(line.tax_amount or 0)
            rows.append((line, qty, unit, line_total))

        subtotal = _q2(subtotal)
        tax = _q2(tax)

        self._allocate_landed_cost(rows, subtotal)

        discount_amount = Decimal(self.discount_amount or 0)
        if Decimal(self.discount_percent or 0) > 0:
            discount_amount = _q2(subtotal * Decimal(self.discount_percent) / Decimal(100))

        shipping = Decimal(self.shipping_cost or 0)
        other = Decimal(self.other_charges or 0)
        grand_total = _q2(subtotal - discount_amount + tax + shipping + other)

        amount_paid = Decimal(self.amount_paid or 0)
        balance_due = _q2(grand_total - amount_paid)

        self.subtotal = subtotal
        self.tax_total = tax
        self.discount_amount = discount_amount
        self.grand_total = grand_total
        self.balance_due = balance_due
        self.save(
            update_fields=[
                "subtotal",
                "tax_total",
                "discount_amount",
                "grand_total",
                "balance_due",
                "updated_at",
            ]
        )

    def _allocate_landed_cost(self, rows, subtotal):
        """Allocate PO-level freight/other across lines by line value.

        ``rows`` is ``(line, qty, unit, line_total)`` per line item.
        Zero subtotal (no lines, or all-zero values) allocates nothing —
        every line keeps unit cost as its landed basis. Writes use
        `.update()` (no signals, no recursion), and only changed values
        are written, mirroring the totals pass above.
        """
        pool_freight = Decimal(self.shipping_cost or 0)
        pool_other = Decimal(self.other_charges or 0)
        for line, qty, unit, line_total in rows:
            weight = line_total / subtotal if subtotal > 0 else Decimal("0")
            freight = _q2(pool_freight * weight)
            other = _q2(pool_other * weight)
            duty = Decimal(line.duty_amount or 0)
            if qty > 0:
                landed = _q4(unit + (freight + duty + other) / qty)
            else:
                landed = _q4(unit)
            updates = {}
            if freight != Decimal(line.freight_allocation or 0):
                updates["freight_allocation"] = freight
            if other != Decimal(line.other_landed_costs or 0):
                updates["other_landed_costs"] = other
            if landed != Decimal(line.landed_cost_per_unit or 0):
                updates["landed_cost_per_unit"] = landed
            if updates:
                updates["updated_at"] = timezone.now()
                POLineItem.objects.filter(pk=line.pk).update(**updates)


class POLineItem(BaseModel):
    """Line item on a purchase order (38 fields)."""

    class ReceivingStatus(models.TextChoices):
        NOT_RECEIVED = "not_received", "Not Received"
        PARTIAL = "partial", "Partial"
        RECEIVED = "received", "Received"
        OVER_RECEIVED = "over_received", "Over-Received"

    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="line_items")
    sort_order = models.IntegerField(default=0)

    # Product
    variant = models.ForeignKey("products.ProductVariant", on_delete=models.PROTECT)
    supplier_product = models.ForeignKey("products.SupplierProduct", on_delete=models.SET_NULL, null=True, blank=True)
    item_name = models.CharField(max_length=255)
    supplier_sku = models.CharField(max_length=100, blank=True)
    our_sku = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)

    # Quantity
    quantity_ordered = models.DecimalField(max_digits=10, decimal_places=3)
    purchasing_uom = models.CharField(max_length=20, default="each")
    qty_per_uom = models.IntegerField(default=1)
    quantity_received = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    quantity_rejected = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    quantity_returned = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    receiving_status = models.CharField(max_length=20, choices=ReceivingStatus.choices, default=ReceivingStatus.NOT_RECEIVED)

    # Pricing
    unit_cost = models.DecimalField(max_digits=10, decimal_places=4)
    list_cost = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Landed cost
    landed_cost_per_unit = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    freight_allocation = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    duty_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    other_landed_costs = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Source
    order_line_item = models.ForeignKey("orders.OrderLineItem", on_delete=models.SET_NULL, null=True, blank=True)
    customer_order_number = models.CharField(max_length=100, blank=True)

    # Dates
    requested_delivery_date = models.DateField(null=True, blank=True)
    vendor_estimated_date = models.DateField(null=True, blank=True)
    actual_received_date = models.DateField(null=True, blank=True)

    # Weight
    item_weight = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    line_total_weight = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["sort_order"]

    def __str__(self):
        return f"{self.purchase_order} — {self.our_sku or self.supplier_sku}"


class POReceipt(BaseModel):
    """Receipt header for a PO delivery (17 fields)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        VERIFIED = "verified", "Verified"
        POSTED = "posted", "Posted"

    class Condition(models.TextChoices):
        GOOD = "good", "Good"
        MINOR_DAMAGE = "minor_damage", "Minor Damage"
        MAJOR_DAMAGE = "major_damage", "Major Damage"
        REFUSED = "refused", "Refused"

    receipt_number = models.CharField(max_length=20, unique=True)
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="receipts")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    received_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey("core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True)
    warehouse = models.ForeignKey("inventory.Warehouse", on_delete=models.SET_NULL, null=True, blank=True)

    carrier = models.CharField(max_length=100, blank=True)
    tracking_number = models.CharField(max_length=255, blank=True)
    bol_number = models.CharField(max_length=100, blank=True)
    package_count = models.IntegerField(default=0)
    condition_on_arrival = models.CharField(max_length=15, choices=Condition.choices, default=Condition.GOOD)
    damage_photos_urls = models.JSONField(default=list, blank=True)
    delivery_document_url = models.URLField(blank=True)

    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return self.receipt_number

    def save(self, *args, **kwargs):
        if not self.receipt_number:
            last_num = POReceipt.objects.aggregate(m=Max("receipt_number"))["m"]
            num = 1
            if last_num:
                try:
                    num = int(last_num.replace("REC-", "")) + 1
                except ValueError:
                    pass
            self.receipt_number = f"REC-{num:05d}"
        previous = (
            POReceipt.objects.filter(pk=self.pk)
            .values_list("status", flat=True)
            .first()
            if self.pk
            else None
        )
        # Post BEFORE the doc save (see SalesInvoice.save): idempotent.
        if self.status in (self.Status.VERIFIED, self.Status.POSTED) and (
            previous not in (self.Status.VERIFIED, self.Status.POSTED)
        ):
            from .posting import post_receipt

            post_receipt(self)
        super().save(*args, **kwargs)


class POReceiptLine(BaseModel):
    """Line item on a receipt (13 fields)."""

    class InspectionStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"
        WAIVED = "waived", "Waived"

    receipt = models.ForeignKey(POReceipt, on_delete=models.CASCADE, related_name="lines")
    po_line_item = models.ForeignKey(POLineItem, on_delete=models.CASCADE)
    quantity_received = models.DecimalField(max_digits=10, decimal_places=3)
    quantity_rejected = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    rejection_reason = models.CharField(max_length=255, blank=True)
    rejection_notes = models.TextField(blank=True)
    lot_number = models.CharField(max_length=100, blank=True)
    serial_numbers = models.JSONField(default=list, blank=True)
    inspection_status = models.CharField(max_length=10, choices=InspectionStatus.choices, default=InspectionStatus.PENDING)
    storage_location = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["receipt", "po_line_item"]

    def __str__(self):
        return f"{self.receipt} — {self.po_line_item}"
