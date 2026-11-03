"""Reporting: the saved-query tenancy gap, and the /erp/reports/ numbers.

KNOWN GAP: ReportDefinition carries no company FK (saved queries are
global), so any authenticated user can list/retrieve/create/update/delete
every report definition, including via direct-PK guessing. Those tests pin
the CURRENT behaviour as evidence for the finding — characterization, not
approval. Adding a company FK + scoping (or staff-gating) MUST replace them
with isolation assertions.

The /erp/reports/ page (ReportListView) is tenant-scoped since step 0 of
CRM Phase 3; ``ReportListViewTests`` pins every number it shows.
"""

from __future__ import annotations

import itertools
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from crm.backfill import backfill_quote_engagements
from customers.models import Customer
from orders.models import Order, Quote
from reporting.models import ReportDefinition
from reporting.views import ReportDefinitionViewSet


def company_user(company, *, username="staff", uid="uid-staff"):
    user = make_staff_user(username=username, password="pw")
    user.userprofile = UserProfile.objects.create(company=company)
    return user


class ReportingTenancyGapTests(TestCase):
    """KNOWN GAP (M1 Track A): no tenant boundary on reporting."""

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.other = Company.objects.create(name="Rival, Inc")
        self.report = ReportDefinition.objects.create(
            name="A revenue", query="SELECT 1",
        )
        customer = Customer.objects.create(
            first_name="A", last_name="B", email="rp@example.com",
            company=self.company,
        )
        self.order = Order.objects.create(customer=customer, company=self.company)
        self.factory = APIRequestFactory()
        self.list_view = ReportDefinitionViewSet.as_view({"get": "list"})
        self.detail_view = ReportDefinitionViewSet.as_view({"get": "retrieve"})

    def _list(self, user):
        request = self.factory.get("/api/v1/reporting/reports/")
        force_authenticate(request, user=user)
        return self.list_view(request)

    def test_gap_report_list_shows_all(self):
        other_user = company_user(self.other, username="rival", uid="uid-rival")
        response = self._list(other_user)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)

    def test_gap_report_pk_guessable_cross_company(self):
        other_user = company_user(self.other, username="rival2", uid="uid-rival2")
        request = self.factory.get(
            f"/api/v1/reporting/reports/{self.report.pk}/"
        )
        force_authenticate(request, user=other_user)
        response = self.detail_view(request, pk=self.report.pk)
        self.assertEqual(response.status_code, 200)


def session_staff(company, username):
    """A back-office login whose session resolves to ``company`` (AD-12)."""
    user = make_staff_user(username=username, password="pw")
    if company is not None:
        UserProfile.objects.create(
            user=user, company=company
        )
    return user


def card_values(response):
    return {c["label"]: c["value"] for c in response.context["summary_cards"]}


