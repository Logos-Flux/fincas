"""
Import a BigCommerce order from JSON data and generate a SalesInvoice.

Usage:
    python manage.py import_bc_invoice 743 --bc-data '{...}'
    python manage.py import_bc_invoice 743 --bc-data "$(cat /tmp/bc_order_743.json)" --dry-run
"""

import json
from datetime import date, datetime, timezone
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Company
from core.provenance import is_erased_source
from customers.models import Customer, CustomerAddress
from orders.models import Order, OrderLineItem, SalesInvoice
from parties.erasure import erased_address
from parties.sync import run_guarded, sync_from_customer
from payments.models import Payment


def parse_bc_date(date_str):
    """Parse BigCommerce date format: 'Wed, 06 Aug 2025 18:38:09 +0000'"""
    try:
        return datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %z").date()
    except (ValueError, TypeError):
        return date.today()


class Command(BaseCommand):
    help = "Import a BigCommerce order and generate a SalesInvoice"

    def add_arguments(self, parser):
        parser.add_argument("bc_order_id", type=str, help="BigCommerce order ID")
        parser.add_argument(
            "--bc-data",
            type=str,
            help="JSON string of BC order data",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview without saving",
        )

    def handle(self, *args, **options):
        bc_order_id = options["bc_order_id"]

        if options["bc_data"]:
            bc = json.loads(options["bc_data"])
        else:
            raise CommandError(
                "Pass --bc-data with the JSON. Example:\n"
                "  python manage.py import_bc_invoice 743 --bc-data '{...}'"
            )

        company = Company.objects.first()
        if not company:
            raise CommandError("No Company exists. Create one first.")

        bc_customer_id = str(bc.get("customer_id", "") or "")
        billing_email = (bc.get("billing_address") or {}).get("email", "")
        if (bc_customer_id and is_erased_source(company.pk, "bigcommerce", "customer", bc_customer_id)) or (
                erased_address(company.pk, billing_email)):
            # phase2-party.md 1.7: never resurrect an erased person.
            self.stdout.write(self.style.WARNING(
                "Skipped: this order's customer was erased; it is not imported."
            ))
            return

        billing = bc.get("billing_address", {})
        order_date = parse_bc_date(bc.get("date_created"))

        # Extract customer PO from staff notes (B2B Ninja ref)
        staff_notes = bc.get("staff_notes", "")
        customer_po = ""
        if "B2B Ninja:" in staff_notes:
            customer_po = staff_notes.split("B2B Ninja:")[1].strip().split("\n")[0].strip()

        # Extract SKU from customer message
        customer_msg = bc.get("customer_message", "")
        sku = ""
        for part in customer_msg.split():
            if part.startswith("UNIPGRM"):
                sku = part.rstrip(",").rstrip(".")
                break
        if not sku:
            sku = "UNIPGRM-5P2"  # Known from BC data

        subtotal = Decimal(bc.get("subtotal_ex_tax", "0"))
        shipping = Decimal(bc.get("shipping_cost_ex_tax", "0"))
        tax = Decimal(bc.get("total_tax", "0"))
        total = Decimal(bc.get("total_inc_tax", "0"))

        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(f"BC Order #{bc_order_id}")
        self.stdout.write(f"Customer: {billing.get('first_name')} {billing.get('last_name')}")
        self.stdout.write(f"Email:    {billing.get('email')}")
        self.stdout.write(f"Date:     {order_date}")
        self.stdout.write(f"SKU:      {sku}")
        self.stdout.write(f"Subtotal: ${subtotal}")
        self.stdout.write(f"Shipping: ${shipping}")
        self.stdout.write(f"Tax:      ${tax}")
        self.stdout.write(f"Total:    ${total}")
        self.stdout.write(f"Customer PO: {customer_po or '(none)'}")
        self.stdout.write(f"Payment:  {bc.get('payment_method')} ({bc.get('payment_status')})")
        self.stdout.write(f"{'='*60}\n")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing saved"))
            return

        with transaction.atomic():
            # 1. Get or create customer
            # One commerce account per address per tenant, case-insensitively
            # (uniq_customer_email_ci_company, phase2-party.md 2.2).
            customer = Customer.objects.filter(company=company, email__iexact=billing.get("email", "")).first()
            created = customer is None
            if created:
                customer = Customer(
                    email=billing.get("email", ""),
                    first_name=billing.get("first_name", ""),
                    last_name=billing.get("last_name", ""),
                    phone=billing.get("phone", ""),
                    company=company,
                    bc_id=str(bc.get("customer_id", "")),
                )
                customer.party_origin = "import"  # parties.sync: Party.origin of a new party
                customer.save()
            else:
                # Re-sync the existing customer's party (phase2-party.md 2.1): an
                # earlier import or a bulk write may have bypassed save().
                run_guarded(sync_from_customer, customer, origin="import")
            if created:
                self.stdout.write(f"Created customer: {customer}")
            else:
                self.stdout.write(f"Found existing customer: {customer}")

            # 2. Create billing address
            bill_addr = CustomerAddress.objects.create(
                customer=customer,
                address_type="billing",
                first_name=billing.get("first_name", ""),
                last_name=billing.get("last_name", ""),
                company_name=billing.get("company", ""),
                address_line_1=billing.get("street_1", ""),
                address_line_2=billing.get("street_2", ""),
                city=billing.get("city", ""),
                state_province=billing.get("state", ""),
                postal_code=billing.get("zip", ""),
                country=billing.get("country_iso2", "US"),
                phone=billing.get("phone", ""),
            )
            self.stdout.write(f"Created billing address: {bill_addr}")

            # 3. Create order
            order_number = f"SO-BC{bc_order_id}"
            order = Order.objects.create(
                order_number=order_number,
                customer=customer,
                billing_address=bill_addr,
                company=company,
                status=Order.Status.COMPLETED,
                payment_status=Order.PaymentStatus.PAID,
                payment_terms="Prepaid",
                customer_po_number=customer_po,
                subtotal_ex_tax=subtotal,
                subtotal_inc_tax=subtotal,
                tax_total=tax,
                shipping_cost_ex_tax=shipping,
                shipping_cost_inc_tax=shipping,
                grand_total=total,
                amount_paid=total,
                balance_due=Decimal("0"),
                bc_id=str(bc_order_id),
                external_source="bigcommerce",
                ip_address=bc.get("ip_address") or None,
                internal_notes=staff_notes,
                metadata={"bc_import": True, "bc_data_keys": list(bc.keys())},
            )
            self.stdout.write(f"Created order: {order.order_number}")

            # 4. Create line item
            OrderLineItem.objects.create(
                order=order,
                sku=sku,
                item_name="Tamarack UNI-PGRM Top of Pole Mount \u2014 5 Solar Panels / 38\u201d\u201372\u201d",
                quantity=1,
                unit_price_ex_tax=subtotal,
                unit_price_inc_tax=subtotal,
                line_total_ex_tax=subtotal,
                line_total_inc_tax=subtotal,
                sort_order=0,
            )
            self.stdout.write(f"Created line item: {sku}")

            # 5. Create invoice
            invoice = SalesInvoice.objects.create(
                invoice_number=f"INV-BC{bc_order_id}",
                order=order,
                company=company,
                invoice_date=order_date,
                payment_terms=SalesInvoice.PaymentTerms.PREPAID,
                subtotal_ex_tax=subtotal,
                tax_total=tax,
                shipping_cost=shipping,
                grand_total=total,
                amount_paid=total,
                balance_due=Decimal("0"),
                status=SalesInvoice.Status.PAID,
                paid_date=order_date,
                notes=f"Imported from BigCommerce Order #{bc_order_id}",
            )
            self.stdout.write(f"Created invoice: {invoice.invoice_number}")

            # 6. Create payment record
            payment = Payment.objects.create(
                order=order,
                invoice=invoice,
                amount=total,
                payment_method=Payment.PaymentMethod.CREDIT_CARD,
                payment_provider=Payment.PaymentProvider.STRIPE,
                payment_type=Payment.PaymentType.PAYMENT,
                status=Payment.Status.CAPTURED,
                transaction_id=bc.get("payment_provider_id", ""),
                payment_date=datetime.combine(order_date, datetime.min.time(), tzinfo=timezone.utc),
                company=company,
                notes=f"BC Order #{bc_order_id} \u2014 {bc.get('payment_method', '')}",
            )
            self.stdout.write(f"Created payment: {payment.payment_number}")

        self.stdout.write(self.style.SUCCESS(f"\nDone! Invoice {invoice.invoice_number} ready."))
        self.stdout.write(f"View: /erp/payments/{invoice.pk}/")
        self.stdout.write(f"PDF:  /erp/payments/{invoice.pk}/pdf/")
