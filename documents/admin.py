from django.contrib import admin

from .models import ProductDocument


@admin.register(ProductDocument)
class ProductDocumentAdmin(admin.ModelAdmin):
    list_display = [
        "title", "product", "document_type", "match_method",
        "match_confidence", "is_verified",
    ]
    list_filter = ["document_type", "match_method", "is_verified"]
    search_fields = ["title", "product__title", "r2_key"]
    readonly_fields = ["id", "created_at", "updated_at"]