class ReportListViewTests(TestCase):
    """Every number on /erp/reports/, against won/lost/open quotes, revision
    chains and a second tenant whose data must never show up."""

    def setUp(self):
        self.company = Company.objects.create(name="Acme Ltd")
        self.other = Company.objects.create(name="Rival Ltd")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Acme", email="ada@example.com",
            company=self.company,
        )
        self.rival_customer = Customer.objects.create(
            first_name="Rex", last_name="Rival", email="rex@example.com",
            company=self.other,
        )
        self.user = session_staff(self.company, "acme")
        self._quotes(self.company, self.customer)
        self._rival_quotes()
        self._backfill()

    _numbers = itertools.count(1)

    def _quote(self, company, customer, *, sales_stage=None, **fields):
        """A pre-engagement quote row, as the CRM Phase 3 step-5 backfill
        finds it (``bulk_create``: no ``save()``, so no engagement yet)."""
        if sales_stage is not None:
            fields["legacy_sales_stage"] = sales_stage
        quote = Quote(company=company, customer=customer,
                      quote_number=f"QT-T{next(self._numbers):05d}", **fields)
        Quote.objects.bulk_create([quote])
        return quote

    @staticmethod
    def _backfill():
        backfill_quote_engagements()

    def _quotes(self, company, customer):
        S, ST = Quote.Status, Quote.SalesStage
        # Chain 1: three revisions linked both ways; the head was converted to
        # an order while its stage was never moved to Closed Won.
        v1 = self._quote(company, customer, status=S.SUPERSEDED)
        v2 = self._quote(company, customer, status=S.SUPERSEDED,
                         original_quote=v1, revision_number=2)
        order = Order.objects.create(customer=customer, company=company)
        v3 = self._quote(company, customer, status=S.ACCEPTED, original_quote=v1,
                         revision_number=3, sales_stage=ST.PROPOSAL_SENT,
                         converted_to_order=order)
        Quote.objects.filter(pk=v1.pk).update(superseded_by=v2)
        Quote.objects.filter(pk=v2.pk).update(superseded_by=v3)
        # Chain 2: linked only by original_quote, statuses never updated.
        c1 = self._quote(company, customer, status=S.SENT)
        self._quote(company, customer, status=S.SENT, original_quote=c1,
                    revision_number=2)
        # Chain 3: linked only by superseded_by; old row's status not updated.
        d2 = self._quote(company, customer, status=S.LOST)
        self._quote(company, customer, status=S.SENT, superseded_by=d2)
        # Singles.
        self._quote(company, customer, status=S.ACCEPTED, sales_stage=ST.CLOSED_WON)  # won
        self._quote(company, customer, status=S.LOST, sales_stage=ST.NEGOTIATION)     # lost
        self._quote(company, customer, status=S.EXPIRED)                              # lost
        self._quote(company, customer, status=S.SENT, sales_stage=ST.CLOSED_LOST)     # lost
        self._quote(company, customer, status=S.SENT)                                 # open
        self._quote(company, customer, status=S.DRAFT)                                # open
        self._quote(company, customer, status=S.CANCELLED)                            # cancelled

    def _rival_quotes(self):
        for _ in range(3):
            self._quote(self.other, self.rival_customer,
                        sales_stage=Quote.SalesStage.CLOSED_WON)

    def _get(self, user, report, **params):
        self.client.force_login(user)
        return self.client.get(
            reverse("erp-report-list"), {"report": report, "period": "month", **params}
        )

    # ── quote conversion ────────────────────────────────────

    def test_quote_conversion_numbers(self):
        response = self._get(self.user, "quote_conversion")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(card_values(response), {
            "Quotes": 10,  # 14 rows; the chains collapse 3->1, 2->1, 2->1
            "Won": 2,      # converted head + Closed Won
            "Lost": 4,     # chain-3 head, status expired, stage Closed Lost, status lost
            "Open": 3,     # sent, draft, chain-2 head
            "Win Rate": "33%",  # 2 / (2 + 4); open and cancelled excluded
        })

    def test_converted_quote_counts_once_as_won(self):
        response = self._get(self.user, "quote_conversion")
        stages = dict(response.context["table_data"])
        # The converted head is still at Proposal sent (closing keeps the
        # stage), yet is counted Won.
        self.assertEqual(stages["Proposal sent"], 1)
        # Closed Won / Closed Lost singles close with no stage; the untouched
        # draft has no engagement yet.
        self.assertEqual(stages["No stage"], 3)
        self.assertEqual(sum(stages.values()), 10)

    def test_chain_is_dated_by_its_original_quote(self):
        from datetime import timedelta

        from django.utils import timezone

        long_ago = timezone.now() - timedelta(days=400)
        root = self._quote(self.company, self.customer, status=Quote.Status.SUPERSEDED)
        self._quote(self.company, self.customer, status=Quote.Status.ACCEPTED,
                    original_quote=root, revision_number=2,
                    sales_stage=Quote.SalesStage.CLOSED_WON)
        Quote.objects.filter(pk=root.pk).update(created_at=long_ago)
        self._backfill()
        cards = card_values(self._get(self.user, "quote_conversion"))
        self.assertEqual((cards["Quotes"], cards["Won"]), (10, 2))  # not this period
        # The report takes dates in the site time zone (America/New_York); the
        # UTC date differs from the local one between 00:00 and 04:00 UTC, which
        # made this fail late at night. Use the local date.
        day = timezone.localtime(long_ago).date().isoformat()
        cards = card_values(self._get(
            self.user, "quote_conversion", date_from=day, date_to=day,
        ))
        self.assertEqual((cards["Quotes"], cards["Won"]), (1, 1))

    def test_win_rate_blank_when_nothing_decided(self):
        Quote.objects.filter(company=self.company).delete()
        self._quote(self.company, self.customer, status=Quote.Status.SENT)
        self._backfill()
        cards = card_values(self._get(self.user, "quote_conversion"))
        self.assertEqual((cards["Quotes"], cards["Open"], cards["Win Rate"]), (1, 1, "—"))

    def test_rival_tenant_sees_only_its_own_quotes(self):
        rival = session_staff(self.other, "rival")
        cards = card_values(self._get(rival, "quote_conversion"))
        self.assertEqual((cards["Quotes"], cards["Won"], cards["Lost"]), (3, 3, 0))
        self.assertEqual(cards["Win Rate"], "100%")

    def test_superuser_sees_every_company(self):
        from django.contrib.auth.models import User

        root = User.objects.create_superuser("root", "root@example.com", "pw")
        response = self._get(root, "quote_conversion")
        self.assertEqual(card_values(response)["Quotes"], 13)
        self.assertContains(response, "All companies (superuser view)")

    def test_login_without_company_sees_nothing(self):
        orphan = session_staff(None, "orphan")
        response = self._get(orphan, "quote_conversion")
        self.assertEqual(card_values(response)["Quotes"], 0)
        self.assertContains(response, "No company linked to this login")

    def test_definitions_are_on_the_page(self):
        response = self._get(self.user, "quote_conversion")
        self.assertContains(response, "How these numbers are calculated")
        self.assertContains(response, "Won / (Won + Lost)")
        self.assertContains(response, "Data for: Acme Ltd")

    # ── the four order/stock reports ────────────────────────

    def test_order_reports_are_tenant_scoped(self):
        Order.objects.create(
            customer=self.rival_customer, company=self.other,
            grand_total=Decimal("999.00"),
        )
        response = self._get(self.user, "sales_summary")
        self.assertEqual(card_values(response)["Total Orders"], 1)  # chain-1 order
        self.assertNotContains(response, "Rex Rival")

        response = self._get(self.user, "order_status")
        self.assertEqual(card_values(response)["Total"], 1)

    def test_accounts_receivable_matches_stored_payment_status(self):
        # payment_status is stored lowercase; the report used to filter on
        # "UNPAID"/"PARTIAL" and so always showed nothing.
        Order.objects.create(
            customer=self.customer, company=self.company,
            status=Order.Status.SHIPPED, payment_status=Order.PaymentStatus.UNPAID,
            grand_total=Decimal("120.00"), balance_due=Decimal("120.00"),
        )
        Order.objects.create(
            customer=self.customer, company=self.company,
            status=Order.Status.PARTIALLY_SHIPPED,
            payment_status=Order.PaymentStatus.PARTIAL,
            grand_total=Decimal("80.00"), balance_due=Decimal("30.00"),
        )
        Order.objects.create(
            customer=self.rival_customer, company=self.other,
            status=Order.Status.SHIPPED, payment_status=Order.PaymentStatus.UNPAID,
            grand_total=Decimal("500.00"), balance_due=Decimal("500.00"),
        )
        cards = card_values(self._get(self.user, "accounts_receivable"))
        self.assertEqual(cards["Unpaid Orders"], 2)
        self.assertEqual(cards["Outstanding Balance"], "$150.00")

    def test_accounts_receivable_totals_are_not_capped_by_the_table(self):
        for _ in range(55):
            Order.objects.create(
                customer=self.customer, company=self.company,
                status=Order.Status.SHIPPED,
                payment_status=Order.PaymentStatus.UNPAID,
                grand_total=Decimal("2.00"), balance_due=Decimal("2.00"),
            )
        response = self._get(self.user, "accounts_receivable")
        cards = card_values(response)
        self.assertEqual(cards["Unpaid Orders"], 55)
        self.assertEqual(cards["Outstanding Balance"], "$110.00")
        self.assertEqual(len(response.context["table_data"]), 50)

    def test_inventory_is_scoped_through_warehouse(self):
        from inventory.models import Warehouse, WarehouseStock
        from products.models import Product, ProductVariant

        for company, code in ((self.company, "A"), (self.other, "B")):
            warehouse = Warehouse.objects.create(
                warehouse_code=f"WH-{code}", name=f"WH {code}", company=company,
            )
            product = Product.objects.create(
                title=f"Panel {code}", part_number=f"P-{code}",
                slug=f"p-{code.lower()}", company=company,
            )
            variant = ProductVariant.objects.create(product=product, sku=f"SKU-{code}")
            WarehouseStock.objects.create(
                warehouse=warehouse, variant=variant, quantity=Decimal("1"),
            )
        response = self._get(self.user, "inventory_levels")
        self.assertEqual(card_values(response)["Total SKUs Tracked"], 1)
        self.assertContains(response, "SKU-A")
        self.assertNotContains(response, "SKU-B")
