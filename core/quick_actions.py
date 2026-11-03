"""Dashboard quick actions: the "New …" buttons on /erp/.

A fixed registry, shown in full by default. Each staff user can hide the
ones they never use (``ERPUserSettings.hidden_quick_actions``, edited from
the card's "Customize" menu). Keys are stable ids persisted per user, so
rename labels freely but never reuse a key for a different action.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse


@dataclass(frozen=True)
class QuickAction:
    key: str
    label: str
    url_name: str
    style: str = "secondary"  # Bootstrap outline variant

    @property
    def url(self) -> str:
        return reverse(self.url_name)


QUICK_ACTIONS: tuple[QuickAction, ...] = (
    QuickAction("new-quote", "New Quote", "erp-quote-create", "primary"),
    QuickAction("new-order", "New Order", "erp-order-create", "primary"),
    QuickAction("new-customer", "New Customer", "erp-customer-create"),
    QuickAction("new-product", "New Product", "erp-product-create"),
    QuickAction("new-purchase-order", "New Purchase Order", "erp-purchaseorder-create"),
)

KEYS = frozenset(a.key for a in QUICK_ACTIONS)


def hidden_for(user) -> set[str]:
    """Keys the user hid, restricted to actions that still exist."""
    settings_obj = getattr(user, "erp_settings", None)
    stored = getattr(settings_obj, "hidden_quick_actions", None)
    if not isinstance(stored, list):
        stored = []
    return {k for k in stored if isinstance(k, str) and k in KEYS}


def actions_for(user) -> list[dict]:
    """Every action with its visibility, in registry order (template input)."""
    hidden = hidden_for(user)
    return [
        {
            "key": a.key, "label": a.label, "url": a.url, "style": a.style,
            "visible": a.key not in hidden,
        }
        for a in QUICK_ACTIONS
    ]
