"""``manage.py check_numbering`` (docs/release-hardening-plan.md 7.3)."""

from __future__ import annotations

import datetime as dt
import io
import json
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.integrity import NumberedSpec, _numbering_for
from core.management import release_exit_codes as codes
from core.models import Company
from crm import engagements
from crm.models import Engagement, Pipeline
from crm.tests.helpers import organisation
from customers.models import Customer
from orders.models import Order


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        call_command("check_numbering", *args, stdout=out, stderr=err)
    except SystemExit as exc:
        code = exc.code
    return code, out.getvalue()


def rows_for(out, doc_type):
    return [r for r in json.loads(out)["rows"] if r["doc_type"] == doc_type]


class NumberingFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.customer = Customer.objects.create(
            first_name="A", last_name="B", email="numbers@example.com", company=cls.company,
        )
        cls.orders = [
            Order.objects.create(customer=cls.customer, company=cls.company) for _ in range(3)
        ]

    def engagement(self):
        pipeline = Pipeline.objects.get(company=self.company, key="sales")
        return engagements.create(
            company=self.company, party=organisation(self.company),
            stage=pipeline.stages.get(key="new"),
        )


class CleanTests(NumberingFixture):
    def test_fresh_numbers_exit_0(self):
        code, out = run()
        self.assertEqual(code, codes.OK, out)
        self.assertIn("STATUS: OK (exit 0)", out)
        self.assertIn("order", out)

    def test_empty_database(self):
        Order.objects.all().delete()
        code, out = run()
        self.assertEqual(code, codes.OK)
        self.assertIn("no numbered documents", out)

    def test_json_rows(self):
        code, out = run("--json")
        self.assertEqual(code, 0)
        [row] = rows_for(out, "order")
        self.assertEqual(row["rows"], 3)
        self.assertEqual((row["duplicates"], row["order_violations"], row["blank"]), (0, 0, 0))
        self.assertEqual(row["highest"] - row["lowest"], 2)
        self.assertEqual(row["gaps_missing"], 0)


class DuplicateTests(NumberingFixture):
    """Every number column carries a UNIQUE constraint today, so a duplicate
    can only come from a restore that lost one. The command is exercised over
    a free-text column standing in for the number field."""

    PO_SPEC = [NumberedSpec("order", Order, "customer_po_number", "SO")]

    def test_duplicate_numbers_roll_back(self):
        for order, number in zip(self.orders, ("SO-00001", "SO-00001", "SO-00002")):
            Order.objects.filter(pk=order.pk).update(customer_po_number=number)
        with mock.patch("core.integrity.numbered_specs", return_value=self.PO_SPEC):
            code, out = run()
            self.assertEqual(code, codes.ROLL_BACK, out)
            self.assertIn(f"order ({self.company.pk}) duplicate: SO-00001", out)
            self.assertIn("STATUS: ROLL BACK (exit 20)", out)
            [row] = rows_for(run("--json")[1], "order")
        self.assertEqual(row["duplicates"], 1)
        self.assertEqual(row["duplicate_samples"], ["SO-00001"])
        self.assertEqual(row["status"], "ROLL BACK")

    def test_the_same_number_in_two_companies_is_not_a_duplicate(self):
        other = Company.objects.create(name="Tenant B")
        theirs = Customer.objects.create(
            first_name="C", last_name="D", email="other@example.com", company=other,
        )
        Order.objects.create(customer=theirs, company=other)
        Order.objects.update(customer_po_number="SO-00001")
        with mock.patch("core.integrity.numbered_specs", return_value=self.PO_SPEC):
            code, out = run("--json")
        rows = rows_for(out, "order")
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["duplicates"] for r in sorted(rows, key=lambda r: r["rows"])], [0, 1])
        self.assertEqual(code, codes.ROLL_BACK)

    def test_engagements_are_grouped_per_company(self):
        other = Company.objects.create(name="Tenant B")
        self.engagement()
        engagements.create(
            company=other, party=organisation(other, "Other Org"),
            stage=Pipeline.objects.get(company=other, key="sales").stages.get(key="new"),
        )
        code, out = run("--json")
        self.assertEqual(code, codes.OK)
        self.assertEqual({r["company"] for r in rows_for(out, "engagement")},
                         {str(self.company.pk), str(other.pk)})

    def test_blank_number_is_degraded(self):
        Engagement.objects.filter(pk=self.engagement().pk).update(number="")
        code, out = run("--json")
        self.assertEqual(code, codes.DEGRADED)
        [row] = rows_for(out, "engagement")
        self.assertEqual(row["blank"], 1)


