from django.db import models

from core.managers import CompanyScopedManager
from core.models import BaseModel


class DiscountCode(BaseModel):
    """Promo/discount code for storefront checkout."""

    class DiscountType(models.TextChoices):
        PERCENT = "percent", "Percentage"
        FIXED_AMOUNT = "fixed_amount", "Fixed Amount ($)"

    class AppliesTo(models.TextChoices):
        ALL = "all", "All Products"
        SPECIFIC_SKUS = "specific_skus", "Specific SKUs"

    code = models.CharField(max_length=50, unique=True, db_index=True)
    description = models.TextField(blank=True)
    discount_type = models.CharField(
        max_length=20, choices=DiscountType.choices, default=DiscountType.PERCENT
    )
    value = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text="e.g. 15.00 = 15% off or $15 off",
    )
    is_active = models.BooleanField(default=True)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_to = models.DateTimeField(null=True, blank=True)
    max_uses = models.PositiveIntegerField(default=1, help_text="1 = one-time use")
    times_used = models.PositiveIntegerField(default=0)
    minimum_order = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Optional minimum subtotal to qualify",
    )
    applies_to = models.CharField(
        max_length=20, choices=AppliesTo.choices, default=AppliesTo.ALL
    )
    allowed_skus = models.JSONField(
        default=list, blank=True,
        help_text="List of SKU strings when applies_to='specific_skus'",
    )
    company = models.ForeignKey(
        "core.Company", on_delete=models.CASCADE, null=True, blank=True
    )

    objects = CompanyScopedManager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.code

    @property
    def is_exhausted(self):
        return self.times_used >= self.max_uses


class DiscountCodeUsage(models.Model):
    """Tracks each time a discount code is used."""

    discount_code = models.ForeignKey(
        DiscountCode, on_delete=models.CASCADE, related_name="usages"
    )
    order_number = models.CharField(max_length=50)
    customer_email = models.CharField(max_length=255, blank=True)
    discount_applied = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text="Actual $ amount deducted",
    )
    used_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-used_at"]

    def __str__(self):
        return f"{self.discount_code.code} → {self.order_number}"
