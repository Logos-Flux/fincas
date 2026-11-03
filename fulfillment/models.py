from django.db import models
from django.db.models import Max

from core.models import BaseModel


class PickTicket(BaseModel):
    """Pick ticket for warehouse fulfillment."""

    class Status(models.TextChoices):
        CREATED = "created", "Created"
        ASSIGNED = "assigned", "Assigned"
        PICKING = "picking", "Picking"
        PICKED = "picked", "Picked"
        PACKING = "packing", "Packing"
        PACKED = "packed", "Packed"
        SHIPPED = "shipped", "Shipped"

    ticket_number = models.CharField(max_length=20, unique=True)
    order = models.ForeignKey(
        "orders.Order", on_delete=models.SET_NULL, null=True, blank=True, related_name="pick_tickets"
    )
    shipment_group = models.ForeignKey(
        "orders.ShipmentGroup", on_delete=models.SET_NULL, null=True, blank=True
    )
    sub_order = models.ForeignKey(
        "orders.SubOrder", on_delete=models.SET_NULL, null=True, blank=True, related_name="pick_tickets"
    )
    warehouse = models.ForeignKey("inventory.Warehouse", on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.CREATED)
    assigned_to = models.ForeignKey("core.UserProfile", on_delete=models.SET_NULL, null=True, blank=True)
    ship_to_address = models.TextField(blank=True)
    ship_to_label = models.CharField(max_length=255, blank=True)
    ship_to_attention = models.CharField(max_length=255, blank=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    picked_at = models.DateTimeField(null=True, blank=True)
    packed_at = models.DateTimeField(null=True, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.ticket_number

    def save(self, *args, **kwargs):
        if not self.ticket_number:
            last_num = PickTicket.objects.aggregate(m=Max("ticket_number"))["m"]
            num = 1
            if last_num:
                try:
                    num = int(last_num.replace("PT-", "")) + 1
                except ValueError:
                    pass
            self.ticket_number = f"PT-{num:05d}"
        super().save(*args, **kwargs)

    #: Guarded status transitions. SHIPPED is terminal: the ticket is the
    #: warehouse's proof of dispatch and must not be re-opened by mutating
    #: status. Direct ORM writes bypass this map; the DRF ``transition``
    #: action enforces it (illegal moves → 400 with a field-error envelope).
    VALID_TRANSITIONS = {
        "created": ["assigned"],
        "assigned": ["picking"],
        "picking": ["picked"],
        "picked": ["packing"],
        "packing": ["packed"],
        "packed": ["shipped"],
        "shipped": [],
    }

    def transition_to(self, new_status):
        """Validate and apply a status transition."""
        allowed = self.VALID_TRANSITIONS.get(self.status, [])
        if new_status not in allowed:
            from django.core.exceptions import ValidationError
            raise ValidationError(
                f"Cannot transition from '{self.status}' to '{new_status}'. "
                f"Allowed: {allowed}"
            )
        self.status = new_status
        self.save(update_fields=["status", "updated_at"])


class PickTicketLine(BaseModel):
    """Line item on a pick ticket."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PICKED = "picked", "Picked"
        SHORT = "short", "Short"

    pick_ticket = models.ForeignKey(PickTicket, on_delete=models.CASCADE, related_name="lines")
    order_line_item = models.ForeignKey(
        "orders.OrderLineItem", on_delete=models.SET_NULL, null=True, blank=True
    )
    variant = models.ForeignKey(
        "products.ProductVariant", on_delete=models.PROTECT, null=True, blank=True
    )
    item_name = models.TextField(blank=True)
    sku = models.CharField(max_length=100, blank=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    bin_location = models.CharField(max_length=50, blank=True)
    picked_qty = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    sort_order = models.IntegerField(default=0)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["pick_ticket", "sort_order", "bin_location"]

    def __str__(self):
        return f"{self.pick_ticket} — {self.sku or self.item_name}"