class OrderAndGapTests(NumberingFixture):
    def test_out_of_order_numbers_are_degraded(self):
        a, b = self.orders[0], self.orders[1]
        swap = a.order_number, b.order_number
        Order.objects.filter(pk=a.pk).update(order_number="SO-TMP")
        Order.objects.filter(pk=b.pk).update(order_number=swap[0])
        Order.objects.filter(pk=a.pk).update(order_number=swap[1])
        code, out = run("--json")
        self.assertEqual(code, codes.DEGRADED, out)
        [row] = rows_for(out, "order")
        self.assertEqual(row["order_violations"], 1)
        self.assertEqual(row["order_violation_samples"], [swap[0]])
        self.assertEqual(row["duplicates"], 0)

    def test_gaps_are_a_statistic_not_a_failure(self):
        Order.objects.filter(pk=self.orders[2].pk).update(order_number="SO-00050")
        code, out = run("--json")
        self.assertEqual(code, codes.OK, out)
        [row] = rows_for(out, "order")
        low = row["lowest"]
        self.assertEqual(row["highest"], 50)
        self.assertEqual(row["gaps_missing"], 50 - low + 1 - 3)
        self.assertEqual(row["gaps_runs"], 1)
        self.assertEqual(row["gaps_largest"], 50 - (low + 1) - 1)

    def test_non_sequence_numbers_are_counted_and_skipped(self):
        Order.objects.filter(pk=self.orders[2].pk).update(order_number="SO-BC777")
        code, out = run("--json")
        self.assertEqual(code, codes.OK)
        [row] = rows_for(out, "order")
        self.assertEqual(row["non_sequence"], 1)
        self.assertEqual(row["non_sequence_samples"], ["SO-BC777"])
        self.assertEqual(row["highest"] - row["lowest"], 1)


class PureStatisticsTests(TestCase):
    """``_numbering_for`` on hand-built rows: the arithmetic without a database."""

    spec = NumberedSpec("order", Order, "order_number", "SO")

    def at(self, minute):
        return timezone.make_aware(dt.datetime(2026, 1, 1, 0, minute))

    def test_ties_on_created_at_are_not_inversions(self):
        values = [("SO-00002", self.at(0), 2), ("SO-00001", self.at(0), 1)]
        row = _numbering_for("c", self.spec, values, samples=5)
        self.assertEqual(row.order_violations, 0)

    def test_inversion_counts_each_lower_later_row(self):
        values = [("SO-00005", self.at(0), 5), ("SO-00003", self.at(1), 3),
                  ("SO-00004", self.at(2), 4), ("SO-00006", self.at(3), 6)]
        row = _numbering_for("c", self.spec, values, samples=1)
        self.assertEqual(row.order_violations, 2)
        self.assertEqual(row.order_violation_samples, ["SO-00003"])

    def test_gap_statistics(self):
        values = [("SO-00001", self.at(0), 1), ("SO-00004", self.at(1), 4),
                  ("SO-00005", self.at(2), 5), ("SO-00009", self.at(3), 9)]
        row = _numbering_for("c", self.spec, values, samples=5)
        self.assertEqual((row.lowest, row.highest), (1, 9))
        self.assertEqual(row.gaps_missing, 5)
        self.assertEqual(row.gaps_runs, 2)
        self.assertEqual(row.gaps_largest, 3)
        self.assertEqual(row.status, "OK")

    def test_width_does_not_matter(self):
        values = [("SO-1", self.at(0), 1), ("SO-00002", self.at(1), 2)]
        row = _numbering_for("c", self.spec, values, samples=5)
        self.assertEqual((row.non_sequence, row.order_violations, row.gaps_missing), (0, 0, 0))
