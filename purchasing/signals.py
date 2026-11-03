from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import POLineItem

_PO_LINE_TOTAL_FIELDS = {
    "quantity_ordered",
    "unit_cost",
    "discount_percent",
    "discount_amount",
    "line_total",
    "tax_amount",
    # Landed inputs/computed values (F-016): a duty edit must retrigger
    # allocation; direct writes to the computed fields are overwritten by
    # recalculate, which itself uses .update() (no signals, no recursion).
    "duty_amount",
    "freight_allocation",
    "other_landed_costs",
    "landed_cost_per_unit",
}


@receiver(post_save, sender=POLineItem)
def po_line_saved(sender, instance, **kwargs):
    update_fields = kwargs.get("update_fields")
    if update_fields and not (set(update_fields) & _PO_LINE_TOTAL_FIELDS):
        return
    instance.purchase_order.recalculate_totals()


@receiver(post_delete, sender=POLineItem)
def po_line_deleted(sender, instance, **kwargs):
    instance.purchase_order.recalculate_totals()
