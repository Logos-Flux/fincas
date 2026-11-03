"""Line-item entry must not block on things the user never had to supply.

Three related defects, all surfacing as "This field is required." on a
quote the user could not save:

1. **The phantom blank row.** `extra=1` renders one empty row under the
   line items table, and Django only validates an extra form whose
   has_changed() is True. renumberAllGroups() in erp/document_form.html
   stamps every row's sg_index with its shipment-group index, so on a
   document with 2+ groups that rewrite (0 -> 1) flipped has_changed() and
   made Django demand variant/sku/item_name/quantity/unit_price on a row
   nobody touched. Unsatisfiable, because __init__ pins an unpicked row's
   variant queryset to .none(). Fixed by ShipmentGroupIndexMixin.

2. **Freeform "misc" lines were impossible on quotes.** QuoteLineItem
   .variant was NOT NULL while OrderLineItem.variant was already nullable,
   so a non-catalog line could go on a sales order but not on a quote.

3. **A blank discount was an error.** discount_percent and
   line_total_ex_tax carry default=0 but lacked blank=True, so Django built
   them as required. Blank now means zero.

These tests post exactly what the unbound widgets render, so they exercise
the real browser round-trip rather than hand-written payloads -- a
hand-written blank payload makes quantity/discount_percent look changed
when the real page never sends those values, which produces false results.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from core.models import Company
from customers.models import Customer
from orders.forms import OrderLineItemFormSet, QuoteLineItemFormSet
from orders.models import Quote, QuoteLineItem

PREFIX = "line_items"


def render_blank_row(formset_cls):
    """Harvest the exact values the GET page puts on screen for the blank row."""
    form = formset_cls(prefix=PREFIX).forms[0]
    rendered = {}
    for name in form.fields:
        value = form[name].value()
        rendered[name] = "" if value is None else str(value)
    return rendered


def post_data(rendered, **overrides):
    data = {
        f"{PREFIX}-TOTAL_FORMS": "1",
        f"{PREFIX}-INITIAL_FORMS": "0",
        f"{PREFIX}-MIN_NUM_FORMS": "0",
        f"{PREFIX}-MAX_NUM_FORMS": "1000",
    }
    for name, value in rendered.items():
        data[f"{PREFIX}-0-{name}"] = value
    for name, value in overrides.items():
        data[f"{PREFIX}-0-{name}"] = value
    return data


class BlankExtraRowTests(TestCase):
    """One case per formset, driven off the same rendered blank row."""

    formsets = {"quote": QuoteLineItemFormSet, "order": OrderLineItemFormSet}

    def test_untouched_blank_row_in_first_group_validates(self):
        # Baseline: this already worked. Guards against a regression that
        # would break every single-shipment-group document.
        for label, cls in self.formsets.items():
            with self.subTest(formset=label):
                fs = cls(post_data(render_blank_row(cls)), prefix=PREFIX)
                self.assertTrue(fs.is_valid(), fs.errors)

    def test_untouched_blank_row_in_later_group_validates(self):
        # The actual bug: sg_index rewritten by renumberAllGroups().
        for label, cls in self.formsets.items():
            for sg_index in ("1", "2", "7"):
                with self.subTest(formset=label, sg_index=sg_index):
                    fs = cls(
                        post_data(render_blank_row(cls), sg_index=sg_index),
                        prefix=PREFIX,
                    )
                    self.assertTrue(
                        fs.is_valid(),
                        f"blank row in shipment group {sg_index} should be "
                        f"ignored, got: {fs.errors}",
                    )

    def test_sg_index_alone_never_marks_a_row_as_touched(self):
        for label, cls in self.formsets.items():
            with self.subTest(formset=label):
                fs = cls(
                    post_data(render_blank_row(cls), sg_index="3"), prefix=PREFIX
                )
                fs.is_valid()
                form = fs.forms[0]
                # changed_data still reports the raw truth ...
                self.assertIn("sg_index", form.changed_data)
                # ... but it must not count as user intent.
                self.assertFalse(form.has_changed())


class HalfFilledRowStillErrorsTests(TestCase):
    """The fix must not silence rows a human actually started filling in.

    Suppressing sg_index is safe only while every other field still counts.
    If these ever pass silently, a partially-entered line would be dropped
    on save with no warning -- worse than the bug being fixed.
    """

    formsets = {"quote": QuoteLineItemFormSet, "order": OrderLineItemFormSet}

    def assert_row_rejected(self, cls, **overrides):
        fs = cls(post_data(render_blank_row(cls), **overrides), prefix=PREFIX)
        self.assertFalse(
            fs.is_valid(),
            f"a row carrying {overrides!r} is user-entered data and must "
            f"not be silently discarded",
        )
        return fs

    def test_a_single_entered_field_keeps_the_row_under_validation(self):
        # Whichever field the user touched, the row stays validated and the
        # ones they left blank are reported back to them.
        cases = {
            "item_name": "Panel, 400W",
            "sku": "SOL-400W",
            "unit_price_ex_tax": "129.99",
            "quantity": "4",
        }
        for label, cls in self.formsets.items():
            for field, value in cases.items():
                with self.subTest(formset=label, entered=field):
                    fs = self.assert_row_rejected(cls, **{field: value})
                    errors = fs.errors[0]
                    self.assertNotIn(field, errors)
                    # The fields left empty must be surfaced, not swallowed.
                    for other in set(cases) - {field}:
                        self.assertIn(other, errors)

    def test_half_filled_row_in_a_later_group_still_errors(self):
        # sg_index suppression must not rescue a row that has real data.
        for label, cls in self.formsets.items():
            for sg_index in ("1", "4"):
                with self.subTest(formset=label, sg_index=sg_index):
                    self.assert_row_rejected(
                        cls, item_name="Panel, 400W", sg_index=sg_index
                    )


class QuoteVariantErrorTests(TestCase):
    """The phantom-row message must stay gone.

    Quote and Order line items now agree: variant is optional on both, so a
    line can describe a freeform / non-catalog item. Before, QuoteLineItem
    .variant was NOT NULL while OrderLineItem.variant was not, which is why
    "variant: This field is required." was a quote-only symptom.
    """

    def test_phantom_blank_row_no_longer_demands_a_variant(self):
        fs = QuoteLineItemFormSet(
            post_data(render_blank_row(QuoteLineItemFormSet), sg_index="1"),
            prefix=PREFIX,
        )
        self.assertTrue(fs.is_valid())
        self.assertEqual(fs.errors, [{}])

    def test_a_started_row_still_reports_what_is_missing(self):
        fs = QuoteLineItemFormSet(
            post_data(
                render_blank_row(QuoteLineItemFormSet), item_name="Panel, 400W"
            ),
            prefix=PREFIX,
        )
        self.assertFalse(fs.is_valid())
        # variant is no longer among them — it is legitimately optional now.
        self.assertNotIn("variant", fs.errors[0])
        for field in ("sku", "quantity", "unit_price_ex_tax"):
            self.assertIn(field, fs.errors[0])


class FreeformLineTests(TestCase):
    """A non-catalog "misc" line must save without a product behind it.

    This is the whole point of making variant nullable: staff need to put a
    one-off charge on a quote without inventing a catalog SKU for it.
    """

    formsets = {"quote": QuoteLineItemFormSet, "order": OrderLineItemFormSet}

    #: Everything a freeform line legitimately needs, and nothing more.
    MISC_LINE = {
        "sku": "MISC",
        "item_name": "Custom fabrication, on-site",
        "quantity": "1",
        "unit_price_ex_tax": "450.00",
    }

    def test_freeform_line_validates_without_a_variant(self):
        for label, cls in self.formsets.items():
            with self.subTest(formset=label):
                fs = cls(
                    post_data(render_blank_row(cls), **self.MISC_LINE),
                    prefix=PREFIX,
                )
                self.assertTrue(
                    fs.is_valid(),
                    f"a complete freeform line should save, got: {fs.errors}",
                )
                self.assertIsNone(fs.forms[0].cleaned_data.get("variant"))

    def test_freeform_line_survives_a_later_shipment_group(self):
        for label, cls in self.formsets.items():
            with self.subTest(formset=label):
                fs = cls(
                    post_data(
                        render_blank_row(cls), sg_index="2", **self.MISC_LINE
                    ),
                    prefix=PREFIX,
                )
                self.assertTrue(fs.is_valid(), fs.errors)


class FreeformLinePersistsTests(TestCase):
    """Prove the DATABASE accepts a variant-less line, not just the form.

    The formset tests above never save. Making variant nullable is a schema
    change, so this is the one that would catch a missing or unapplied
    migration.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="freeform@example.com", company=self.company,
        )

    def test_quote_line_saves_with_no_variant(self):
        quote = Quote.objects.create(
            customer=self.customer, company=self.company,
        )
        line = QuoteLineItem.objects.create(
            quote=quote,
            variant=None,
            sku="MISC",
            item_name="Custom fabrication, on-site",
            quantity=Decimal("1"),
            unit_price_ex_tax=Decimal("450.0000"),
            line_total_ex_tax=Decimal("450.00"),
        )
        line.refresh_from_db()
        self.assertIsNone(line.variant_id)
        self.assertEqual(line.item_name, "Custom fabrication, on-site")

        # And the parent still totals correctly off a variant-less line.
        quote.refresh_from_db()
        self.assertEqual(quote.subtotal_ex_tax, Decimal("450.00"))

    def test_line_defaults_discount_to_zero_at_the_model_layer(self):
        quote = Quote.objects.create(
            customer=self.customer, company=self.company,
        )
        line = QuoteLineItem.objects.create(
            quote=quote, variant=None, sku="MISC", item_name="Thing",
            quantity=Decimal("1"), unit_price_ex_tax=Decimal("10.0000"),
        )
        line.refresh_from_db()
        self.assertEqual(line.discount_percent, Decimal("0.00"))


