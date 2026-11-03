from django.db import models

from core.models import BaseModel


class PriceList(BaseModel):
    """Named price list (Standard Retail, Wholesale, etc.)."""

    class PriceListType(models.TextChoices):
        SELLING = "selling", "Selling"
        BUYING = "buying", "Buying"

    name = models.CharField(max_length=255, unique=True)
    price_list_type = models.CharField(
        max_length=10, choices=PriceListType.choices, default=PriceListType.SELLING,
    )
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    currency = models.CharField(max_length=3, default="USD")
    company = models.ForeignKey(
        "core.Company", on_delete=models.SET_NULL, null=True, blank=True,
    )
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    margin_floor_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
    )
    discount_cap_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
    )
    description = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    erpnext_name = models.CharField(max_length=255, blank=True, db_index=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class PriceListItem(BaseModel):
    """Price per variant in a list, with qty-break tiers."""

    price_list = models.ForeignKey(PriceList, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(
        "products.ProductVariant", on_delete=models.CASCADE, related_name="price_list_items",
    )
    price = models.DecimalField(max_digits=10, decimal_places=4)
    min_qty = models.DecimalField(max_digits=10, decimal_places=3, default=1)
    uom = models.CharField(max_length=20, blank=True)
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        unique_together = [("price_list", "variant", "min_qty")]
        ordering = ["price_list", "variant", "min_qty"]

    def __str__(self):
        return f"{self.price_list} — {self.variant.sku} @ {self.min_qty}+"


class TaxRate(BaseModel):
    """Tax rate by jurisdiction."""

    class TaxType(models.TextChoices):
        SALES = "sales", "Sales Tax"
        USE = "use", "Use Tax"
        VAT = "vat", "VAT"

    name = models.CharField(max_length=255)
    rate = models.DecimalField(max_digits=6, decimal_places=4)
    jurisdiction = models.CharField(max_length=50)
    tax_type = models.CharField(
        max_length=10, choices=TaxType.choices, default=TaxType.SALES,
    )
    is_active = models.BooleanField(default=True)
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    erpnext_name = models.CharField(max_length=255, blank=True, db_index=True)

    class Meta:
        ordering = ["jurisdiction", "name"]

    def __str__(self):
        return f"{self.name} ({self.rate * 100:.2f}%)"
