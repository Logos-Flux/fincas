"""Seed rows for the upgrade-path test, run at the PREVIOUS release's code.

Usage (from a checkout of the previous tag, after ``migrate`` and
``seed_demo_data``)::

    python manage.py shell --settings=config.settings.dev < /path/to/HEAD/scripts/upgrade/seed_previous_release.py

It writes the rows the data migrations after v0.3.0-rc.1 key on: EmailLogs
of every document type (communications.0003), Leads (crm.0012), ActivityLogs
(crm.0013) and a quote with pipeline data (crm.0010). Migrations already
applied at the previous tag (crm.0005, parties.0002, core.0002, ...) are not
exercised here. Only fields that
existed by v0.3.0 are used, so the script keeps working against older tags;
it is run by the previous release's code, never by HEAD's. Against a tag that
already froze ActivityLog (v0.4.0-rc.1 on) it skips those rows.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone

from communications.models import EmailLog
from core.models import Company, UserProfile
from crm import models as crm_models
from crm.models import ActivityLog, Lead
from customers.models import Customer
from orders.models import Order, Quote, SalesInvoice, SubOrder

company = Company.objects.order_by("created_at").first()
assert company is not None, "run seed_demo_data first"
customer = Customer.objects.filter(company=company).order_by("created_at").first()
assert customer is not None, "run seed_demo_data first"
admin = get_user_model().objects.get(username="admin")
profile, _ = UserProfile.objects.get_or_create(user=admin, defaults={"company": company})
now = timezone.now()

quote = Quote.objects.create(customer=customer, company=company, status="sent", sales_rep=profile)
order = Order.objects.create(customer=customer, company=company, subtotal_ex_tax=100, grand_total=100)
invoice = SalesInvoice.objects.create(
    order=order, company=company, invoice_date=date.today(),
    subtotal_ex_tax=Decimal("100"), grand_total=Decimal("100"), balance_due=Decimal("100"),
)
sub_order = SubOrder.objects.create(order=order, sub_order_number="UPG-1-1", status="shipped")

lead = Lead.objects.create(
    company=company, lead_number="LD-UPGRADE-1", first_name="Grace", last_name="Hopper",
    email="grace@example.com", status="new", request_type="quote", message="Upgrade-path lead",
    utm_source="newsletter",
)
Lead.objects.create(
    company=company, lead_number="LD-UPGRADE-2", first_name="Alan", last_name="Turing",
    email="alan@example.com", status="closed_lost", loss_reason="price",
)
# From v0.4.0-rc.1 on, ActivityLog is frozen (CRM Phase 3 step 6) and its rows
# were already moved by crm.0013 at that tag, so there is nothing to seed.
frozen = getattr(crm_models, "ActivityLogFrozen", ())  # () catches nothing
for kind, fields in (
    ("call", {"lead": lead, "duration_minutes": 15}),
    ("note", {"customer": customer}),
    ("task", {"customer": customer}),
    ("email", {"customer": customer}),
):
    try:
        ActivityLog.objects.create(
            company=company, activity_type=kind, subject=f"Upgrade {kind}", description="seeded",
            performed_by=profile, activity_date=now - timedelta(days=1), **fields,
        )
    except frozen:
        print("activity logs: frozen at this tag, none seeded")
        break

recipient = customer.email or "buyer@example.com"
for document, document_type, status in (
    (quote, "quote", "sent"),
    (order, "sales_order", "sent"),
    (order, "sales_order", "bounced"),
    (invoice, "invoice", "sent"),
    (sub_order, "shipping_notification", "sent"),
    (order, "purchase_order", "sent"),  # skipped by the backfill (suppliers are Phase 4)
):
    EmailLog.objects.create(
        document_type=document_type, document_id=document.pk, document_number="UPG",
        recipient_email=recipient, subject=f"Your {document_type}", template_name=document_type,
        status=status, triggered_by="manual", sent_by=admin,
    )

print(f"seeded: email logs {EmailLog.objects.count()}, leads {Lead.objects.count()}, "
      f"activity logs {ActivityLog.objects.count()}, quotes {Quote.objects.count()}")
# The point of the test: communications.0003 must meet EmailLog rows (the
# downstream copy that hit #88 had 22; a fresh or staging database has 0).
if not EmailLog.objects.exclude(document_type="purchase_order").exists():
    raise SystemExit("no EmailLog rows seeded")
