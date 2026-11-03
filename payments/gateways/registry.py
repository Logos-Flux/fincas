"""Payment Gateway registry combining built-in gateways with plugin-provided gateways."""

from __future__ import annotations

from typing import Dict, Optional, Type

from .base import BasePaymentGateway
from .manual import ManualPaymentGateway
from .stripe_gateway import StripePaymentGateway
from .usdc import USDCGateway

BUILTIN_GATEWAYS: Dict[str, Type[BasePaymentGateway]] = {
    ManualPaymentGateway.gateway_id: ManualPaymentGateway,
    StripePaymentGateway.gateway_id: StripePaymentGateway,
    USDCGateway.gateway_id: USDCGateway,
}


def list_gateways() -> Dict[str, Type[BasePaymentGateway]]:
    """Return all available payment gateway classes (built-in + plugin extensions)."""
    gateways = dict(BUILTIN_GATEWAYS)

    # Incorporate plugins
    try:
        from core.plugins import plugin_registry
        plugin_gateways = plugin_registry.get_payment_gateways()
        gateways.update(plugin_gateways)
    except Exception:
        pass

    return gateways


def get_gateway(gateway_id: str, config: Optional[dict] = None) -> BasePaymentGateway:
    """Retrieve an instantiated payment gateway by identifier."""
    available = list_gateways()
    if gateway_id not in available:
        valid_ids = ", ".join(sorted(available.keys()))
        raise KeyError(
            f"Unknown payment gateway '{gateway_id}'. Available gateways: {valid_ids}"
        )
    cls = available[gateway_id]
    return cls(config=config)
