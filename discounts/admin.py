from django.contrib import admin

from .models import DiscountCode, DiscountCodeUsage


class DiscountCodeUsageInline(admin.TabularInline):
    model = DiscountCodeUsage
    extra = 0
    readonly_fields = ["order_number", "customer_email", "discount_applied", "used_at"]
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(DiscountCode)
class DiscountCodeAdmin(admin.ModelAdmin):
    list_display = [
        "code", "discount_type", "value", "is_active",
        "times_used", "max_uses", "valid_to",
    ]
    list_filter = ["discount_type", "is_active", "applies_to"]
    search_fields = ["code", "description"]
    readonly_fields = ["id", "created_at", "updated_at", "times_used"]
    inlines = [DiscountCodeUsageInline]
    fieldsets = (
        ("Code", {
            "fields": ("id", "code", "description", "is_active"),
        }),
        ("Discount", {
            "fields": ("discount_type", "value", "minimum_order"),
        }),
        ("Validity", {
            "fields": ("valid_from", "valid_to", "max_uses", "times_used"),
        }),
        ("Scope", {
            "fields": ("applies_to", "allowed_skus"),
        }),
        ("Metadata", {
            "fields": ("company", "created_at", "updated_at"),
        }),
    )
