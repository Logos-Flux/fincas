from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import OrderLineItem, QuoteLineItem, SubOrder

# Fields whose change can affect parent totals. If a `.save(update_fields=...)`
# explicitly touches none of these, recalc is a no-op and we skip it.
_LINE_TOTAL_FIELDS = {
    "quantity",
    "unit_price_ex_tax",
    "discount_percent",
    "discount_amount",
    "line_total_ex_tax",
    "line_tax",
}


@receiver(post_save, sender=SubOrder)
def suborder_status_changed(sender, instance, **kwargs):
    """Roll up SubOrder status changes to parent Order."""
    update_fields = kwargs.get("update_fields")
    if update_fields and "status" not in update_fields:
        return
    instance.order.update_status_from_suborders()


@receiver(post_save, sender=OrderLineItem)
def order_line_saved(sender, instance, **kwargs):
    update_fields = kwargs.get("update_fields")
    if update_fields and not (set(update_fields) & _LINE_TOTAL_FIELDS):
        return
    order = instance.order
    order.recalculate_totals()
    for invoice in order.invoices.all():
        invoice.recalculate_totals()


@receiver(post_delete, sender=OrderLineItem)
def order_line_deleted(sender, instance, **kwargs):
    order = instance.order
    order.recalculate_totals()
    for invoice in order.invoices.all():
        invoice.recalculate_totals()


@receiver(post_save, sender=QuoteLineItem)
def quote_line_saved(sender, instance, **kwargs):
    update_fields = kwargs.get("update_fields")
    if update_fields and not (set(update_fields) & _LINE_TOTAL_FIELDS):
        return
    instance.quote.recalculate_totals()


@receiver(post_delete, sender=QuoteLineItem)
def quote_line_deleted(sender, instance, **kwargs):
    instance.quote.recalculate_totals()
