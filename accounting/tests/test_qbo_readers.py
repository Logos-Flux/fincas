"""Parsing real-shaped QBO exports: preambles, header drift, money, dates.

Fixtures are written as CSV at runtime rather than committed as binary .xlsx,
with one xlsx round-trip to prove both paths agree.
"""

import datetime
import tempfile
from decimal import Decimal
from pathlib import Path

from django.test import TestCase

from accounting.qbo.readers import (
    QBOFormatError,
    parse_bool,
    parse_date,
    parse_decimal,
    read_table,
)


class _TempFileMixin:
    def write(self, name, content):
        path = Path(self.tmp.name) / name
        path.write_text(content, encoding="utf-8")
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)


class ReadTableTest(_TempFileMixin, TestCase):
    def test_reads_a_plain_csv(self):
        path = self.write(
            "coa.csv",
            "Account Number,Account Name,Type,Detail Type\n"
            "1000,Checking,Bank,Checking\n"
            "4000,Sales,Income,Sales of Product Income\n",
        )
        rows = list(read_table(path))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["account_number"], "1000")
        self.assertEqual(rows[0]["account_name"], "Checking")
        self.assertEqual(rows[1]["account_type"], "Income")

    def test_skips_the_report_preamble(self):
        # QBO report exports lead with company name, report title, date range
        # and a blank line before the real header.
        path = self.write(
            "journal.csv",
            "Example Supply Inc\n"
            "Journal\n"
            "January 1 - December 31 2025\n"
            "\n"
            "Date,Transaction Type,Num,Name,Memo/Description,Account,Debit,Credit\n"
            "01/15/2025,Invoice,1001,Acme,Widget sale,Accounts Receivable,108.25,\n"
            "01/15/2025,Invoice,1001,Acme,Widget sale,Sales,,100.00\n",
        )
        rows = list(read_table(path))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["doc_number"], "1001")
        self.assertEqual(rows[0]["account_name"], "Accounts Receivable")

    def test_header_aliases_resolve_to_one_field(self):
        for header in ("Account Number", "Account No", "Account #", "Number"):
            path = self.write(
                f"{header.replace(' ', '_').replace('#', 'hash')}.csv",
                f"{header},Account Name,Type\n1000,Checking,Bank\n",
            )
            rows = list(read_table(path))
            self.assertEqual(rows[0]["account_number"], "1000", header)

    def test_blank_and_spacer_rows_are_dropped(self):
        path = self.write(
            "spaced.csv",
            "Account Name,Type\n"
            "Checking,Bank\n"
            ",\n"
            "\n"
            "Sales,Income\n",
        )
        rows = list(read_table(path))
        self.assertEqual([r["account_name"] for r in rows], ["Checking", "Sales"])

    def test_missing_header_is_an_explicit_error(self):
        path = self.write("junk.csv", "just\nsome\nprose\n")
        with self.assertRaises(QBOFormatError):
            list(read_table(path))

    def test_unsupported_extension_is_rejected(self):
        path = self.write("data.txt", "Account Name,Type\nChecking,Bank\n")
        with self.assertRaises(QBOFormatError):
            list(read_table(path))

    def test_short_rows_are_padded_not_dropped(self):
        path = self.write(
            "short.csv",
            "Date,Transaction Type,Num,Account,Debit,Credit\n"
            "01/15/2025,Invoice,1001,Sales,100.00\n",
        )
        rows = list(read_table(path))
        self.assertEqual(rows[0]["debit"], "100.00")
        self.assertEqual(rows[0].get("credit", ""), "")

    def test_duplicate_headers_do_not_shadow_each_other(self):
        path = self.write(
            "dupes.csv", "Account,Amount,Amount\nChecking,10.00,20.00\n"
        )
        rows = list(read_table(path))
        self.assertEqual(set(rows[0].raw.values()), {"Checking", "10.00", "20.00"})

    def test_currency_qualified_money_headers_resolve(self):
        """"Debit (USD)" is the money column, not an unknown column.

        Report exports label the currency; "Export Data" exports do not. When
        the qualified spelling did not resolve, every leg read 0.00 and each
        row was dropped as a zero-amount line — the file parsed, the dates were
        fine, and the import committed nothing while reporting no money error.
        """
        path = self.write(
            "je.csv",
            "Date,Num,Account,Debit (USD),Credit (USD)\n"
            "2025-12-31,JE-X1,General business expenses,11000.00,\n"
            "2025-12-31,JE-X1,Operating Checking (0001),,11000.00\n",
        )
        rows = list(read_table(path, context="transactions"))
        self.assertEqual(
            [(r["debit"], r["credit"]) for r in rows],
            [("11000.00", ""), ("", "11000.00")],
        )

    def test_an_exact_header_is_not_shadowed_by_a_currency_qualified_one(self):
        """A file carrying both keeps them as two distinct columns."""
        path = self.write(
            "both.csv", "Account,Debit,Debit (USD)\nChecking,1.00,2.00\n"
        )
        row = next(iter(read_table(path)))
        self.assertEqual(row["debit"], "1.00")

    def test_currency_qualified_headers_alone_identify_the_header_row(self):
        """Two qualified money columns are enough to find the header."""
        path = self.write(
            "tb.csv",
            "Some Report Title\n"
            "Account,Debit (USD),Credit (USD)\n"
            "Checking,957.86,0.00\n",
        )
        rows = list(read_table(path))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["account_name"], "Checking")
        self.assertEqual(rows[0]["debit"], "957.86")

    def test_currency_stripping_does_not_touch_other_columns(self):
        path = self.write(
            "coa.csv", "Account Number,Account Name\n1000,Checking\n"
        )
        row = next(iter(read_table(path, context="coa")))
        self.assertEqual(row["account_number"], "1000")
        self.assertEqual(row["account_name"], "Checking")

    def test_has_reports_whether_a_column_existed(self):
        path = self.write("cols.csv", "Account Name,Type\nChecking,Bank\n")
        row = next(iter(read_table(path)))
        self.assertTrue(row.has("account_name"))
        self.assertFalse(row.has("debit"))

    def test_xlsx_and_csv_agree(self):
        import openpyxl

        rows_data = [
            ["Account Number", "Account Name", "Type", "Detail Type"],
            ["1000", "Checking", "Bank", "Checking"],
            ["4000", "Sales", "Income", "Sales of Product Income"],
        ]
        xlsx_path = Path(self.tmp.name) / "coa.xlsx"
        workbook = openpyxl.Workbook()
        for row in rows_data:
            workbook.active.append(row)
        workbook.save(xlsx_path)

        csv_path = self.write(
            "coa.csv", "\n".join(",".join(r) for r in rows_data) + "\n"
        )

        from_xlsx = [(r["account_number"], r["account_name"], r["account_type"])
                     for r in read_table(xlsx_path)]
        from_csv = [(r["account_number"], r["account_name"], r["account_type"])
                    for r in read_table(csv_path)]
        self.assertEqual(from_xlsx, from_csv)
        self.assertEqual(len(from_xlsx), 2)


