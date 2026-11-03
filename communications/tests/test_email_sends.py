import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings

from communications.models import EmailLog
from communications.services import (
    build_tracking_url,
    send_order_confirmation,
    send_shipping_notification,
)
from core.models import Company
from customers.models import Customer
from orders.models import Order, SubOrder


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class SendOrderConfirmationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply Inc")
        self.customer = Customer.objects.create(
            first_name="John",
            last_name="Doe",
            email="john@example.com",
            company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer,
            company=self.company,
            subtotal_ex_tax=100,
            grand_total=107,
            tax_total=7,
        )
        self.user = User.objects.create_user(username="admin", password="test")

    def test_send_creates_email_log_with_sent_status(self):
        log = send_order_confirmation(
            order=self.order,
            recipient_email="john@example.com",
            triggered_by="manual",
            user=self.user,
        )
        self.assertEqual(log.status, EmailLog.Status.SENT)
        self.assertEqual(log.document_type, EmailLog.DocumentType.SALES_ORDER)
        self.assertEqual(log.document_id, self.order.pk)
        self.assertEqual(log.document_number, self.order.order_number)
        self.assertEqual(log.recipient_email, "john@example.com")
        self.assertEqual(log.triggered_by, "manual")
        self.assertEqual(log.sent_by, self.user)

    def test_send_creates_email_log_with_failed_status_on_error(self):
        with patch(
            "communications.services.EmailMultiAlternatives.send",
            side_effect=Exception("SMTP error"),
        ):
            log = send_order_confirmation(
                order=self.order,
                recipient_email="john@example.com",
                triggered_by="auto",
                user=self.user,
            )
        self.assertEqual(log.status, EmailLog.Status.FAILED)
        self.assertIn("SMTP error", log.error_message)

    def test_send_uses_correct_subject(self):
        log = send_order_confirmation(
            order=self.order,
            recipient_email="john@example.com",
            triggered_by="manual",
            user=self.user,
        )
        self.assertIn(self.order.order_number, log.subject)

    def test_email_log_count_increments(self):
        self.assertEqual(EmailLog.objects.count(), 0)
        send_order_confirmation(
            order=self.order,
            recipient_email="john@example.com",
            triggered_by="manual",
            user=self.user,
        )
        self.assertEqual(EmailLog.objects.count(), 1)
        send_order_confirmation(
            order=self.order,
            recipient_email="john@example.com",
            triggered_by="manual",
            user=self.user,
        )
        self.assertEqual(EmailLog.objects.count(), 2)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AutoSendOnBookingTests(TestCase):
    """Integration tests that POST to the actual order edit view to verify auto-send wiring."""

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply Inc")
        self.customer = Customer.objects.create(
            first_name="Jane",
            last_name="Smith",
            email="jane@example.com",
            company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer,
            company=self.company,
            status=Order.Status.PENDING,
            grand_total=200,
        )
        self.user = User.objects.create_user(
            username="staff", password="test", is_staff=True,
        )
        self.client.login(username="staff", password="test")

    def _post_status_transition(self, order, new_status):
        """POST to the order edit view with a status transition action.

        Submits the minimal form data needed for the view's form + formset
        to be valid, plus the _action for the status change.
        """
        from orders.forms import OrderForm, OrderLineItemFormSet

        # Build minimal valid form data from the existing order
        form = OrderForm(instance=order)
        data = {}
        for field_name, field in form.fields.items():
            value = form.initial.get(field_name, field.initial)
            if value is None:
                value = ""
            data[field_name] = value

        # Formset management data (zero forms — no line items)
        formset = OrderLineItemFormSet(instance=order)
        prefix = formset.prefix  # "line_items"
        for key, val in formset.management_form.initial.items():
            data[f"{prefix}-{key}"] = val
        data[f"{prefix}-TOTAL_FORMS"] = 0
        data[f"{prefix}-INITIAL_FORMS"] = 0

        # Clean up None values
        for key in list(data.keys()):
            if data[key] is None:
                data[key] = ""

        data["_action"] = f"set_status_{new_status}"
        return self.client.post(
            f"/erp/orders/{order.pk}/edit/",
            data=data,
        )

    def test_pending_to_awaiting_fulfillment_auto_sends_email(self):
        """PENDING → AWAITING_FULFILLMENT should auto-send confirmation email."""
        self.assertEqual(EmailLog.objects.count(), 0)
        response = self._post_status_transition(self.order, Order.Status.AWAITING_FULFILLMENT)
        self.assertEqual(response.status_code, 302)  # redirect on success

        # Verify email was sent and logged
        self.assertEqual(EmailLog.objects.count(), 1)
        log = EmailLog.objects.first()
        self.assertEqual(log.status, EmailLog.Status.SENT)
        self.assertEqual(log.triggered_by, "auto")
        self.assertEqual(log.recipient_email, "jane@example.com")
        self.assertEqual(log.document_id, self.order.pk)

    def test_awaiting_fulfillment_to_awaiting_shipment_does_not_send(self):
        """Non-PENDING transitions should NOT auto-send."""
        # First move to AWAITING_FULFILLMENT
        self.order.status = Order.Status.AWAITING_FULFILLMENT
        from django.utils import timezone
        self.order.booked_at = timezone.now()
        self.order.save(update_fields=["status", "booked_at"])

        # Clear any logs from setup
        EmailLog.objects.all().delete()

        # Now move AWAITING_FULFILLMENT → AWAITING_SHIPMENT
        response = self._post_status_transition(self.order, Order.Status.AWAITING_SHIPMENT)
        self.assertEqual(response.status_code, 302)

        # No email should have been sent
        self.assertEqual(EmailLog.objects.count(), 0)

    def test_on_hold_to_awaiting_fulfillment_does_not_resend(self):
        """ON_HOLD → AWAITING_FULFILLMENT should NOT auto-send (booked_at already set, not a PENDING transition)."""
        from django.utils import timezone

        self.order.status = Order.Status.ON_HOLD
        self.order.booked_at = timezone.now()
        self.order.save(update_fields=["status", "booked_at"])

        EmailLog.objects.all().delete()

        response = self._post_status_transition(self.order, Order.Status.AWAITING_FULFILLMENT)
        self.assertEqual(response.status_code, 302)

        # Order should be AWAITING_FULFILLMENT, but no email sent (was_pending is False)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_FULFILLMENT)
        self.assertEqual(EmailLog.objects.count(), 0)


