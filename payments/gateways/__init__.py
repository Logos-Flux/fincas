"""Pluggable Payment Gateway system."""

from .base import BasePaymentGateway, GatewayResult
from .manual import ManualPaymentGateway
from .registry import get_gateway, list_gateways
from .stripe_gateway import StripePaymentGateway
from .usdc import USDCGateway

__all__ = [
    "BasePaymentGateway",
    "GatewayResult",
    "ManualPaymentGateway",
    "StripePaymentGateway",
    "USDCGateway",
    "get_gateway",
    "list_gateways",
]
