from django.contrib import admin

from .models import Payment


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = [
        "payment_number", "order", "payment_method", "payment_type",
        "amount", "currency", "status", "payment_date",
    ]
    list_filter = [
        "status", "payment_method", "payment_provider",
        "payment_type", "risk_level",
    ]
    search_fields = [
        "payment_number", "transaction_id", "order__order_number",
        "card_last_four",
    ]
    readonly_fields = ["id", "created_at", "updated_at"]
    fieldsets = (
        ("Identification", {"fields": (
            "id", "payment_number", "order",
        )}),
        ("Method", {"fields": (
            "payment_method", "payment_provider", "payment_type",
        )}),
        ("Transaction", {"fields": (
            "transaction_id", "authorization_code", "gateway_response",
        )}),
        ("Amount", {"fields": (
            "amount", "currency", "exchange_rate",
            "processing_fee", "net_amount",
        )}),
        ("Status", {"fields": (
            "status", "status_note", "payment_date", "settled_date",
        )}),
        ("Card Details", {"classes": ("collapse",), "fields": (
            "card_brand", "card_last_four", "card_exp_month", "card_exp_year",
        )}),
        ("Bank Details", {"classes": ("collapse",), "fields": (
            "bank_name", "bank_reference",
        )}),
        ("Fraud/Risk", {"classes": ("collapse",), "fields": (
            "risk_score", "risk_level", "risk_decision", "risk_reason",
            "risk_message", "risk_rules_triggered", "risk_response_raw",
            "fraud_report", "fraud_reviewed_by", "fraud_reviewed_at",
        )}),
        ("Verification", {"classes": ("collapse",), "fields": (
            "avs_result", "cvv_result", "three_d_secure_result",
            "three_d_secure_version", "network_status", "network_decline_code",
        )}),
        ("Device & Marketplace", {"classes": ("collapse",), "fields": (
            "ip_address", "device_fingerprint",
            "marketplace_order_id", "marketplace_fee",
        )}),
        ("Refund", {"classes": ("collapse",), "fields": (
            "refund_reason", "parent_payment",
        )}),
        ("Internal", {"fields": (
            "company", "received_by", "notes", "metadata",
        )}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )
