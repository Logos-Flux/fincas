"""Constants and helpers for the release-test seed (``manage.py seed_release_test``).

Shipped with the package (a management command may not import ``config``),
imported by ``config/settings/harness.py`` for the tenant id.
"""

from __future__ import annotations

import contextlib
import datetime
import random
import uuid
from unittest import mock

#: The one Company every release-test database holds; the default
#: ``GOLDEN_PORTAL_COMPANY`` under the harness settings.
HARNESS_COMPANY_ID = uuid.UUID("0c0ffee0-4e1e-4a5e-9a11-0000000d0001")
HARNESS_COMPANY_NAME = "Golden Commerce Corp"  # what seed_demo_data looks up by name

#: The day the fresh seed is written for (``--as-of``). The runner rolls the
#: demo clock to today afterwards, so "due today" is today on any date.
DEFAULT_AS_OF = datetime.date(2026, 10, 15)
SEED_TAG = "seed_release_test"
UUID_SEED = "golden-release-test"


@contextlib.contextmanager
def deterministic_uuids(seed: str = UUID_SEED):
    """Make every ``default=uuid.uuid4`` primary key come from one seeded stream.

    Two seeds of an empty database then give the same ids (the determinism
    test hashes them). The field default is a bound function object, so
    patching ``uuid.uuid4`` would not reach it: each field's ``default`` is
    swapped and its cached ``_get_default`` dropped, then restored.
    """
    from django.apps import apps

    rng = random.Random(seed)

    def next_uuid():
        return uuid.UUID(bytes=rng.randbytes(16), version=4)

    patched = []
    for model in apps.get_models():
        for field in model._meta.local_fields:
            if getattr(field, "default", None) is uuid.uuid4:
                patched.append(field)
                field.default = next_uuid
                field.__dict__.pop("_get_default", None)
    try:
        yield
    finally:
        for field in patched:
            field.default = uuid.uuid4
            field.__dict__.pop("_get_default", None)


def as_of_moment(day: datetime.date, hhmm: str = "10:00"):
    """``day`` at ``hhmm`` in the deployment's TIME_ZONE, as an aware UTC datetime."""
    from zoneinfo import ZoneInfo

    from django.conf import settings

    hour, minute = map(int, hhmm.split(":"))
    local = datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=ZoneInfo(settings.TIME_ZONE))
    return local.astimezone(datetime.UTC)


def frozen_now(when: datetime.datetime):
    """Pin ``django.utils.timezone.now`` (what ``auto_now_add`` and the seeds read)."""
    return mock.patch("django.utils.timezone.now", return_value=when)


# ── R5 fixture: a paid, shipped order ────────────────────────────────────────

#: The order R5 "Returns and refunds" starts from (docs/release-hardening-plan.md
#: section 4): shipped, invoiced and paid, with the invoice and the payment
#: posted to the books. Keyed on this number, so the seed is idempotent.
PAID_ORDER_NUMBER = "SO-HRN-PAID-01"
PAID_ORDER_SKU = "ACT-500W"
PAID_ORDER_QTY = 2


