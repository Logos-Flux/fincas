"""Unit tests for pluggable payment gateways and registry."""

from decimal import Decimal

from django.test import SimpleTestCase, override_settings

from payments.gateways import (
    ManualPaymentGateway,
    StripePaymentGateway,
    get_gateway,
    list_gateways,
)


class TestPaymentGateways(SimpleTestCase):
    def test_builtin_gateways_listed(self):
        gateways = list_gateways()
        assert "manual" in gateways
        assert "stripe" in gateways
        assert gateways["manual"] is ManualPaymentGateway
        assert gateways["stripe"] is StripePaymentGateway

    def test_manual_gateway_charge(self):
        gw = get_gateway("manual")
        result = gw.charge(
            amount=Decimal("150.00"),
            currency="USD",
            payment_source="CHECK-1024",
        )
        assert result.success is True
        assert result.amount == Decimal("150.00")
        assert result.currency == "USD"
        assert result.status == "settled"
        assert result.transaction_id.startswith("MANUAL-")

    def test_manual_gateway_refund(self):
        gw = get_gateway("manual")
        result = gw.refund(
            transaction_id="MANUAL-12345",
            amount=Decimal("50.00"),
            reason="Customer request",
        )
        assert result.success is True
        assert result.status == "refunded"
        assert result.transaction_id.startswith("REF-")

    def test_stripe_mock_mode_charge_and_refund(self):
        with override_settings(STRIPE_MOCK_MODE=True):
            gw = get_gateway("stripe")
            result = gw.charge(
                amount=Decimal("49.99"),
                currency="USD",
                payment_source="tok_visa",
            )
            assert result.success is True
            assert result.status == "captured"
            assert result.transaction_id.startswith("ch_mock_")

            refund_result = gw.refund(transaction_id=result.transaction_id)
            assert refund_result.success is True
            assert refund_result.status == "refunded"
            assert refund_result.transaction_id.startswith("re_mock_")

    def test_stripe_unconfigured_error(self):
        with override_settings(DEBUG=False, STRIPE_MOCK_MODE=False, STRIPE_SECRET_KEY=""):
            gw = get_gateway("stripe")
            result = gw.charge(
                amount=Decimal("10.00"),
                currency="USD",
                payment_source="tok_visa",
            )
            assert result.success is False
            assert result.error_code == "NOT_CONFIGURED"

    def test_plugin_contributed_gateway(self):
        with override_settings(
            GOLDEN_PLUGINS=["golden_erp.addons.example_audit_notifier.plugin.ExampleAuditNotifierPlugin"]
        ):
            from core.plugins import plugin_registry
            plugin_registry.reload()

            gateways = list_gateways()
            assert "crypto_transfer" in gateways

            crypto_gw = get_gateway("crypto_transfer")
            result = crypto_gw.charge(
                amount=Decimal("250.00"),
                currency="USDC",
                payment_source="0x71C...B29",
            )
            assert result.success is True
            assert result.currency == "USDC"
            assert result.transaction_id.startswith("0x")

    def test_unknown_gateway_raises_keyerror(self):
        with self.assertRaises(KeyError):
            get_gateway("non_existent_gateway")
