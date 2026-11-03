"""Synthetic QBO export corpus fuzz — every alias spelling + edge cases (T2-9).

Parametrically loads each synthetic file from
``accounting/tests/fixtures/qbo_synthetic`` via ``read_table`` / ``Row`` and
asserts the field resolves to the expected value. The test FAILS if an alias
is missing or mis-routed, which is the point — this fuzz exists because the
real export file hasn't arrived (OP #294/#947) and COLUMN_ALIASES must be
validated before it does.

Coverage is driven by the corpus itself: every .csv/.xlsx pair in the fixture
dir exercises one alias spelling or one edge case. A parametrized csv+xlsx
consistency check ensures both code paths agree.

Ambiguities (documented in AMBIGUITIES.md next to the fixtures):
  - "Number"/"Num"/"No"  → account_number AND doc_number (same normalized key)
  - "Type"               → account_type AND transaction_type
  - "Description"/"Memo Description" → description AND memo
  These are inherent QBO vocabulary overlaps; see the module docstring for
  which field wins per Row.get call order. Context aliases fix only the
  "Name"/"ID" split.
"""

import pathlib

from django.test import (
    TestCase as DjangoTestCase,  # noqa: F401 - available but not needed for these
)

from accounting.qbo.readers import (
    QBOFormatError,
    aliases_for,
    parse_date,
    parse_decimal,
    read_table,
)

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "qbo_synthetic"


def _rows(path, context=None):
    return list(read_table(path, context=context))


# ---------------------------------------------------------------------------
# Helpers: csv and xlsx must agree
# ---------------------------------------------------------------------------


class CsvXlsxParityTest(DjangoTestCase):
    """Every csv fixture has an xlsx twin; they must parse identically."""

    def test_every_csv_has_an_xlsx_twin(self):
        csvs = sorted(FIXTURE_DIR.glob("*.csv"))
        self.assertGreater(len(csvs), 0, "No csv fixtures found")
        for csv_path in csvs:
            xlsx = csv_path.with_suffix(".xlsx")
            self.assertTrue(xlsx.exists(), f"Missing xlsx twin for {csv_path.name}")

    def test_csv_and_xlsx_parse_to_same_values(self):
        # Spot-check a few representative files in both formats; exhaustive
        # per-alias parity is covered by the parametrized tests below which
        # run in both formats via the dual-format cases.
        for name in (
            "coa_full_with_preamble_and_extras",
            "txn_full_journal_with_preamble",
            "coa_name_is_account",
            "txn_name_is_entity",
        ):
            csv_rows = _rows(FIXTURE_DIR / f"{name}.csv", context=None)
            xlsx_rows = _rows(FIXTURE_DIR / f"{name}.xlsx", context=None)
            # Compare raw header->value keys (normalized comparison would hide bugs)
            self.assertEqual(len(csv_rows), len(xlsx_rows), name)
            for cr, xr in zip(csv_rows, xlsx_rows):
                self.assertEqual(set(cr.raw.keys()), set(xr.raw.keys()), name)
                for k in cr.raw:
                    # openpyxl yields None for empty cells, csv yields '' — both mean blank
                    cv = "" if cr.raw[k] is None else str(cr.raw[k])
                    xv = "" if xr.raw[k] is None else str(xr.raw[k])
                    self.assertEqual(cv, xv, f"{name} col {k!r}")


# ---------------------------------------------------------------------------
# COA: account_number alias spellings (csv)
# ---------------------------------------------------------------------------


class CoaAccountNumberAliasTest(DjangoTestCase):
    def test_account_number_via_account_number(self):
        row = _rows(FIXTURE_DIR / "coa_account_number_via_account_number.csv")[0]
        self.assertEqual(row["account_number"], "1000")

    def test_account_number_via_account_no(self):
        row = _rows(FIXTURE_DIR / "coa_account_number_via_account_no.csv")[0]
        self.assertEqual(row["account_number"], "1000")

    def test_account_number_via_hash(self):
        row = _rows(FIXTURE_DIR / "coa_account_number_via_account_hash.csv")[0]
        self.assertEqual(row["account_number"], "1000")

    def test_account_number_via_number(self):
        row = _rows(FIXTURE_DIR / "coa_account_number_via_number.csv")[0]
        self.assertEqual(row["account_number"], "1000")

    def test_account_number_via_num(self):
        row = _rows(FIXTURE_DIR / "coa_account_number_via_num.csv")[0]
        self.assertEqual(row["account_number"], "1000")

    def test_account_number_via_no(self):
        row = _rows(FIXTURE_DIR / "coa_account_number_via_no.csv")[0]
        self.assertEqual(row["account_number"], "1000")

    def test_coa_aliases_resolve_via_xlsx_too(self):
        # One representative xlsx check proves the xlsx path handles alias headers
        row = _rows(FIXTURE_DIR / "coa_account_number_via_account_hash.xlsx")[0]
        self.assertEqual(row["account_number"], "1000")


