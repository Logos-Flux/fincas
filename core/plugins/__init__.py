"""Plugin architecture for Fincas."""

from .base import BasePlugin, DashboardWidget, ListColumn, TemplateExtension, TokenScope
from .registry import PluginRegistry, plugin_registry
from .signals import (
    customer_registered,
    inventory_adjusted,
    order_cancelled,
    order_placed,
    order_status_changed,
    party_created,
    party_erased,
    party_merged,
    party_unmerged,
    party_updated,
    payment_completed,
    payment_refunded,
    shipment_delivered,
    shipment_dispatched,
)

__all__ = [
    "BasePlugin",
    "DashboardWidget",
    "ListColumn",
    "TemplateExtension",
    "TokenScope",
    "PluginRegistry",
    "plugin_registry",
    "order_placed",
    "order_status_changed",
    "order_cancelled",
    "payment_completed",
    "payment_refunded",
    "shipment_dispatched",
    "shipment_delivered",
    "inventory_adjusted",
    "customer_registered",
    "party_created",
    "party_updated",
    "party_merged",
    "party_unmerged",
    "party_erased",
]