def seed_paid_shipped_order(company, *, stdout=None):
    """Create the R5 order unless it exists; return ``(order, created)``.

    Separate from the rest of the seed on purpose (a journey fixture, not
    catalog). Uses the first customer, variant and warehouse the base seed
    made when the named ones are missing (a scrubbed-copy overlay), and
    skips with a note when the company has no posting rule for invoices or
    payments (the invoice and payment would refuse to post).
    """
    from decimal import Decimal

    from django.utils import timezone

    from accounting.models import DocumentPostingRule
    from customers.models import Customer
    from inventory.models import Warehouse
    from orders.models import Order, OrderLineItem, SalesInvoice, ShipmentGroup
    from payments.models import Payment
    from products.models import ProductVariant

    say = stdout.write if stdout is not None else (lambda *_: None)
    existing = Order._base_manager.filter(order_number=PAID_ORDER_NUMBER).first()
    if existing is not None:
        say(f"  Paid order {PAID_ORDER_NUMBER} exists.")
        return existing, False
    rules = set(DocumentPostingRule.objects.filter(company=company).values_list("doc_type", flat=True))
    if {DocumentPostingRule.DocType.SALES_INVOICE, DocumentPostingRule.DocType.PAYMENT} - rules:
        say(f"  Paid order {PAID_ORDER_NUMBER} skipped: no invoice/payment posting rules for this company.")
        return None, False
    customer = (
        Customer.objects.filter(company=company, email__iexact="procurement@acme-industrial.test").first()
        or Customer.objects.filter(company=company, is_active=True).order_by("created_at").first()
    )
    variant = (
        ProductVariant.objects.filter(sku=PAID_ORDER_SKU).first()
        or ProductVariant.objects.filter(is_active=True).order_by("sku").first()
    )
    warehouse = (
        Warehouse.objects.filter(warehouse_code="WH-EAST").first()
        or Warehouse.objects.filter(company=company).order_by("warehouse_code").first()
    )
    if customer is None or variant is None:
        say(f"  Paid order {PAID_ORDER_NUMBER} skipped: no customer or variant to put on it.")
        return None, False
    unit = Decimal(variant.retail_price or variant.wholesale_price or 100)
    qty = Decimal(PAID_ORDER_QTY)
    order = Order.objects.create(
        company=company, customer=customer, order_number=PAID_ORDER_NUMBER,
        status=Order.Status.SHIPPED, payment_status=Order.PaymentStatus.PAID,
        fulfillment_status=Order.FulfillmentStatus.FULFILLED,
        internal_notes="Release-test fixture: paid and shipped (R5).",
    )
    group = ShipmentGroup.objects.create(
        order=order, name="Shipment 1", sort_order=0, warehouse=warehouse,
        fulfillment_type=ShipmentGroup.FulfillmentType.INTERNAL,
    )
    OrderLineItem.objects.create(
        order=order, shipment_group=group, variant=variant, sku=variant.sku,
        item_name=str(getattr(variant, "title", "") or variant.sku), quantity=qty, quantity_shipped=qty,
        unit_price_ex_tax=unit, line_total_ex_tax=unit * qty,
        fulfillment_status=OrderLineItem.FulfillmentStatus.SHIPPED,
    )
    order.recalculate_totals()
    order.refresh_from_db()
    total = Decimal(order.grand_total or 0)
    today = timezone.now().date()
    invoice = SalesInvoice.objects.create(
        order=order, company=company, status=SalesInvoice.Status.PAID, invoice_date=today, paid_date=today,
        subtotal_ex_tax=order.subtotal_ex_tax or 0, tax_total=order.tax_total or 0,
        shipping_cost=order.shipping_cost_ex_tax or 0, grand_total=total, amount_paid=total,
        balance_due=Decimal("0"),
    )
    Payment.objects.create(
        order=order, company=company, invoice=invoice, amount=total, currency=order.currency or "USD",
        payment_method=Payment.PaymentMethod.CREDIT_CARD, payment_provider=Payment.PaymentProvider.MANUAL,
        payment_type=Payment.PaymentType.PAYMENT, status=Payment.Status.CAPTURED,
        payment_date=timezone.now(), transaction_id=f"harness-{PAID_ORDER_NUMBER}",
    )
    say(f"  Paid order {PAID_ORDER_NUMBER} created ({variant.sku} x {PAID_ORDER_QTY}, {total} paid, posted).")
    return order, True


#: The signed-in account with no staff role that R10 drives at ``/erp/``.
NONSTAFF_LOGIN = "harness-nonstaff"


def ensure_nonstaff_login(runs_dir, username: str = NONSTAFF_LOGIN) -> str:
    """A login that can sign in but holds no staff role (R10: ``/erp/`` must 403).

    Created, or reset, with a fresh random password, which is written only to
    ``<runs_dir>/logins.txt`` as ``<username> <password> nonstaff`` (mode 0600;
    an earlier line for the same username is replaced). Returns the username.
    Called by the journey through ``manage.py shell``, never at app start.
    """
    import os
    import secrets
    from pathlib import Path

    from django.contrib.auth import get_user_model

    from core.models import ERPUserSettings

    User = get_user_model()
    password = secrets.token_urlsafe(12)
    user, _ = User.objects.get_or_create(
        username=username,
        defaults={"email": f"{username}@example.com", "first_name": "Harness", "last_name": "Visitor"},
    )
    user.set_password(password)
    user.is_active = True
    user.is_staff = False
    user.is_superuser = False
    user.save()
    user.groups.clear()
    ERPUserSettings.objects.update_or_create(
        user=user, defaults={"must_change_password": False, "email_verified": True})
    path = Path(runs_dir) / "logins.txt"
    header = "# username password role  (seed_release_test; never commit)"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines()
             if not line.startswith(f"{username} ")] if path.exists() else [header]
    lines.append(f"{username} {password} nonstaff")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    return username
