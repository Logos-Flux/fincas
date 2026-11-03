"""Carrier registry combining built-in carriers with plugin-provided carriers."""

from __future__ import annotations

from typing import Dict, Optional, Type

from .base import BaseShippingCarrier
from .easypost_carrier import EasyPostCarrier
from .manual import ManualCarrier

BUILTIN_CARRIERS: Dict[str, Type[BaseShippingCarrier]] = {
    ManualCarrier.carrier_id: ManualCarrier,
    EasyPostCarrier.carrier_id: EasyPostCarrier,
}


def list_carriers() -> Dict[str, Type[BaseShippingCarrier]]:
    """Return all available shipping carrier classes (built-in + plugin extensions)."""
    carriers = dict(BUILTIN_CARRIERS)

    # Incorporate plugins
    try:
        from core.plugins import plugin_registry
        plugin_carriers = plugin_registry.get_shipping_carriers()
        carriers.update(plugin_carriers)
    except Exception:
        pass

    return carriers


def get_carrier(carrier_id: str, config: Optional[dict] = None) -> BaseShippingCarrier:
    """Retrieve an instantiated shipping carrier by identifier."""
    available = list_carriers()
    if carrier_id not in available:
        valid_ids = ", ".join(sorted(available.keys()))
        raise KeyError(
            f"Unknown shipping carrier '{carrier_id}'. Available carriers: {valid_ids}"
        )
    cls = available[carrier_id]
    return cls(config=config)
