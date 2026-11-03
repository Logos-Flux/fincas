"""Unit tests for pluggable shipping carriers and registry."""

from decimal import Decimal

from django.test import SimpleTestCase, override_settings

from fulfillment.carriers import (
    EasyPostCarrier,
    ManualCarrier,
    Parcel,
    ShippingAddress,
    get_carrier,
    list_carriers,
)


class TestShippingCarriers(SimpleTestCase):
    def setUp(self):
        self.origin = ShippingAddress(
            company="Golden Hub",
            street1="100 Distribution Way",
            city="Chicago",
            state="IL",
            postal_code="60601",
        )
        self.destination = ShippingAddress(
            name="John Customer",
            street1="200 Main St",
            city="Peoria",
            state="IL",
            postal_code="61602",
        )
        self.parcel = Parcel(
            weight_oz=Decimal("16.0"),
            length_in=Decimal("10.0"),
            width_in=Decimal("8.0"),
            height_in=Decimal("4.0"),
        )

    def test_builtin_carriers_listed(self):
        carriers = list_carriers()
        assert "manual" in carriers
        assert "easypost" in carriers
        assert carriers["manual"] is ManualCarrier
        assert carriers["easypost"] is EasyPostCarrier

    def test_manual_carrier_rates(self):
        carrier = get_carrier("manual")
        rates = carrier.get_rates(self.origin, self.destination, self.parcel)
        assert len(rates) == 2
        pickup = next(r for r in rates if r.service_code == "PICKUP")
        assert pickup.rate == Decimal("0.00")
        assert pickup.carrier_id == "manual"

    def test_manual_carrier_label_and_tracking(self):
        carrier = get_carrier("manual")
        label = carrier.create_label(
            self.origin, self.destination, self.parcel, service_code="PICKUP"
        )
        assert label.success is True
        assert label.tracking_number.startswith("MAN-")
        assert label.postage_cost == Decimal("0.00")

        tracking = carrier.track(label.tracking_number)
        assert tracking.status == "delivered"

    def test_easypost_mock_mode_rates_and_label(self):
        with override_settings(EASYPOST_MOCK_MODE=True):
            carrier = get_carrier("easypost")
            rates = carrier.get_rates(self.origin, self.destination, self.parcel)
            assert len(rates) >= 1

            label = carrier.create_label(
                self.origin, self.destination, self.parcel, service_code="GroundAdvantage"
            )
            assert label.success is True
            assert label.tracking_number.startswith("940011189956")
            assert label.postage_cost == Decimal("7.25")

    def test_unknown_carrier_raises_keyerror(self):
        with self.assertRaises(KeyError):
            get_carrier("non_existent_carrier")
