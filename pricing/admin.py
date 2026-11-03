from django.contrib import admin

from .models import (
    PriceList,
    PriceListItem,
    TaxRate,
)


class PriceListItemInline(admin.TabularInline):
    model = PriceListItem
    extra = 0
    fields = ["variant", "price", "min_qty", "uom", "is_active"]


@admin.register(PriceList)
class PriceListAdmin(admin.ModelAdmin):
    list_display = ["name", "price_list_type", "currency", "is_default", "is_active"]
    list_filter = ["price_list_type", "is_active", "currency"]
    search_fields = ["name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    inlines = [PriceListItemInline]


@admin.register(PriceListItem)
class PriceListItemAdmin(admin.ModelAdmin):
    list_display = ["price_list", "variant", "price", "min_qty", "is_active"]
    list_filter = ["price_list", "is_active"]
    search_fields = ["variant__sku"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(TaxRate)
class TaxRateAdmin(admin.ModelAdmin):
    list_display = ["name", "rate", "jurisdiction", "tax_type", "is_active"]
    list_filter = ["tax_type", "is_active", "jurisdiction"]
    search_fields = ["name", "jurisdiction"]
    readonly_fields = ["id", "created_at", "updated_at"]
