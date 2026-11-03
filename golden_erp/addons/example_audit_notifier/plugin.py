"""Reference Fincas Plugin demonstrating extension capabilities."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Type

from core.plugins.base import BasePlugin, DashboardWidget
from core.plugins.signals import order_placed, payment_completed
from payments.gateways.base import BasePaymentGateway, GatewayResult

logger = logging.getLogger("golden_erp.addons.example_audit_notifier")


class CryptoPaymentGateway(BasePaymentGateway):
    """Custom pluggable cryptocurrency payment gateway contributed by this plugin."""

    gateway_id = "crypto_transfer"
    display_name = "USDC / Web3 Direct Transfer"

    def charge(
        self,
        amount: Decimal,
        currency: str,
        payment_source: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> GatewayResult:
        tx_hash = f"0x{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}"
        return GatewayResult.successful(
            transaction_id=tx_hash,
            amount=amount,
            currency="USDC",
            status="settled",
            raw_response={"tx_hash": tx_hash, "wallet": payment_source},
        )


class ExampleAuditNotifierPlugin(BasePlugin):
    """Reference Fincas Addon.

    Demonstrates:
    - Custom navigation link injection
    - Dashboard widget contribution
    - Pluggable payment gateway registration
    - Lifecycle signal observation
    """

    name = "example_audit_notifier"
    verbose_name = "Audit Notifier & Crypto Gateway Addon"
    version = "1.0.0"
    author = "Fincas Community"
    description = "Logs security events and provides an experimental USDC payment gateway."

    events_received: List[str] = []

    def on_ready(self) -> None:
        """Register listeners to core business signals."""
        order_placed.connect(self._on_order_placed, weak=False)
        payment_completed.connect(self._on_payment_completed, weak=False)

    def _on_order_placed(self, sender, **kwargs) -> None:
        msg = f"Audit Plugin: Order placed notification received from {sender}"
        logger.info(msg)
        self.events_received.append(msg)

    def _on_payment_completed(self, sender, **kwargs) -> None:
        msg = f"Audit Plugin: Payment completed notification received from {sender}"
        logger.info(msg)
        self.events_received.append(msg)

    def get_nav_items(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "link",
                "label": "Audit Hub",
                "section": "audit",
                "url_name": "erp-dashboard",
                "icon": "ti ti-shield-check",
                "color": "text-warning",
                "staff_only": True,
            }
        ]

    def get_dashboard_widgets(self) -> List[DashboardWidget]:
        return [
            DashboardWidget(
                id="audit_quick_view",
                title="Security & Audit Monitor",
                component_or_template="erp/widgets/audit_summary.html",
                width_cols=6,
                order=10,
            )
        ]

    def get_payment_gateways(self) -> Dict[str, Type[BasePaymentGateway]]:
        return {
            CryptoPaymentGateway.gateway_id: CryptoPaymentGateway,
        }