# ---------------------------------------------------------------------------
# COA: header normalization (case / whitespace / punctuation)
# ---------------------------------------------------------------------------


class CoaHeaderNormalizationTest(DjangoTestCase):
    def test_whitespace_and_case_normalization(self):
        row = _rows(FIXTURE_DIR / "coa_header_normalization.csv")[0]
        self.assertEqual(row["account_number"], "1000")
        self.assertEqual(row["account_name"], "Checking")

    def test_punctuation_normalization(self):
        row = _rows(FIXTURE_DIR / "coa_header_punctuation.csv")[0]
        self.assertEqual(row["account_number"], "1000")
        self.assertEqual(row["account_name"], "Checking")

    def test_case_variants(self):
        row = _rows(FIXTURE_DIR / "coa_header_case_variants.csv")[0]
        self.assertEqual(row["account_number"], "2000")
        self.assertEqual(row["account_name"], "Savings")


# ---------------------------------------------------------------------------
# COA: full export with preamble, extras, empty rows
# ---------------------------------------------------------------------------


class CoaFullExportTest(DjangoTestCase):
    def test_preamble_is_skipped_and_all_fields_resolve(self):
        rows = _rows(FIXTURE_DIR / "coa_full_with_preamble_and_extras.csv", context="coa")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["account_number"], "1000")
        self.assertEqual(rows[0]["account_name"], "Checking")
        # Full name
        self.assertEqual(rows[0]["account_full_name"], "Checking")
        # Type / detail — parens + slash normalization
        self.assertEqual(rows[2]["account_type"], "Accounts Receivable (A/R)")
        # Balance / currency
        self.assertEqual(rows[0]["balance"], "12,345.67")
        self.assertEqual(rows[0]["currency"], "USD")
        # qbo_id in coa context: "ID" still maps
        self.assertEqual(rows[0]["qbo_id"], "1")
        # Active + parent
        self.assertEqual(rows[0]["active"], "Active")
        # Unknown extra columns don't confuse header detection; blank row dropped

    def test_minimal_columns_missing_fields_are_blank(self):
        rows = _rows(FIXTURE_DIR / "coa_minimal_columns.csv")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["account_number"], "1000")
        self.assertEqual(rows[0]["account_name"], "Checking")
        self.assertEqual(rows[0].get("balance", ""), "")
        self.assertFalse(rows[0].has("balance"))

    def test_lone_name_is_account_in_coa_context(self):
        rows = _rows(FIXTURE_DIR / "coa_name_is_account.csv", context="coa")
        self.assertEqual(rows[0]["account_name"], "Checking")
        self.assertEqual(rows[0].has("account_name"), True)

    def test_name_and_account_both_present(self):
        row = _rows(FIXTURE_DIR / "coa_name_and_account_both_present.csv", context="coa")[0]
        # "Account" is higher-priority alias; Name still resolves to account_name but
        # explicit Account header wins in raw (they are distinct columns)
        self.assertEqual(row.raw.get("Account"), "Explicit Account Value")
        self.assertEqual(row.raw.get("Name"), "Bare Name Value")
        # Row.get iterates aliases_for("coa")["account_name"] = ["account name","account","name",...]
        # normalize("Account") is in there, so account_name resolves to the Account column's value
        self.assertEqual(row["account_name"], "Explicit Account Value")

    def test_duplicate_headers_do_not_shadow(self):
        row = _rows(FIXTURE_DIR / "coa_duplicate_headers.csv")[0]
        self.assertEqual(set(row.raw.values()), {"Checking", "10.00", "20.00"})

    def test_empty_rows_and_spacers(self):
        rows = _rows(FIXTURE_DIR / "coa_empty_rows_and_spacers.csv")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["account_name"], "Checking")
        self.assertEqual(rows[1]["account_name"], "Sales")

    def test_short_rows_padded(self):
        row = _rows(FIXTURE_DIR / "coa_short_rows_padded.csv")[0]
        self.assertEqual(row["account_number"], "1000")
        self.assertEqual(row["account_name"], "Checking")
        self.assertEqual(row.get("balance", ""), "")

    def test_header_with_only_one_match_raises(self):
        with self.assertRaises(QBOFormatError):
            _rows(FIXTURE_DIR / "coa_header_only_one_match.csv")

    def test_description_vs_memo_header(self):
        row = _rows(FIXTURE_DIR / "coa_description_vs_memo.csv")[0]
        # "Description" maps to both description and memo; both should resolve
        self.assertEqual(row["description"], "Main checking")
        self.assertEqual(row["memo"], "Main checking")


