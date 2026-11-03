from django.contrib import admin

from .models import Order, OrderLineItem, Quote, QuoteLineItem, SubOrder


class QuoteLineItemInline(admin.TabularInline):
    model = QuoteLineItem
    extra = 0
    fields = [
        "sort_order", "variant", "item_name", "sku", "quantity",
        "unit_price_ex_tax", "line_total_ex_tax", "is_optional", "is_alternative",
    ]
    readonly_fields = ["id"]


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    list_display = [
        "quote_number", "revision_number", "customer", "status",
        "engagement", "grand_total", "created_at",
    ]
    list_filter = ["status", "company"]
    search_fields = ["quote_number", "customer__email", "customer__last_name"]
    # The deal lives on the engagement (CRM Phase 3, spec 1.6); the legacy
    # pipeline columns are shown read-only until step 12 drops them.
    LEGACY_FIELDS = tuple(f"legacy_{name}" for name in Quote.MOVED_TO_ENGAGEMENT)
    readonly_fields = ["id", "created_at", "updated_at", "engagement", *LEGACY_FIELDS]
    raw_id_fields = ["engagement"]
    inlines = [QuoteLineItemInline]
    fieldsets = (
        ("Identification", {"fields": (
            "id", "quote_number", "revision_number", "revision_reason",
            "superseded_by", "original_quote",
        )}),
        ("Status", {"fields": ("status", "status_note")}),
        ("Customer", {"fields": (
            "customer", "billing_address", "shipping_address", "contact",
        )}),
        ("Sales", {"fields": (
            "engagement", "sales_rep", "commission_rate", "channel",
        )}),
        ("Totals", {"fields": (
            "currency", "subtotal_ex_tax", "tax_total",
            "shipping_cost_ex_tax", "grand_total",
            "discount_amount", "discount_percent", "coupon_discount",
        )}),
        ("Profitability", {"classes": ("collapse",), "fields": (
            "total_cost", "total_margin", "margin_percent",
            "target_margin_percent", "pricing_strategy_notes",
        )}),
        ("Validity & Delivery", {"classes": ("collapse",), "fields": (
            "validity_period_days", "quote_expiration_date",
            "sent_at", "sent_via", "sent_to_email", "viewed_at",
        )}),
        ("Acceptance", {"classes": ("collapse",), "fields": (
            "accepted_at", "accepted_by_name", "accepted_by_title",
            "acceptance_method", "customer_po_number", "acceptance_document_url",
        )}),
        ("Conversion", {"classes": ("collapse",), "fields": (
            "converted_to_order", "converted_at", "converted_by",
        )}),
        ("Terms & Notes", {"classes": ("collapse",), "fields": (
            "payment_terms_proposed", "shipping_terms_proposed",
            "special_conditions", "customer_notes", "internal_notes",
        )}),
        ("Internal", {"fields": ("company", "metadata")}),
        ("Legacy pipeline (read-only, moved to the engagement)", {
            "classes": ("collapse",), "fields": LEGACY_FIELDS,
        }),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


class OrderLineItemInline(admin.TabularInline):
    model = OrderLineItem
    extra = 0
    fields = [
        "sort_order", "variant", "item_name", "sku", "quantity",
        "unit_price_ex_tax", "line_total_ex_tax", "fulfillment_status", "sub_order",
    ]
    readonly_fields = ["id"]


class SubOrderInline(admin.TabularInline):
    model = SubOrder
    extra = 0
    fields = [
        "sub_order_number", "status", "warehouse", "supplier",
        "carrier", "tracking_number", "estimated_ship_date",
    ]
    readonly_fields = ["id", "sub_order_number"]
    show_change_link = True


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = [
        "order_number", "customer", "status", "payment_status",
        "fulfillment_status", "grand_total", "order_date",
    ]
    list_filter = [
        "status", "payment_status", "fulfillment_status",
        "order_type", "company",
    ]
    search_fields = [
        "order_number", "customer__email", "customer__last_name",
        "external_id", "customer_po_number",
    ]
    readonly_fields = ["id", "created_at", "updated_at"]
    raw_id_fields = ["engagement"]
    inlines = [OrderLineItemInline, SubOrderInline]
    fieldsets = (
        ("Identification", {"fields": (
            "id", "order_number", "slug", "order_type", "quote",
            "channel", "external_source", "external_id",
            "customer_po_number", "customer_po_date",
        )}),
        ("Customer", {"fields": (
            "customer", "billing_address", "shipping_address",
            "contact", "customer_message",
        )}),
        ("Status", {"fields": (
            "status", "payment_status", "fulfillment_status",
            "status_note", "booked_at",
        )}),
        ("Totals", {"fields": (
            "currency", "exchange_rate", "price_list",
            "subtotal_ex_tax", "subtotal_inc_tax", "tax_total", "grand_total",
            "discount_amount", "discount_percent", "coupon_discount",
            "store_credit_amount", "refunded_amount",
        )}),
        ("Shipping", {"fields": (
            "shipping_cost_ex_tax", "shipping_cost_inc_tax",
            "shipping_cost_tax", "handling_fee",
        )}),
        ("Payment", {"fields": ("payment_terms", "amount_paid", "balance_due")}),
        ("Tax", {"fields": (
            "tax_rate", "tax_provider_id", "tax_exempt", "tax_exempt_certificate",
        )}),
        ("Sales", {"fields": (
            "sales_rep", "commission_rate", "campaign", "lead_source", "engagement",
        )}),
        ("Dates", {"fields": (
            "order_date", "requested_ship_date", "shipped_date",
        )}),
        ("Internal", {"fields": (
            "company", "amended_from", "terms_text", "internal_notes",
            "metadata", "ip_address",
        )}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(SubOrder)
class SubOrderAdmin(admin.ModelAdmin):
    list_display = [
        "sub_order_number", "order", "status", "warehouse",
        "carrier", "tracking_number",
    ]
    list_filter = ["status", "is_drop_ship"]
    search_fields = ["sub_order_number", "order__order_number", "tracking_number"]
    readonly_fields = ["id", "created_at", "updated_at"]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Auto-send shipping notification when status is shipped + has tracking
        if (
            change
            and obj.status in ("shipped", "in_transit")
            and obj.tracking_number
            and not obj.shipping_notification_sent_at
        ):
            from communications.services import send_shipping_notification

            send_shipping_notification(
                sub_order=obj, triggered_by="auto", user=request.user,
            )
