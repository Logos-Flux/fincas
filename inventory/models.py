from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Sum
from django.db.models.expressions import Expression

from core.models import BaseModel


class Warehouse(BaseModel):
    """Physical or virtual warehouse (19 fields)."""

    class WarehouseType(models.TextChoices):
        STOCK = "stock", "Stock"
        TRANSIT = "transit", "Transit"
        SCRAP = "scrap", "Scrap"
        RETURNS = "returns", "Returns"
        VIRTUAL = "virtual", "Virtual"

    # Owning company (Step 2, F-027). Nullable at the DB level so the
    # migration backfills honestly; ``clean()`` requires it, and unscoped
    # reads fail closed, so a company-less warehouse is invisible rather
    # than shared. Stock inherits its company through this FK — there is
    # deliberately no second company FK on WarehouseStock to drift.
    company = models.ForeignKey(
        "core.Company", on_delete=models.CASCADE, null=True, blank=True,
        related_name="warehouses",
    )
    warehouse_code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=255)
    warehouse_type = models.CharField(
        max_length=10, choices=WarehouseType.choices, default=WarehouseType.STOCK,
    )
    is_active = models.BooleanField(default=True)
    # Inline address (warehouses have their own address, not a customer address)
    address_line_1 = models.CharField(max_length=255, blank=True)
    address_line_2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state_province = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=2, default="US", blank=True)
    ship_from_label = models.CharField(max_length=100, blank=True)
    accepts_incoming = models.BooleanField(default=True)
    can_fulfill = models.BooleanField(default=True)
    default_for_company = models.ForeignKey(
        "core.Company", on_delete=models.SET_NULL, null=True, blank=True,
    )
    processing_time_days = models.IntegerField(default=1)
    manager = models.ForeignKey(
        "core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True,
    )
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    erpnext_name = models.CharField(max_length=255, blank=True, db_index=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.warehouse_code} — {self.name}"

    def clean(self):
        super().clean()
        if self.company_id is None:
            raise ValidationError(
                "Warehouse must belong to a company: assign one before "
                "stock can live here. Company-less warehouses are invisible "
                "to scoped reads, never shared."
            )


class WarehouseStock(BaseModel):
    """Stock level per variant per warehouse (23 fields)."""

    variant = models.ForeignKey(
        "products.ProductVariant", on_delete=models.CASCADE, related_name="warehouse_stocks",
    )
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, related_name="stocks")

    # Quantities
    quantity = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    reserved_qty = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    incoming_qty = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    committed_qty = models.DecimalField(max_digits=10, decimal_places=3, default=0)

    # Reorder
    reorder_level = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    reorder_qty = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    safety_stock = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    max_stock = models.DecimalField(max_digits=10, decimal_places=3, default=0)

    # Location
    bin_location = models.CharField(max_length=50, blank=True)
    zone = models.CharField(max_length=50, blank=True)

    # Valuation
    valuation_rate = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    total_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Tracking
    last_stock_update = models.DateTimeField(null=True, blank=True)
    last_counted_at = models.DateTimeField(null=True, blank=True)
    last_count_variance = models.DecimalField(max_digits=10, decimal_places=3, default=0)

    class Meta:
        unique_together = [("variant", "warehouse")]
        ordering = ["variant", "warehouse"]

    def __str__(self):
        return f"{self.variant.sku} @ {self.warehouse.warehouse_code}"

    def clean(self):
        super().clean()
        warehouse = getattr(self, "warehouse", None)
        if warehouse is not None and warehouse.company_id is None:
            raise ValidationError(
                {
                    "warehouse": (
                        "Warehouse has no owning company — assign the "
                        "warehouse to a company before stocking it."
                    )
                }
            )

    @property
    def available_qty(self):
        return self.quantity - self.reserved_qty - self.committed_qty

    def save(self, *args, **kwargs):
        adding = self._state.adding
        update_fields = kwargs.get("update_fields")
        previous = None
        if not adding and (update_fields is None or "quantity" in update_fields):
            previous = (
                WarehouseStock._base_manager.using(kwargs.get("using") or self._state.db)
                .filter(pk=self.pk)
                .values_list("quantity", flat=True)
                .first()
            )
        super().save(*args, **kwargs)
        if isinstance(self.quantity, Expression):
            # quantity = F("quantity") - n: read back the value the DB computed.
            self.refresh_from_db(fields=["quantity"])
        if adding or previous is not None:
            self._sync_variant_stock(kwargs.get("using") or self._state.db)
        self._emit_lifecycle(adding, previous)

    def delete(self, *args, **kwargs):
        using = kwargs.get("using") or self._state.db
        result = super().delete(*args, **kwargs)
        self._sync_variant_stock(using)
        return result

    def _sync_variant_stock(self, using):
        """Keep ``ProductVariant.stock`` (what the storefront, the product
        editor and the API show) equal to the on-hand total across active
        warehouses (F-092). Receiving, counts and adjustments only write
        these rows; before this the variant's number stayed at whatever the
        seed or an import set. A variant with no warehouse rows keeps its
        hand-entered stock. ``update()``: no variant save side effects.

        The variant row is locked before the sum, so two warehouses of one
        variant changing at once serialise here and the later one sums the
        earlier one's committed row instead of writing a stale total."""
        from products.models import ProductVariant

        variants = ProductVariant._base_manager.using(using).filter(pk=self.variant_id)
        with transaction.atomic(using=using):
            list(variants.select_for_update().values_list("pk", flat=True))
            total = (
                WarehouseStock._base_manager.using(using)
                .filter(variant_id=self.variant_id, warehouse__is_active=True)
                .aggregate(total=Sum("quantity"))["total"]
            )
            variants.update(stock=int(total or 0))

    def _emit_lifecycle(self, adding, previous):
        """inventory_adjusted — single emission point: there is no stock
        service; DRF, ERP-assist, admin and imports all save this row."""
        quantity = Decimal(str(self.quantity or 0))
        if adding:
            delta = quantity
        elif previous is None:
            return
        else:
            delta = quantity - Decimal(str(previous))
        if not delta:
            return
        from core.plugins import signals
        from core.plugins.dispatch import emit

        emit(
            signals.inventory_adjusted, WarehouseStock, self,
            warehouse_stock=self, delta=delta, reason="",
        )