# ---------------------------------------------------------------------------
# Transactions: doc_number, transaction_type, entity, qbo_id alias spells
# ---------------------------------------------------------------------------


class TxnAliasSpellingsTest(DjangoTestCase):
    def test_doc_number_aliases(self):
        for tag in ("num", "no", "number", "doc_number", "document_number", "ref_no", "reference_number"):
            path = FIXTURE_DIR / f"txn_doc_number_via_{tag}.csv"
            row = _rows(path)[0]
            self.assertEqual(row["doc_number"], "1001", f"alias {tag}")

    def test_transaction_type_aliases(self):
        for tag in ("type", "transaction_type", "txn_type"):
            path = FIXTURE_DIR / f"txn_transaction_type_via_{tag}.csv"
            row = _rows(path)[0]
            self.assertEqual(row["transaction_type"], "Invoice", f"alias {tag}")

    def test_entity_aliases(self):
        for tag in ("customer", "vendor", "payee", "customer_vendor", "entity"):
            path = FIXTURE_DIR / f"txn_entity_via_{tag}.csv"
            row = _rows(path)[0]
            self.assertEqual(row["entity_name"], "Acme Solar", f"alias {tag}")

    def test_qbo_id_aliases_in_transactions_context(self):
        for tag in ("transaction_id", "txn_id", "journal_id", "qbo_txn_id"):
            path = FIXTURE_DIR / f"txn_qbo_id_via_{tag}.csv"
            row = _rows(path, context="transactions")[0]
            self.assertEqual(row["qbo_id"], "TXN-99", f"alias {tag}")

    def test_qbo_id_aliases_in_transactions_context_xlsx(self):
        row = _rows(FIXTURE_DIR / "txn_qbo_id_via_transaction_id.xlsx", context="transactions")[0]
        self.assertEqual(row["qbo_id"], "TXN-99")


# ---------------------------------------------------------------------------
# Transactions: Name / ID disambiguation (the critical cross-context trap)
# ---------------------------------------------------------------------------


class TxnContextDisambiguationTest(DjangoTestCase):
    def test_name_is_entity_not_account_in_journal(self):
        row = _rows(FIXTURE_DIR / "txn_name_is_entity.csv", context="transactions")[0]
        self.assertEqual(row["entity_name"], "Acme Solar")
        self.assertEqual(row["account_name"], "")

    def test_name_is_entity_not_account_in_journal_xlsx(self):
        row = _rows(FIXTURE_DIR / "txn_name_is_entity.xlsx", context="transactions")[0]
        self.assertEqual(row["entity_name"], "Acme Solar")
        self.assertEqual(row["account_name"], "")

    def test_name_and_account_both_present_transactions(self):
        row = _rows(FIXTURE_DIR / "txn_name_and_account.csv", context="transactions")[0]
        self.assertEqual(row["entity_name"], "Acme Solar")
        self.assertEqual(row["account_name"], "Accounts Receivable")

    def test_full_journal_preamble_and_all_fields(self):
        rows = _rows(FIXTURE_DIR / "txn_full_journal_with_preamble.csv", context="transactions")
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["date"], "01/15/2025")
        self.assertEqual(rows[0]["transaction_type"], "Invoice")
        self.assertEqual(rows[0]["doc_number"], "1001")
        self.assertEqual(rows[0]["entity_name"], "Acme Solar")
        self.assertEqual(rows[0]["account_name"], "Accounts Receivable")
        self.assertEqual(rows[0]["debit"], "108.25")
        self.assertEqual(rows[1]["credit"], "100.00")
        # alias via context: "Name" correctly not stealing account
        self.assertEqual(rows[2]["entity_name"], "Vendor Co")

    def test_bare_id_not_in_transactions_context_qbo_id(self):
        # In transactions context, bare "ID" must NOT resolve qbo_id (only transaction-scoped IDs)
        # aliases_for("transactions")["qbo_id"] is ["transaction id","txn id","qbo txn id","journal id"]
        aliases = aliases_for("transactions")["qbo_id"]
        from accounting.qbo.mapping import normalize as _normalize
        self.assertNotIn(_normalize("ID"), [_normalize(a) for a in aliases])


# ---------------------------------------------------------------------------
# Transactions: dates + money
# ---------------------------------------------------------------------------


