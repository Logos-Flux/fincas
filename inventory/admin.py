from django.contrib import admin

from .models import Warehouse, WarehouseStock


class WarehouseStockInline(admin.TabularInline):
    model = WarehouseStock
    extra = 0
    fields = ["variant", "quantity", "reserved_qty", "bin_location", "zone"]
    readonly_fields = ["id"]


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = [
        "warehouse_code", "name", "warehouse_type", "is_active",
        "accepts_incoming", "can_fulfill",
    ]
    list_filter = ["warehouse_type", "is_active"]
    search_fields = ["warehouse_code", "name"]
    readonly_fields = ["id", "created_at", "updated_at"]
    inlines = [WarehouseStockInline]


@admin.register(WarehouseStock)
class WarehouseStockAdmin(admin.ModelAdmin):
    list_display = [
        "variant", "warehouse", "quantity", "reserved_qty",
        "bin_location", "last_stock_update",
    ]
    list_filter = ["warehouse"]
    search_fields = ["variant__sku", "bin_location"]
    readonly_fields = ["id", "created_at", "updated_at"]