class ParseDecimalTest(TestCase):
    def test_plain_numbers(self):
        self.assertEqual(parse_decimal("100.00"), Decimal("100.00"))
        self.assertEqual(parse_decimal(100), Decimal("100"))
        self.assertEqual(parse_decimal(100.5), Decimal("100.5"))

    def test_currency_symbols_and_separators(self):
        self.assertEqual(parse_decimal("$1,234.56"), Decimal("1234.56"))
        self.assertEqual(parse_decimal("1,234,567.89"), Decimal("1234567.89"))

    def test_parentheses_mean_negative(self):
        # Accounting convention. Read as positive, a reversal doubles the
        # balance instead of cancelling it.
        self.assertEqual(parse_decimal("(1,234.56)"), Decimal("-1234.56"))
        self.assertEqual(parse_decimal("($500.00)"), Decimal("-500.00"))

    def test_explicit_minus(self):
        self.assertEqual(parse_decimal("-42.00"), Decimal("-42.00"))

    def test_blank_becomes_the_default(self):
        self.assertEqual(parse_decimal(""), Decimal("0.00"))
        self.assertEqual(parse_decimal(None), Decimal("0.00"))
        self.assertEqual(parse_decimal("", Decimal("7")), Decimal("7"))

    def test_digitless_placeholders_are_treated_as_absent(self):
        # QBO pads empty money cells with these; they mean "nothing here",
        # not "malformed".
        for placeholder in ("n/a", "—", "-", "", "   "):
            self.assertEqual(parse_decimal(placeholder), Decimal("0.00"), placeholder)

    def test_a_cell_with_digits_that_is_not_a_number_raises(self):
        # The dangerous case: a permissive strip would read "12x34" as 1234 and
        # post it to the ledger.
        for garbage in ("12x34", "1.2.3", "100-", "1,2,3.4.5", "12 34 abc"):
            with self.assertRaises(QBOFormatError, msg=garbage):
                parse_decimal(garbage)


