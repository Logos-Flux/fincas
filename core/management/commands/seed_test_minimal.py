"""Seed a minimal deterministic dataset for the testing-plan suites.

Creates (idempotent via get_or_create):
- Company "Test Minimal Corp" (+ staff user ``tester`` / ``test12345``)
- Warehouse TEST-WH1 with stock for every variant
- 1 brand, 1 category, 10 products each with 1 variant + retail price
- Price books: "Test Retail" (USD, default) and "Test Wholesale" (USD)
- 1 retail customer linked to the default price list
- 1 approved distributor supplier

Run from a fresh database: ``manage.py migrate && manage.py seed_test_minimal``.
Safe to re-run — counts must not change on the second run.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Company
from customers.models import Customer
from inventory.models import Warehouse, WarehouseStock
from pricing.models.price_book import PriceList, PriceListItem
from products.models import Brand, Category, Product, ProductVariant
from purchasing.models import Supplier


class Command(BaseCommand):
    help = "Seed minimal deterministic test dataset (see testing-plan §2)."

    @transaction.atomic
    def handle(self, *args, **options):
        User = get_user_model()
        user, _ = User.objects.get_or_create(
            username="tester",
            defaults={"email": "tester@example.com", "is_staff": True},
        )
        if not user.check_password("test12345"):
            user.set_password("test12345")
            user.is_staff = True
            user.save()

        company, _ = Company.objects.get_or_create(name="Test Minimal Corp")

        warehouse, _ = Warehouse.objects.get_or_create(
            warehouse_code="TEST-WH1",
            defaults={"name": "Test Warehouse 1", "default_for_company": company},
        )

        brand, _ = Brand.objects.get_or_create(title="Test Brand")
        category, _ = Category.objects.get_or_create(title="Test Category")

        retail, _ = PriceList.objects.get_or_create(
            name="Test Retail",
            defaults={
                "price_list_type": PriceList.PriceListType.SELLING,
                "is_default": True,
                "is_active": True,
                "currency": "USD",
                "company": company,
            },
        )
        wholesale, _ = PriceList.objects.get_or_create(
            name="Test Wholesale",
            defaults={
                "price_list_type": PriceList.PriceListType.SELLING,
                "is_default": False,
                "is_active": True,
                "currency": "USD",
                "company": company,
            },
        )

        for i in range(1, 11):
            product, _ = Product.objects.get_or_create(
                slug=f"test-product-{i:02d}",
                defaults={
                    "title": f"Test Product {i:02d}",
                    "product_type": Product.ProductType.PHYSICAL,
                    "brand": brand,
                    "is_active": True,
                    "visible_on_website": True,
                },
            )
            variant, _ = ProductVariant.objects.get_or_create(
                sku=f"TEST-SKU-{i:02d}",
                defaults={
                    "product": product,
                    "name": f"Test Product {i:02d} / Default",
                    "retail_price": Decimal("19.99") + i,
                    "stock_status": "in_stock",
                },
            )
            WarehouseStock.objects.get_or_create(
                variant=variant,
                warehouse=warehouse,
                defaults={"quantity": Decimal(100)},
            )
            PriceListItem.objects.get_or_create(
                price_list=retail,
                variant=variant,
                defaults={"price": variant.retail_price, "min_qty": 1},
            )
            PriceListItem.objects.get_or_create(
                price_list=wholesale,
                variant=variant,
                defaults={
                    "price": (variant.retail_price * Decimal("0.8")).quantize(
                        Decimal("0.0001")
                    ),
                    "min_qty": 10,
                },
            )

        Customer.objects.get_or_create(
            email="retail-customer@example.com",
            defaults={
                "first_name": "Retail",
                "last_name": "Customer",
                "company": company,
                "pricing_tier": Customer.PricingTier.RETAIL,
                "default_price_list": retail,
                "is_active": True,
            },
        )

        Supplier.objects.get_or_create(
            supplier_code="TEST-SUP-01",
            defaults={
                "company_name": "Test Supplier Inc",
                "supplier_type": Supplier.SupplierType.DISTRIBUTOR,
                "company": company,
            },
        )

        self.stdout.write(self.style.SUCCESS("Seeded minimal test dataset."))