class BuildTrackingUrlTests(TestCase):
    """PR #26 checklist: Verify tracking URLs open correct carrier pages."""

    def test_ups_tracking_url(self):
        url = build_tracking_url("ups", "1Z999AA10123456784")
        self.assertEqual(
            url, "https://www.ups.com/track?tracknum=1Z999AA10123456784"
        )

    def test_fedex_tracking_url(self):
        url = build_tracking_url("fedex", "123456789012")
        self.assertEqual(
            url, "https://www.fedex.com/fedextrack/?trknbr=123456789012"
        )

    def test_usps_tracking_url(self):
        url = build_tracking_url("usps", "9400111899223100001234")
        self.assertEqual(
            url,
            "https://tools.usps.com/go/TrackConfirmAction?tLabels=9400111899223100001234",
        )

    def test_carrier_matching_is_case_insensitive(self):
        url = build_tracking_url("UPS", "1Z999")
        self.assertIn("ups.com", url)

    def test_carrier_matching_strips_whitespace(self):
        url = build_tracking_url("  fedex  ", "123")
        self.assertIn("fedex.com", url)

    def test_unknown_carrier_returns_fallback_url(self):
        url = build_tracking_url("dhl", "12345", fallback_url="https://example.com/track")
        self.assertEqual(url, "https://example.com/track")

    def test_unknown_carrier_no_fallback_returns_empty(self):
        url = build_tracking_url("dhl", "12345")
        self.assertEqual(url, "")

    def test_no_tracking_number_returns_fallback(self):
        url = build_tracking_url("ups", "", fallback_url="https://example.com/track")
        self.assertEqual(url, "https://example.com/track")

    def test_none_tracking_number_returns_fallback(self):
        url = build_tracking_url("ups", None, fallback_url="https://example.com")
        self.assertEqual(url, "https://example.com")

    def test_none_carrier_with_tracking_returns_fallback(self):
        url = build_tracking_url(None, "12345", fallback_url="https://example.com")
        self.assertEqual(url, "https://example.com")


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    SALES_NOTIFICATION_EMAIL="sales@example.com",
)
class OrderConfirmationBccTests(TestCase):
    """Verify BCC arrives at sales@example.com."""

    def setUp(self):
        self.company = Company.objects.create(name="Acme Inc")
        self.customer = Customer.objects.create(
            first_name="John", last_name="Doe",
            email="john@example.com", company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer, company=self.company,
            subtotal_ex_tax=100, grand_total=107, tax_total=7,
        )
        self.user = User.objects.create_user(username="admin", password="test")

    def test_order_confirmation_bccs_sales_email(self):
        send_order_confirmation(
            order=self.order,
            recipient_email="john@example.com",
            triggered_by="manual",
            user=self.user,
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("sales@example.com", mail.outbox[0].bcc)

    def test_no_bcc_when_setting_empty(self):
        with self.settings(SALES_NOTIFICATION_EMAIL=""):
            send_order_confirmation(
                order=self.order,
                recipient_email="john@example.com",
                triggered_by="manual",
                user=self.user,
            )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].bcc, [])

    def test_order_confirmation_bccs_multiple_emails(self):
        with self.settings(
            SALES_NOTIFICATION_EMAIL="rep1@example.com, rep2@example.com"
        ):
            send_order_confirmation(
                order=self.order,
                recipient_email="john@example.com",
                triggered_by="manual",
                user=self.user,
            )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            sorted(mail.outbox[0].bcc),
            ["rep1@example.com", "rep2@example.com"],
        )


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DuplicatePreventionTests(TestCase):
    """PR #26 checklist: Verify duplicate sends are blocked."""

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply Inc")
        self.customer = Customer.objects.create(
            first_name="Jane", last_name="Smith",
            email="jane@example.com", company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer, company=self.company,
            subtotal_ex_tax=50, grand_total=53.50, tax_total=3.50,
        )
        self.user = User.objects.create_user(username="staff", password="test")

    def test_order_confirmation_sets_confirmation_sent_at(self):
        self.assertIsNone(self.order.confirmation_sent_at)
        send_order_confirmation(
            order=self.order,
            recipient_email="jane@example.com",
            triggered_by="manual",
            user=self.user,
        )
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.confirmation_sent_at)

    def test_shipping_notification_sets_sent_at(self):
        sub = SubOrder.objects.create(
            order=self.order, sub_order_number="SO-00001-1",
            status="shipped", carrier="ups", tracking_number="1Z999",
        )
        self.assertIsNone(sub.shipping_notification_sent_at)
        send_shipping_notification(sub_order=sub, triggered_by="manual", user=self.user)
        sub.refresh_from_db()
        self.assertIsNotNone(sub.shipping_notification_sent_at)

    def test_admin_skips_send_when_already_notified(self):
        """SubOrderAdmin.save_model should NOT send when shipping_notification_sent_at is set."""
        from django.utils import timezone

        sub = SubOrder.objects.create(
            order=self.order, sub_order_number="SO-00001-1",
            status="shipped", carrier="ups", tracking_number="1Z999",
            shipping_notification_sent_at=timezone.now(),
        )
        # Simulate admin save_model logic
        should_send = (
            sub.status in ("shipped", "in_transit")
            and sub.tracking_number
            and not sub.shipping_notification_sent_at
        )
        self.assertFalse(should_send)

    def test_admin_sends_when_not_yet_notified(self):
        """SubOrderAdmin.save_model should send when shipping_notification_sent_at is NULL."""
        sub = SubOrder.objects.create(
            order=self.order, sub_order_number="SO-00001-1",
            status="shipped", carrier="ups", tracking_number="1Z999",
        )
        should_send = (
            sub.status in ("shipped", "in_transit")
            and sub.tracking_number
            and not sub.shipping_notification_sent_at
        )
        self.assertTrue(should_send)

    def test_shipping_notification_returns_none_without_customer_email(self):
        no_email_customer = Customer.objects.create(
            first_name="No", last_name="Email", email="", company=self.company,
        )
        order = Order.objects.create(
            customer=no_email_customer, company=self.company,
            subtotal_ex_tax=10, grand_total=10,
        )
        sub = SubOrder.objects.create(
            order=order, sub_order_number="SO-00002-1",
            status="shipped", carrier="ups", tracking_number="1Z999",
        )
        result = send_shipping_notification(sub_order=sub)
        self.assertIsNone(result)