class ParseDateTest(TestCase):
    def test_iso_is_never_reinterpreted(self):
        self.assertEqual(parse_date("2025-03-04"), datetime.date(2025, 3, 4))
        self.assertEqual(
            parse_date("2025-03-04", dayfirst=True), datetime.date(2025, 3, 4)
        )

    def test_us_slash_format_by_default(self):
        self.assertEqual(parse_date("03/04/2025"), datetime.date(2025, 3, 4))

    def test_dayfirst_flips_the_ambiguous_reading(self):
        # The exact silent-corruption case: same string, different date.
        self.assertEqual(
            parse_date("03/04/2025", dayfirst=True), datetime.date(2025, 4, 3)
        )

    def test_spelled_out_months(self):
        self.assertEqual(parse_date("15 Jan 2025"), datetime.date(2025, 1, 15))
        self.assertEqual(parse_date("January 15, 2025"), datetime.date(2025, 1, 15))

    def test_datetime_and_date_pass_through(self):
        self.assertEqual(
            parse_date(datetime.datetime(2025, 1, 15, 10, 30)),
            datetime.date(2025, 1, 15),
        )
        self.assertEqual(parse_date(datetime.date(2025, 1, 15)), datetime.date(2025, 1, 15))

    def test_blank_is_none(self):
        self.assertIsNone(parse_date(""))
        self.assertIsNone(parse_date(None))

    def test_unparseable_raises(self):
        with self.assertRaises(QBOFormatError):
            parse_date("sometime last spring")


class ParseBoolTest(TestCase):
    def test_truthy_spellings(self):
        for value in ("Yes", "y", "TRUE", "1", "Active", "enabled"):
            self.assertTrue(parse_bool(value), value)

    def test_falsy_spellings(self):
        for value in ("No", "n", "FALSE", "0", "Inactive", "deleted"):
            self.assertFalse(parse_bool(value), value)

    def test_blank_uses_the_default(self):
        self.assertTrue(parse_bool(""))
        self.assertFalse(parse_bool("", default=False))

    def test_unrecognised_uses_the_default(self):
        self.assertTrue(parse_bool("maybe"))
        self.assertFalse(parse_bool("maybe", default=False))


class ContextualAliasTest(_TempFileMixin, TestCase):
    """Bare "Name" means different things in different QBO exports.

    In an account-list export it is the account. In a journal export it is the
    customer/vendor. Reading it the same way in both silently files every
    transaction against an account named after a customer — no error, no
    warning, a corrupt ledger. These pin the disambiguation.
    """

    JOURNAL_NAME_ONLY = (
        "Date,Transaction Type,Num,Name,Debit,Credit\n"
        "01/15/2025,Invoice,1001,Acme Solar,100.00,\n"
    )

    def test_name_is_the_account_in_a_coa_export(self):
        path = self.write("coa.csv", "Name,Type,Detail Type\nChecking,Bank,Checking\n")
        row = next(iter(read_table(path, context="coa")))
        self.assertEqual(row["account_name"], "Checking")

    def test_name_is_the_entity_not_the_account_in_a_journal_export(self):
        path = self.write("journal.csv", self.JOURNAL_NAME_ONLY)
        row = next(iter(read_table(path, context="transactions")))
        self.assertEqual(row["entity_name"], "Acme Solar")
        # The critical assertion: the customer must NOT be read as the account.
        self.assertEqual(row["account_name"], "")

    def test_an_explicit_account_column_still_wins_over_name(self):
        path = self.write(
            "both.csv",
            "Date,Transaction Type,Num,Name,Account,Debit,Credit\n"
            "01/15/2025,Invoice,1001,Acme Solar,Accounts Receivable,100.00,\n",
        )
        row = next(iter(read_table(path, context="transactions")))
        self.assertEqual(row["account_name"], "Accounts Receivable")
        self.assertEqual(row["entity_name"], "Acme Solar")

    def test_a_name_only_header_is_still_recognised_as_a_header(self):
        # _KNOWN_HEADERS must span every context, or a header row spelled only
        # with context-specific names is skipped and the file looks headerless.
        path = self.write("coa.csv", "Name,Type\nChecking,Bank\n")
        self.assertEqual(len(list(read_table(path, context="coa"))), 1)

    def test_unknown_context_is_rejected(self):
        path = self.write("coa.csv", "Name,Type\nChecking,Bank\n")
        with self.assertRaises(ValueError):
            list(read_table(path, context="nonsense"))


class EuropeanNumberFormatTest(TestCase):
    """"1.234,56" is 1234.56 in a European export.

    Stripping commas blindly turns it into 1.23456, which then quantizes to
    1.23 — a ~1000x understatement, silent, on a ledger. The module already
    ships `--dayfirst` for non-US exports, so this input is reachable.
    """

    def test_european_decimal_comma_is_refused_not_mis_parsed(self):
        for value in ("1.234,56", "€1.234,56", "(1.234,56)"):
            with self.assertRaises(QBOFormatError, msg=value):
                parse_decimal(value)

    def test_us_format_with_both_separators_still_parses(self):
        self.assertEqual(parse_decimal("1,234.56"), Decimal("1234.56"))
        self.assertEqual(parse_decimal("1,234,567.89"), Decimal("1234567.89"))

    def test_a_bare_comma_thousands_separator_still_parses(self):
        self.assertEqual(parse_decimal("1,234"), Decimal("1234"))
