from django.contrib import admin

from .models import POLineItem, POReceipt, POReceiptLine, PurchaseOrder, Supplier


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = [
        "supplier_code", "company_name", "supplier_type",
        "is_active", "is_approved", "primary_contact_email",
    ]
    list_filter = ["supplier_type", "is_active", "is_approved", "supports_drop_ship"]
    search_fields = ["supplier_code", "company_name", "primary_contact_email"]
    readonly_fields = ["id", "created_at", "updated_at"]


class POLineItemInline(admin.TabularInline):
    model = POLineItem
    extra = 0
    fields = [
        "sort_order", "variant", "item_name", "our_sku", "supplier_sku",
        "quantity_ordered", "unit_cost", "line_total", "receiving_status",
    ]
    readonly_fields = ["id"]


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = [
        "po_number", "supplier", "po_type", "status",
        "grand_total", "receiving_status", "po_date",
    ]
    list_filter = ["status", "po_type", "receiving_status", "payment_status", "company"]
    search_fields = ["po_number", "supplier__company_name", "tracking_number"]
    readonly_fields = ["id", "po_date", "created_at", "updated_at"]
    inlines = [POLineItemInline]


class POReceiptLineInline(admin.TabularInline):
    model = POReceiptLine
    extra = 0
    fields = [
        "po_line_item", "quantity_received", "quantity_rejected",
        "inspection_status", "storage_location",
    ]


@admin.register(POReceipt)
class POReceiptAdmin(admin.ModelAdmin):
    list_display = [
        "receipt_number", "purchase_order", "status",
        "condition_on_arrival", "received_at",
    ]
    list_filter = ["status", "condition_on_arrival"]
    search_fields = ["receipt_number", "purchase_order__po_number"]
    readonly_fields = ["id", "created_at", "updated_at"]
    inlines = [POReceiptLineInline]