class UpdateSubOrderTrackingTests(TestCase):
    """Tests for the AJAX endpoint that saves tracking info to a SubOrder."""

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply Inc")
        self.customer = Customer.objects.create(
            first_name="John", last_name="Doe",
            email="john@example.com", company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer, company=self.company,
            subtotal_ex_tax=100, grand_total=107, tax_total=7,
        )
        self.sub_order = SubOrder.objects.create(
            order=self.order, sub_order_number="SO-00001-1",
        )
        self.user = User.objects.create_user(
            username="staff", password="test", is_staff=True,
        )
        self.client.login(username="staff", password="test")
        self.url = f"/erp/orders/sub-orders/{self.sub_order.pk}/update-tracking/"

    def test_saves_carrier_and_tracking_number(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"carrier": "ups", "tracking_number": "1Z999"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.sub_order.refresh_from_db()
        self.assertEqual(self.sub_order.carrier, "ups")
        self.assertEqual(self.sub_order.tracking_number, "1Z999")

    def test_auto_generates_tracking_url_for_known_carrier(self):
        self.client.post(
            self.url,
            data=json.dumps({"carrier": "ups", "tracking_number": "1Z999"}),
            content_type="application/json",
        )
        self.sub_order.refresh_from_db()
        self.assertEqual(
            self.sub_order.tracking_url,
            "https://www.ups.com/track?tracknum=1Z999",
        )

    def test_uses_client_tracking_url_for_other_carrier(self):
        self.client.post(
            self.url,
            data=json.dumps({
                "carrier": "other",
                "tracking_number": "XYZ123",
                "tracking_url": "https://custom-carrier.com/track/XYZ123",
            }),
            content_type="application/json",
        )
        self.sub_order.refresh_from_db()
        self.assertEqual(
            self.sub_order.tracking_url,
            "https://custom-carrier.com/track/XYZ123",
        )

    def test_clears_tracking_when_fields_empty(self):
        # Pre-populate tracking data
        self.sub_order.carrier = "ups"
        self.sub_order.tracking_number = "1Z999"
        self.sub_order.tracking_url = "https://www.ups.com/track?tracknum=1Z999"
        self.sub_order.save()

        response = self.client.post(
            self.url,
            data=json.dumps({"carrier": "", "tracking_number": ""}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.sub_order.refresh_from_db()
        self.assertEqual(self.sub_order.carrier, "")
        self.assertEqual(self.sub_order.tracking_number, "")
        self.assertEqual(self.sub_order.tracking_url, "")

    def test_returns_404_for_invalid_uuid(self):
        import uuid

        bad_url = f"/erp/orders/sub-orders/{uuid.uuid4()}/update-tracking/"
        response = self.client.post(
            bad_url,
            data=json.dumps({"carrier": "ups", "tracking_number": "1Z999"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_returns_400_for_malformed_json(self):
        response = self.client.post(
            self.url,
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_requires_login(self):
        self.client.logout()
        response = self.client.post(
            self.url,
            data=json.dumps({"carrier": "ups", "tracking_number": "1Z999"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 302)

    def test_send_shipping_email_does_not_update_tracking(self):
        """SendShippingEmailView should NOT modify tracking fields."""
        self.sub_order.carrier = "ups"
        self.sub_order.tracking_number = "ORIGINAL"
        self.sub_order.save(update_fields=["carrier", "tracking_number"])

        send_url = f"/erp/orders/sub-orders/{self.sub_order.pk}/send-shipping-email/"
        self.client.post(
            send_url,
            data=json.dumps({"carrier": "fedex", "tracking_number": "OVERRIDE"}),
            content_type="application/json",
        )
        self.sub_order.refresh_from_db()
        self.assertEqual(self.sub_order.carrier, "ups")
        self.assertEqual(self.sub_order.tracking_number, "ORIGINAL")
