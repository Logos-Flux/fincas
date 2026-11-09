"""Seed the Fincas database with generic, production-ready demo data.

Creates:
- Default admin superuser (admin / admin or custom password)
- Default company entity
- Default warehouse & stock locations
- Categories, brands, products, and variants
- Selling price lists & tiered volume items
- Customers & addresses
- Approved suppliers
- Standard GAAP chart of accounts
- Demo parties with contact points (``seed_parties``)
- Demo CRM engagements with history (``seed_crm``)
- The demo login's own My day (``seed_my_day``), when that account exists
- The demo clock's anchor (``core.demo_clock``): today, unless one is stored,
  so ``demo_roll_dates`` can later move every date up to the current day
"""

from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction

from accounting.models import Account, DocumentPostingRule
from core import demo_clock
from core.models import Company, ERPUserSettings, UserProfile
from customers.models import Customer, CustomerAddress
from inventory.models import Warehouse, WarehouseStock
from pricing.models.price_book import PriceList, PriceListItem
from products.models import Brand, Category, Product, ProductVariant
from purchasing.models import Supplier


class Command(BaseCommand):
    help = "Seed database with rich, vertical-agnostic demonstration data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--admin-username",
            default="admin",
            help="Superuser username (default: admin)",
        )
        parser.add_argument(
            "--admin-password",
            default="admin",
            help="Superuser password (default: admin)",
        )
        parser.add_argument(
            "--admin-email",
            default="admin@example.com",
            help="Superuser email (default: admin@example.com)",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("Seeding Fincas demonstration data..."))

        # 1. Admin Superuser
        User = get_user_model()
        username = options["admin_username"]
        password = options["admin_password"]
        email = options["admin_email"]

        user, created = User.objects.get_or_create(
            username=username,
            defaults={"email": email, "is_staff": True, "is_superuser": True},
        )
        if created or not user.check_password(password):
            user.set_password(password)
            user.is_staff = True
            user.is_superuser = True
            user.save()
            self.stdout.write(f"  Created / updated superuser '{username}'.")
        else:
            self.stdout.write(f"  Superuser '{username}' already configured.")

        ERPUserSettings.objects.get_or_create(
            user=user,
            defaults={"must_change_password": False, "email_verified": True},
        )

        # 2. Company
        company, _ = Company.objects.get_or_create(
            name="Golden Commerce Corp",
            defaults={
                "website_url": "https://golden-erp.org",
                "phone": "+1-800-555-0199",
                "notes": "Primary corporate operating entity for Fincas demo.",
            },
        )
        self.stdout.write(f"  Company '{company.name}' verified.")

        # 3. Warehouse (Step 2: owned by the seed company)
        warehouse, _ = Warehouse.objects.get_or_create(
            warehouse_code="WH-MAIN",
            defaults={
                "name": "Central Distribution Hub",
                "address_line_1": "100 Logistics Blvd",
                "city": "Chicago",
                "state_province": "IL",
                "postal_code": "60601",
                "country": "US",
                "is_active": True,
                "company": company,
            },
        )
        if warehouse.company_id is None:
            warehouse.company = company
            warehouse.save(update_fields=["company"])
        self.stdout.write(f"  Warehouse '{warehouse.name}' ({warehouse.warehouse_code}) verified.")

        # 4. Brands
        brand_apex, _ = Brand.objects.get_or_create(
            title="Apex Dynamics",
            defaults={
                "description": "Industrial electromechanical components and automation systems.",
                "visible_on_website": True,
            },
        )
        brand_vanguard, _ = Brand.objects.get_or_create(
            title="Vanguard Precision",
            defaults={
                "description": "High-precision instrumentation, sensors, and structural hardware.",
                "visible_on_website": True,
            },
        )

        # 5. Categories
        cat_hardware, _ = Category.objects.get_or_create(
            title="Industrial Hardware",
            defaults={"description": "Brackets, fasteners, and heavy-duty structural mountings."},
        )
        cat_electronics, _ = Category.objects.get_or_create(
            title="Control Electronics",
            defaults={"description": "Programmable logic controllers, actuators, and power modules."},
        )

        # 6. Products & Variants
        demo_catalog = [
            {
                "title": "Industrial High-Torque Servo Actuator 500W",
                "brand": brand_apex,
                "category": cat_electronics,
                "part_number": "ACT-500W",
                "sku": "ACT-500W",
                "retail_price": Decimal("389.00"),
                "wholesale_price": Decimal("275.00"),
                "stock": 42,
                "bin": "A-01-04",
                "reorder_level": 10,
            },
            {
                "title": "Modular Logic Controller Core Module",
                "brand": brand_apex,
                "category": cat_electronics,
                "part_number": "MLC-CORE-08",
                "sku": "MLC-CORE-08",
                "retail_price": Decimal("649.50"),
                "wholesale_price": Decimal("460.00"),
                "stock": 18,
                "bin": "A-02-11",
                "reorder_level": 5,
            },
            {
                "title": "Reinforced Structural Corner Bracket 90-Deg",
                "brand": brand_vanguard,
                "category": cat_hardware,
                "part_number": "BRK-90-HD",
                "sku": "BRK-90-HD",
                "retail_price": Decimal("24.95"),
                "wholesale_price": Decimal("14.50"),
                "stock": 350,
                "bin": "B-14-02",
                "reorder_level": 50,
            },
            {
                "title": "Ultra-Precision Laser Measurement Sensor",
                "brand": brand_vanguard,
                "category": cat_electronics,
                "part_number": "SNR-LASER-30",
                "sku": "SNR-LASER-30",
                "retail_price": Decimal("895.00"),
                "wholesale_price": Decimal("620.00"),
                "stock": 8,
                "bin": "A-04-01",
                "reorder_level": 3,
            },
        ]

        created_variants = []
        for item in demo_catalog:
            prod, _ = Product.objects.get_or_create(
                title=item["title"],
                defaults={
                    "brand": item["brand"],
                    "part_number": item["part_number"],
                    "is_active": True,
                    "visible_on_website": True,
                },
            )
            prod.categories.add(item["category"])

            variant, _ = ProductVariant.objects.get_or_create(
                sku=item["sku"],
                defaults={
                    "product": prod,
                    "name": item["title"],
                    "retail_price": item["retail_price"],
                    "wholesale_price": item["wholesale_price"],
                    "stock": item["stock"],
                    "is_active": True,
                    "visible_on_website": True,
                },
            )
            created_variants.append(variant)

            # Stock entry in warehouse
            WarehouseStock.objects.update_or_create(
                warehouse=warehouse,
                variant=variant,
                defaults={
                    "quantity": item["stock"],
                    "bin_location": item["bin"],
                    "reorder_level": item["reorder_level"],
                },
            )

        self.stdout.write(f"  Populated {len(created_variants)} products & variants with warehouse inventory.")

        # 7. Selling Price Lists
        retail_list, _ = PriceList.objects.get_or_create(
            name="Standard Retail",
            defaults={
                "price_list_type": PriceList.PriceListType.SELLING,
                "is_default": True,
                "is_active": True,
                "currency": "USD",
                "company": company,
            },
        )
        wholesale_list, _ = PriceList.objects.get_or_create(
            name="Wholesale Partner Tier 1",
            defaults={
                "price_list_type": PriceList.PriceListType.SELLING,
                "is_default": False,
                "is_active": True,
                "currency": "USD",
                "company": company,
            },
        )

        for var in created_variants:
            if var.retail_price:
                PriceListItem.objects.update_or_create(
                    price_list=retail_list,
                    variant=var,
                    min_qty=Decimal("1.000"),
                    defaults={"price": var.retail_price, "is_active": True},
                )
            if var.wholesale_price:
                PriceListItem.objects.update_or_create(
                    price_list=wholesale_list,
                    variant=var,
                    min_qty=Decimal("5.000"),
                    defaults={"price": var.wholesale_price, "is_active": True},
                )

        # 8. Customers & Addresses
        cust, _ = Customer.objects.get_or_create(
            email="procurement@acme-industrial.test",
            defaults={
                "first_name": "Marcus",
                "last_name": "Sterling",
                "company_name": "Acme Industrial Technologies",
                "customer_type": Customer.CustomerType.BUSINESS,
                "pricing_tier": Customer.PricingTier.WHOLESALE,
                "company": company,
                "phone": "+1-312-555-0144",
                "is_active": True,
            },
        )

        CustomerAddress.objects.get_or_create(
            customer=cust,
            address_type=CustomerAddress.AddressType.SHIPPING,
            defaults={
                "address_line_1": "450 Industrial Parkway",
                "city": "Peoria",
                "state_province": "IL",
                "postal_code": "61602",
                "country": "US",
                "is_default": True,
            },
        )

        # 9. Suppliers. A supplier belongs to the company: a shipment group
        # or purchase order keeps a vendor only when it is the document
        # company's own (orders.erp_views._save_shipment_groups), and the
        # vendor picker offers only those. An unbound SUPP-001 from an
        # earlier seed is adopted (R1.10, 2026-10-17).
        supplier, created = Supplier.objects.get_or_create(
            supplier_code="SUPP-001",
            defaults={
                "company_name": "Global Components Manufacturing",
                "display_name": "Global Components",
                "supplier_type": Supplier.SupplierType.MANUFACTURER,
                "primary_contact_name": "David Vance",
                "primary_contact_email": "orders@globalcomponents.test",
                "is_active": True,
                "is_approved": True,
                "company": company,
            },
        )
        if not created and supplier.company_id is None:
            supplier.company = company
            supplier.save(update_fields=["company", "updated_at"])

        # 10. Standard Chart of Accounts
        standard_accounts = [
            ("1010", "Cash and Cash Equivalents", Account.AccountType.ASSET, Account.AccountSubType.BANK),
            ("1200", "Accounts Receivable", Account.AccountType.ASSET, Account.AccountSubType.ACCOUNTS_RECEIVABLE),
            ("1400", "Inventory Asset", Account.AccountType.ASSET, Account.AccountSubType.INVENTORY),
            ("2010", "Accounts Payable", Account.AccountType.LIABILITY, Account.AccountSubType.ACCOUNTS_PAYABLE),
            ("3010", "Common Stock & Equity", Account.AccountType.EQUITY, Account.AccountSubType.EQUITY),
            ("4010", "Wholesale Merchandise Sales", Account.AccountType.INCOME, Account.AccountSubType.SALES_INCOME),
            ("4020", "Retail Sales", Account.AccountType.INCOME, Account.AccountSubType.SALES_INCOME),
            ("5010", "Cost of Goods Sold - Products", Account.AccountType.EXPENSE, Account.AccountSubType.COST_OF_GOODS_SOLD),
            ("6010", "Freight & Shipping Expense", Account.AccountType.EXPENSE, Account.AccountSubType.SHIPPING_EXPENSE),
            ("1490", "Suspense / Clearing", Account.AccountType.ASSET, Account.AccountSubType.OTHER_CURRENT_ASSET),
            ("2040", "Sales Tax Payable", Account.AccountType.LIABILITY, Account.AccountSubType.SALES_TAX_PAYABLE),
            ("2050", "Customer Deposits", Account.AccountType.LIABILITY, Account.AccountSubType.OTHER_CURRENT_LIABILITY),
            ("3020", "Opening Balance Equity", Account.AccountType.EQUITY, Account.AccountSubType.OPENING_BALANCE_EQUITY),
            ("4030", "Discounts Given", Account.AccountType.INCOME, Account.AccountSubType.DISCOUNTS_REFUNDS),
            ("4040", "Shipping Income", Account.AccountType.INCOME, Account.AccountSubType.SHIPPING_INCOME),
            ("6020", "Merchant Processing Fees", Account.AccountType.EXPENSE, Account.AccountSubType.PROCESSING_FEE_EXPENSE),
        ]

        for code, name, acct_type, sub_type in standard_accounts:
            Account.objects.get_or_create(
                company=company,
                account_number=code,
                defaults={
                    "name": name,
                    "account_type": acct_type,
                    "account_subtype": sub_type,
                    "is_active": True,
                },
            )

        # 10b. Default posting rules (AD-13) over the seeded chart.
        by_number = {
            a.account_number: a
            for a in Account.objects.filter(company=company)
        }

        def _rule(doc_type, **fields):
            rule, created = DocumentPostingRule.objects.get_or_create(
                company=company, doc_type=doc_type, defaults=fields
            )
            if not created:
                for field, account in fields.items():
                    setattr(rule, field, account)
                rule.save()
            return rule

        _rule(
            DocumentPostingRule.DocType.SALES_INVOICE,
            receivable_account=by_number["1200"],
            revenue_account=by_number["4010"],
            tax_account=by_number["2040"],
            discount_account=by_number["4030"],
            shipping_income_account=by_number["4040"],
        )
        _rule(
            DocumentPostingRule.DocType.PAYMENT,
            receivable_account=by_number["1200"],
            cash_account=by_number["1010"],
            fee_account=by_number["6020"],
            # Prepayments on orders with no invoice yet are customer deposits
            # until the invoice is issued (0.4.4).
            deposit_account=by_number["2050"],
        )
        _rule(
            DocumentPostingRule.DocType.REFUND,
            receivable_account=by_number["1200"],
            cash_account=by_number["1010"],
        )
        _rule(
            DocumentPostingRule.DocType.PURCHASE_RECEIPT,
            inventory_account=by_number["1400"],
            payable_account=by_number["2010"],
        )

        # 11. Parties (CRM Phase 2): idempotent top-up, so re-runs add nothing.
        call_command("seed_parties", count=40, company=str(company.pk), stdout=self.stdout)

        # 12. CRM engagements with history, tasks and communications (Phase 3
        # step 8): idempotent top-up on the seeded parties.
        call_command("seed_crm", count=60, company=str(company.pk), stdout=self.stdout)

        # 13. The public demo login's My day, when that account exists and
        # works in this company (staging creates it with setup_demo_user,
        # then runs seed_my_day itself).
        demo_login = settings.DEMO_LOGIN_USERNAME
        if demo_login and User.objects.filter(username=demo_login, is_staff=True).exists():
            works_in = (UserProfile.objects.filter(user__username=demo_login)
                        .values_list("company_id", flat=True).first())
            if works_in in (None, company.pk):
                call_command("seed_my_day", user=demo_login, company=str(company.pk), force=True, stdout=self.stdout)

        # 14. Everything above is dated relative to now: anchor the demo clock.
        demo_clock.ensure_anchor(stdout=self.stdout)

        # Echo the password only when it is the development default: a
        # supplied one (staging, read from stdin) must never reach stdout or
        # a log (it did until 2026-10-17).
        shown = password if password == "admin" else "(as supplied)"
        self.stdout.write(self.style.SUCCESS(
            "\n✅ Successfully seeded Fincas demo database!\n"
            f"   Admin credentials: {username} / {shown}\n"
            "   Ready at: http://localhost:8000/erp/"
        ))
