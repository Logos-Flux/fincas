from django.contrib import admin

from documents.models import ProductDocument

from .models import (
    Brand,
    Category,
    Product,
    ProductCategory,
    ProductImage,
    ProductOption,
    ProductOptionValue,
    ProductVariant,
    SupplierProduct,
    VariantImage,
    VariantOptionValue,
)


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ["title", "slug", "visible_on_website", "is_authorized_dealer", "sort_order"]
    list_filter = ["visible_on_website", "is_authorized_dealer", "is_featured_store"]
    search_fields = ["title", "erpnext_name", "bc_id"]
    prepopulated_fields = {"slug": ("title",)}
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ["title", "slug", "parent", "tree_depth", "visible_on_website", "sort_order"]
    list_filter = ["visible_on_website", "is_featured_store", "tree_depth"]
    search_fields = ["title", "erpnext_name", "bc_id"]
    prepopulated_fields = {"slug": ("title",)}
    readonly_fields = ["id", "tree_depth", "created_at", "updated_at"]


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 0
    fields = ["sku", "name", "retail_price", "sale_price", "stock", "stock_status", "is_active", "sort_order"]
    readonly_fields = ["id"]
    show_change_link = True


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 0
    fields = ["cf_image_id", "is_primary", "alt_text", "sort_order"]


class ProductCategoryInline(admin.TabularInline):
    model = ProductCategory
    extra = 0
    fields = ["category", "is_primary", "sort_order"]


class ProductOptionInline(admin.TabularInline):
    model = ProductOption
    extra = 0
    fields = ["name", "sort_order"]
    show_change_link = True


class ProductDocumentInline(admin.TabularInline):
    model = ProductDocument
    extra = 0
    fields = [
        "title", "document_type", "r2_url",
        "file_size", "match_method", "is_verified",
    ]
    readonly_fields = ["file_size"]


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ["title", "part_number", "brand", "product_type", "is_active", "visible_on_website"]
    list_filter = ["product_type", "is_active", "visible_on_website", "brand", "is_featured_store"]
    search_fields = ["title", "part_number", "slug"]
    prepopulated_fields = {"slug": ("title",)}
    readonly_fields = ["id", "created_at", "updated_at"]
    inlines = [ProductVariantInline, ProductImageInline, ProductCategoryInline, ProductOptionInline, ProductDocumentInline]
    fieldsets = (
        ("Core", {"fields": (
            "id", "title", "slug", "part_number", "product_type",
            "is_active", "visible_on_website", "has_variants", "brand", "company",
        )}),
        ("Description", {"fields": ("description", "short_description")}),
        ("Display", {"fields": ("is_featured_store", "search_boost", "primary_image_id", "thumbnail_image_id")}),
        ("Identifiers", {"fields": ("gtin", "mpn")}),
        ("Specifications", {"fields": ("specifications",)}),
        ("Safety Warnings", {"fields": ("safety_warnings",)}),
        ("Ratings", {"fields": ("rating_value", "rating_count")}),
        ("Item Dimensions", {"fields": (
            "item_weight", "item_weight_uom",
            "item_length", "item_width", "item_height", "item_dimension_uom",
        )}),
        ("Shipping Dimensions", {"fields": (
            "shipping_weight", "shipping_weight_uom",
            "shipping_length", "shipping_width", "shipping_height", "shipping_dimension_uom",
        )}),
        ("Shipping Flags", {"fields": (
            "ships_usps", "ships_ups", "ships_ltl", "ships_pickup", "free_shipping",
            "hazmat_flag", "hazmat_class", "oversized_flag",
        )}),
        ("UOM", {"fields": (
            "selling_uom", "selling_qty_per_uom",
            "purchasing_uom", "purchasing_qty_per_uom",
            "sell_pallet_only", "purchase_pallet_only",
        )}),
        ("MOQ & Lead Time", {"fields": ("minimum_order_qty", "lead_time_days")}),
        ("SEO", {"classes": ("collapse",), "fields": (
            "seo_title", "seo_meta_description", "seo_og_title", "seo_og_description",
            "seo_keywords", "seo_robots", "seo_faqs", "seo_related_searches",
            "seo_use_cases", "seo_last_optimized",
        )}),
        ("GMC", {"classes": ("collapse",), "fields": (
            "gmc_google_category", "gmc_product_type", "gmc_condition", "gmc_availability",
            "gmc_shipping_label", "gmc_custom_label_0", "gmc_custom_label_1",
            "gmc_custom_label_2", "gmc_custom_label_3", "gmc_custom_label_4",
            "gmc_additional_image_links", "gmc_energy_efficiency_class", "gmc_age_group",
            "gmc_multipack", "gmc_is_bundle",
        )}),
        ("Infrastructure", {"classes": ("collapse",), "fields": ("vectorize_id",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


class VariantImageInline(admin.TabularInline):
    model = VariantImage
    extra = 0
    fields = ["cf_image_id", "is_primary", "alt_text", "sort_order"]


class VariantOptionValueInline(admin.TabularInline):
    model = VariantOptionValue
    extra = 0


class SupplierProductInline(admin.TabularInline):
    model = SupplierProduct
    extra = 0
    fields = ["supplier", "supplier_sku", "cost", "is_primary", "is_active"]


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ["sku", "product", "retail_price", "stock", "stock_status", "is_active"]
    list_filter = ["is_active", "stock_status"]
    search_fields = ["sku", "name", "product__title"]
    readonly_fields = ["id", "created_at", "updated_at"]
    inlines = [VariantImageInline, VariantOptionValueInline, SupplierProductInline]


class ProductOptionValueInline(admin.TabularInline):
    model = ProductOptionValue
    extra = 0
    fields = ["value", "sort_order"]


@admin.register(ProductOption)
class ProductOptionAdmin(admin.ModelAdmin):
    list_display = ["name", "product", "sort_order"]
    search_fields = ["name", "product__title"]
    inlines = [ProductOptionValueInline]


@admin.register(SupplierProduct)
class SupplierProductAdmin(admin.ModelAdmin):
    list_display = ["variant", "supplier", "cost", "is_primary", "is_active"]
    list_filter = ["is_active", "is_primary"]
    search_fields = ["variant__sku", "supplier_sku"]
