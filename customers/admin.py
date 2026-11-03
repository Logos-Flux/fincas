from django.contrib import admin

from .models import Customer, CustomerAddress


class CustomerAddressInline(admin.StackedInline):
    model = CustomerAddress
    extra = 0
    fields = [
        "address_type", "is_default", "label",
        "first_name", "last_name", "company_name",
        "address_line_1", "address_line_2", "city", "state_province",
        "postal_code", "country", "phone",
        "is_residential", "has_loading_dock", "has_forklift",
        "delivery_instructions",
    ]


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = [
        "first_name", "last_name", "email", "customer_type",
        "pricing_tier", "is_active", "company",
    ]
    list_filter = ["customer_type", "pricing_tier", "is_active", "is_tax_exempt"]
    search_fields = ["first_name", "last_name", "email", "stripe_customer_id"]
    # user is the portal login link: changed only by the identity flows.
    readonly_fields = ["id", "created_at", "updated_at", "last_order_at", "user"]
    inlines = [CustomerAddressInline]


@admin.register(CustomerAddress)
class CustomerAddressAdmin(admin.ModelAdmin):
    list_display = ["customer", "address_type", "city", "state_province", "is_default"]
    list_filter = ["address_type", "country"]
    search_fields = ["customer__email", "city", "postal_code"]
    readonly_fields = ["id", "created_at", "updated_at"]
