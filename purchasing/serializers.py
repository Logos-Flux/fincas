from rest_framework import serializers

from core.serializers import GuardedDocumentSerializer

from .models import POLineItem, POReceipt, POReceiptLine, PurchaseOrder, Supplier


class SupplierListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = [
            "id", "supplier_code", "company_name", "supplier_type",
            "is_active", "is_approved", "primary_contact_email",
        ]


class SupplierDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class POLineItemSerializer(GuardedDocumentSerializer):
    # Landed allocation is server-computed by recalculate_totals() (F-016) —
    # never caller-written. duty_amount stays writable: it is the one
    # genuine per-line input (customs actuals) and feeds landed_cost_per_unit.
    SERVER_TOTAL_FIELDS = frozenset({
        "freight_allocation",
        "other_landed_costs",
        "landed_cost_per_unit",
    })

    class Meta:
        model = POLineItem
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class PurchaseOrderListSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source="supplier.company_name", read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = [
            "id", "po_number", "supplier", "supplier_name", "po_type",
            "status", "grand_total", "receiving_status", "po_date",
        ]


class PurchaseOrderDetailSerializer(GuardedDocumentSerializer):
    line_items = POLineItemSerializer(many=True, read_only=True)

    # Computed by PurchaseOrder.recalculate_totals() / the payment ledger —
    # never caller-written. Discount inputs, shipping_cost, and other_charges
    # remain writable as calculation inputs.
    SERVER_TOTAL_FIELDS = frozenset({
        "subtotal",
        "tax_total",
        "grand_total",
        "balance_due",
        "amount_paid",
    })

    class Meta:
        model = PurchaseOrder
        fields = "__all__"
        read_only_fields = ["id", "po_date", "created_at", "updated_at"]


class POReceiptLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = POReceiptLine
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class POReceiptSerializer(serializers.ModelSerializer):
    lines = POReceiptLineSerializer(many=True, read_only=True)

    class Meta:
        model = POReceipt
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]