class BlankDiscountDefaultsToZeroTests(TestCase):
    """Leaving discount empty means no discount, not a validation error.

    discount_percent and line_total_ex_tax carry default=0 on the model but
    lacked blank=True, so Django built them as required form fields. An
    empty discount box — the overwhelmingly common case — was an error.
    line_total_ex_tax is worse: the page's JS computes it, so a user has no
    business typing it at all.
    """

    formsets = {"quote": QuoteLineItemFormSet, "order": OrderLineItemFormSet}

    COMPLETE_LINE = {
        "sku": "MISC",
        "item_name": "Custom fabrication",
        "quantity": "2",
        "unit_price_ex_tax": "100.00",
    }

    def test_blank_discount_and_line_total_are_accepted_as_zero(self):
        for label, cls in self.formsets.items():
            with self.subTest(formset=label):
                fs = cls(
                    post_data(
                        render_blank_row(cls),
                        discount_percent="",
                        line_total_ex_tax="",
                        **self.COMPLETE_LINE,
                    ),
                    prefix=PREFIX,
                )
                self.assertTrue(fs.is_valid(), fs.errors)
                cleaned = fs.forms[0].cleaned_data
                self.assertEqual(cleaned["discount_percent"], Decimal("0"))
                self.assertEqual(cleaned["line_total_ex_tax"], Decimal("0"))

    def test_an_entered_discount_is_still_respected(self):
        # Defaulting must not clobber a real value.
        for label, cls in self.formsets.items():
            with self.subTest(formset=label):
                fs = cls(
                    post_data(
                        render_blank_row(cls),
                        discount_percent="12.50",
                        **self.COMPLETE_LINE,
                    ),
                    prefix=PREFIX,
                )
                self.assertTrue(fs.is_valid(), fs.errors)
                self.assertEqual(
                    fs.forms[0].cleaned_data["discount_percent"],
                    Decimal("12.50"),
                )

    def test_quantity_and_price_are_still_required(self):
        # These have no model default. Silently zeroing them would price a
        # line at $0.00 because someone tabbed past the box.
        for label, cls in self.formsets.items():
            for field in ("quantity", "unit_price_ex_tax"):
                with self.subTest(formset=label, omitted=field):
                    line = dict(self.COMPLETE_LINE, **{field: ""})
                    fs = cls(
                        post_data(render_blank_row(cls), **line), prefix=PREFIX
                    )
                    self.assertFalse(fs.is_valid())
                    self.assertIn(field, fs.errors[0])