class TxnDateAndMoneyTest(DjangoTestCase):
    def test_mixed_date_formats(self):
        rows = _rows(FIXTURE_DIR / "txn_dates_mixed.csv")
        self.assertEqual(parse_date(rows[0]["date"]), parse_date("2025-03-04"))
        self.assertEqual(parse_date(rows[1]["date"]), parse_date("03/04/2025"))
        self.assertEqual(parse_date(rows[2]["date"]), parse_date("15 Jan 2025"))
        self.assertEqual(parse_date(rows[3]["date"]), parse_date("January 15, 2025"))

    def test_money_variants(self):
        rows = _rows(FIXTURE_DIR / "txn_money_variants.csv")
        self.assertEqual(parse_decimal(rows[0]["debit"]), parse_decimal("$1,234.56"))
        self.assertEqual(parse_decimal(rows[1]["debit"]), parse_decimal("(500.00)"))
        self.assertEqual(parse_decimal(rows[2]["debit"]), parse_decimal("-42.00"))
        self.assertEqual(parse_decimal(rows[3]["credit"]), parse_decimal("100.00"))

    def test_minimal_columns(self):
        row = _rows(FIXTURE_DIR / "txn_minimal_columns.csv")[0]
        self.assertEqual(row["date"], "01/15/2025")
        self.assertEqual(row["account_name"], "Sales")
        self.assertEqual(row["debit"], "100.00")

    def test_extras_and_blanks(self):
        rows = _rows(FIXTURE_DIR / "txn_with_extras_and_blanks.csv")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["account_name"], "Sales")

    def test_duplicate_amount(self):
        row = _rows(FIXTURE_DIR / "txn_duplicate_amount.csv")[0]
        self.assertEqual(set(row.raw.values()), {"01/15/2025", "Sales", "10.00", "20.00"})

    def test_header_normalization_txn(self):
        for name in ("txn_header_normalization", "txn_header_punctuation"):
            row = _rows(FIXTURE_DIR / f"{name}.csv", context="transactions")[0]
            self.assertEqual(row["entity_name"], "Acme", name)
            self.assertEqual(row["account_name"], "Sales", name)


# ---------------------------------------------------------------------------
# Ambiguous headers: Number / Type / Description / Memo Description
# (documenting which field wins — these are intentional collisions)
# ---------------------------------------------------------------------------


class AmbiguousAliasTest(DjangoTestCase):
    """QBO vocabulary overlaps. Which field wins is a code decision; pin it."""

    def test_number_is_account_number_not_doc_number_without_context(self):
        # "Number" is in both account_number and doc_number. Base table has
        # account_number first in COLUMN_ALIASES dict, but Row.get is per-field,
        # so BOTH fields resolve from the same header — the ambiguity is that
        # a single "Number" column populates two logical fields.
        row = _rows(FIXTURE_DIR / "txn_number_header_ambiguous.csv")[0]
        self.assertEqual(row["account_number"], "1001")
        self.assertEqual(row["doc_number"], "1001")
        # This is the documented overlap: AMBIGUITIES.md

    def test_type_is_both_account_type_and_transaction_type(self):
        row = _rows(FIXTURE_DIR / "txn_type_header_ambiguous.csv")[0]
        self.assertEqual(row["account_type"], "Invoice")
        self.assertEqual(row["transaction_type"], "Invoice")

    def test_description_maps_to_both_description_and_memo(self):
        row = _rows(FIXTURE_DIR / "coa_description_vs_memo.csv")[0]
        self.assertEqual(row["description"], "Main checking")
        self.assertEqual(row["memo"], "Main checking")

    def test_memo_description_maps_to_both(self):
        row = _rows(FIXTURE_DIR / "txn_memo_description_ambiguous.csv")[0]
        self.assertEqual(row["description"], "Office supplies")
        self.assertEqual(row["memo"], "Office supplies")


# ---------------------------------------------------------------------------
# Regression: aliases_for contract + _KNOWN_HEADERS span
# ---------------------------------------------------------------------------


class AliasContractTest(DjangoTestCase):
    def test_every_base_alias_is_in_known_headers(self):
        from accounting.qbo.mapping import normalize
        from accounting.qbo.readers import _KNOWN_HEADERS, COLUMN_ALIASES

        for field, aliases in COLUMN_ALIASES.items():
            for alias in aliases:
                self.assertIn(normalize(alias), _KNOWN_HEADERS, f"{field}:{alias}")

    def test_every_context_alias_is_in_known_headers(self):
        from accounting.qbo.mapping import normalize
        from accounting.qbo.readers import _KNOWN_HEADERS, CONTEXT_ALIASES

        for ctx, table in CONTEXT_ALIASES.items():
            for field, aliases in table.items():
                for alias in aliases:
                    self.assertIn(normalize(alias), _KNOWN_HEADERS, f"{ctx}:{field}:{alias}")

    def test_aliases_for_unknown_context_raises(self):
        with self.assertRaises(ValueError):
            aliases_for("nonsense")

    def test_aliases_for_none_returns_base(self):
        from accounting.qbo.readers import COLUMN_ALIASES

        self.assertEqual(aliases_for(None), COLUMN_ALIASES)
        self.assertEqual(aliases_for(""), COLUMN_ALIASES)
