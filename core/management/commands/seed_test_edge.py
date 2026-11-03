"""Seed edge-case dataset for the testing-plan suites (tenancy, promos, periods).

Builds on ``seed_test_minimal`` — run that first. Creates (idempotent):
- Company "Edge Beta Corp" alongside "Test Minimal Corp" (cross-tenant tests)
- EUR price book "Edge Euro" on Beta
- Empty warehouse EDGE-EMPTY (no WarehouseStock rows → out-of-stock paths)
- Discount codes: EDGE-EXPIRED (valid_to in the past), EDGE-USED-UP
  (times_used == max_uses), EDGE-MIN500 ($500 minimum order)
- Fiscal periods on Test Minimal Corp: EDGE-CLOSED-2024 (closed) and
  EDGE-LOCKED-2024 (locked), plus EDGE-OPEN (open)

Safe to re-run — counts must not change on the second run.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounting.models import FiscalPeriod
from core.models import Company
from discounts.models import DiscountCode
from inventory.models import Warehouse
from pricing.models.price_book import PriceList


class Command(BaseCommand):
    help = "Seed edge-case test dataset (see testing-plan §2)."

    @transaction.atomic
    def handle(self, *args, **options):
        alpha, _ = Company.objects.get_or_create(name="Test Minimal Corp")
        beta, _ = Company.objects.get_or_create(name="Edge Beta Corp")

        PriceList.objects.get_or_create(
            name="Edge Euro",
            defaults={
                "price_list_type": PriceList.PriceListType.SELLING,
                "is_default": False,
                "is_active": True,
                "currency": "EUR",
                "company": beta,
            },
        )

        Warehouse.objects.get_or_create(
            warehouse_code="EDGE-EMPTY",
            defaults={"name": "Edge Empty Warehouse", "default_for_company": beta},
        )

        yesterday = timezone.now() - timedelta(days=1)
        last_year = timezone.now() - timedelta(days=365)
        DiscountCode.objects.get_or_create(
            code="EDGE-EXPIRED",
            defaults={
                "discount_type": DiscountCode.DiscountType.PERCENT,
                "value": Decimal("10.00"),
                "is_active": True,
                "valid_from": last_year,
                "valid_to": yesterday,
                "max_uses": 100,
                "company": alpha,
            },
        )
        DiscountCode.objects.get_or_create(
            code="EDGE-USED-UP",
            defaults={
                "discount_type": DiscountCode.DiscountType.PERCENT,
                "value": Decimal("15.00"),
                "is_active": True,
                "max_uses": 1,
                "times_used": 1,
                "company": alpha,
            },
        )
        DiscountCode.objects.get_or_create(
            code="EDGE-MIN500",
            defaults={
                "discount_type": DiscountCode.DiscountType.FIXED_AMOUNT,
                "value": Decimal("25.00"),
                "is_active": True,
                "max_uses": 100,
                "minimum_order": Decimal("500.00"),
                "company": alpha,
            },
        )

        FiscalPeriod.objects.get_or_create(
            company=alpha,
            name="EDGE-CLOSED-2024",
            defaults={
                "start_date": date(2024, 1, 1),
                "end_date": date(2024, 12, 31),
                "status": FiscalPeriod.Status.CLOSED,
            },
        )
        FiscalPeriod.objects.get_or_create(
            company=alpha,
            name="EDGE-LOCKED-2024",
            defaults={
                "start_date": date(2024, 1, 1),
                "end_date": date(2024, 6, 30),
                "status": FiscalPeriod.Status.LOCKED,
            },
        )
        today = date.today()
        FiscalPeriod.objects.get_or_create(
            company=alpha,
            name="EDGE-OPEN",
            defaults={
                "start_date": today.replace(day=1),
                "end_date": date(today.year + 1, 12, 31),
                "status": FiscalPeriod.Status.OPEN,
            },
        )

        self.stdout.write(self.style.SUCCESS("Seeded edge-case test dataset."))
