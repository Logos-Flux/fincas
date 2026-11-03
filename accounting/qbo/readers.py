"""Turn a QBO export file into normalized, header-keyed rows.

QBO's "Export Data" produces .xlsx; its report screens produce .csv. Both are
supported and behave identically downstream.

Two things make raw QBO exports awkward, and both are handled here:

  * **Preamble rows.** Report exports lead with the company name, the report
    title and a date range before the real header row. ``read_table`` scans
    for the first row that looks like a header instead of assuming row 1.
  * **Header drift.** The same column is "Account #", "Account Number" or
    "Number" depending on export path and locale. Callers ask for a *field*
    ("account_number") and ``Row`` resolves it through an alias table, so the
    importer never hard-codes a spelling.

Header matching is normalized (casefolded, punctuation stripped) — see
``mapping.normalize``.
"""

import csv
import datetime
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .mapping import normalize

#: Field name → the header spellings QBO is known to use for it. First match
#: in the row wins, so order these most-specific first.
#:
#: ⚠ "Name" is genuinely ambiguous *between file types*: in an account-list
#: export it is the account's name, in a journal export it is the
#: customer/vendor on the line. Resolving it the same way in both silently
#: files transactions against accounts named after customers — so the bare
#: alias lives in the per-context overlay below, never in this base table.
COLUMN_ALIASES = {
    "account_number": ["account number", "account no", "account #", "number", "num", "no"],
    "account_name": ["account name", "account", "distribution account"],
    "account_full_name": ["full name", "account full name", "full account name"],
    "account_type": ["type", "account type"],
    "detail_type": ["detail type", "account detail type", "sub type", "subtype"],
    "description": ["description", "account description", "memo description"],
    "balance": ["balance", "balance total", "opening balance", "total"],
    "currency": ["currency", "currency code"],
    "qbo_id": ["id", "account id", "qbo id", "quickbooks id"],
    "active": ["active", "status", "is active"],
    "parent": ["parent", "parent account", "sub account of", "subaccount of"],
    # Transaction / journal columns
    "date": ["date", "transaction date", "txn date", "posting date"],
    "transaction_type": ["transaction type", "txn type", "type"],
    "doc_number": [
        "num", "no", "doc number", "document number", "ref no",
        "reference number", "number",
    ],
    "entity_name": ["customer", "vendor", "payee", "customer vendor", "entity"],
    "memo": ["memo description", "memo", "description"],
    # Approval state on a hand-maintained correcting-entries sheet. Only ever
    # read when the caller passes --require-status; the importer has no opinion
    # about what the values mean, it just matches them.
    "entry_status": ["status", "posting status", "entry status", "approval status"],
    "debit": ["debit", "debits", "amount debit"],
    "credit": ["credit", "credits", "amount credit"],
    "amount": ["amount", "amount usd"],
}

#: Per-context alias overrides, merged over COLUMN_ALIASES by ``read_table``.
#: This is where bare "Name" is disambiguated — see the warning above.
CONTEXT_ALIASES = {
    # Chart-of-accounts exports: a lone "Name" column is the account.
    "coa": {
        "account_name": ["account name", "account", "name", "distribution account"],
    },
    # Journal / transaction-detail exports: a lone "Name" column is the
    # customer, vendor or payee — never the account.
    "transactions": {
        "entity_name": [
            "name", "customer", "vendor", "payee", "customer vendor", "entity",
        ],
        # And a lone "ID"/"Account ID" is the *account's* id, not the
        # transaction's. Reading it as the entry id makes every row that
        # touches one account collapse into a single journal entry, deleting
        # and rewriting its lines each time — so only explicitly
        # transaction-scoped spellings count here.
        "qbo_id": ["transaction id", "txn id", "qbo txn id", "journal id"],
    },
}


def aliases_for(context=None):
    """Alias table for a file context, falling back to the base table."""
    if not context:
        return COLUMN_ALIASES
    if context not in CONTEXT_ALIASES:
        raise ValueError(
            f"Unknown reader context {context!r}; expected one of "
            f"{sorted(CONTEXT_ALIASES)}."
        )
    return {**COLUMN_ALIASES, **CONTEXT_ALIASES[context]}


#: A row qualifies as the header when it contains at least this many known
#: column names. Two is enough to skip a one-cell title row without demanding
#: that every export carry the same columns.
_MIN_HEADER_MATCHES = 2

_KNOWN_HEADERS = {
    normalize(alias)
    for table in [COLUMN_ALIASES, *CONTEXT_ALIASES.values()]
    for aliases in table.values()
    for alias in aliases
}

#: A trailing currency qualifier on a money column: "Debit (USD)", "Amount $",
#: "Balance EUR". ``normalize`` already drops the parentheses, so this only has
#: to strip the code or symbol left behind at the end.
#:
#: Report exports written for human eyes label the currency; "Export Data"
#: exports do not. Without this, a currency-labelled money column resolves to
#: nothing, every leg reads 0.00, and the import skips each row as a
#: zero-amount line — a *silent* no-op, because the file parses, the dates are
#: fine, and the only thing missing is the money. Anchored to the end and
#: limited to real currency tokens so "Account Number" and friends are
#: untouched.
_CURRENCY_SUFFIX = re.compile(
    r"\s+(?:usd|cad|aud|nzd|gbp|eur|jpy|chf|cny|inr|mxn|brl|zar|sgd|hkd|sek|nok|dkk|pln"
    r"|[$€£¥])$"
)


def normalize_header(value):
    """``normalize`` plus tolerance for a trailing currency qualifier."""
    return _CURRENCY_SUFFIX.sub("", normalize(value))

#: Tried in order. Unambiguous ISO first so a YYYY-MM-DD export is never
#: reinterpreted; the ambiguous slash formats are governed by `dayfirst`.
_ISO_FORMATS = ["%Y-%m-%d", "%Y/%m/%d"]
_UNAMBIGUOUS_FORMATS = ["%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y %H:%M:%S"]
_MONTH_FIRST = ["%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y"]
_DAY_FIRST = ["%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"]


class QBOFormatError(ValueError):
    """The file could not be parsed as a QBO export."""


class Row:
    """One data row, addressable by logical field name.

    ``row["account_name"]`` resolves through ``COLUMN_ALIASES``; ``row.raw``
    keeps the original header→value dict for diagnostics.
    """

    __slots__ = ("raw", "_normalized", "line_number", "_aliases")

    def __init__(self, raw, line_number, aliases=None):
        self.raw = raw
        self.line_number = line_number
        self._aliases = aliases or COLUMN_ALIASES
        indexed = {normalize(k): v for k, v in raw.items()}
        # Also make a currency-qualified header reachable by its bare name —
        # but only where nothing already claims that name, so a file carrying
        # both "Debit" and "Debit (USD)" keeps them as two distinct columns
        # instead of letting one silently shadow the other.
        for key, value in raw.items():
            indexed.setdefault(normalize_header(key), value)
        self._normalized = indexed

    def get(self, field, default=""):
        for alias in self._aliases.get(field, [field]):
            value = self._normalized.get(normalize(alias))
            if value not in (None, ""):
                return value
        return default

    def __getitem__(self, field):
        return self.get(field)

    def has(self, field):
        """True when the source actually carried a column for this field."""
        return any(
            normalize(alias) in self._normalized
            for alias in self._aliases.get(field, [field])
        )

    def is_annotation(self):
        """True for a one-cell commentary row: a section divider or banner.

        Hand-maintained journals carry rows like
        ``--- JE-X1 --- 2025-12-31 --- POST ---`` that occupy a single column
        and describe the block beneath them. A journal leg needs at least an
        account *and* an amount, so a row with one populated cell cannot be a
        leg under any header mapping.

        ⚠ This counts RAW cells, never resolved fields. Counting resolved
        fields would make a file whose headers failed to map look like a page
        of annotations and disappear without a single error — which is the
        exact silent no-op the money-column aliases exist to prevent.
        """
        populated = 0
        for value in self.raw.values():
            if value is None:
                continue
            if str(value).strip():
                populated += 1
                if populated > 1:
                    return False
        return populated == 1

    def is_blank(self):
        for value in self.raw.values():
            if value is None:
                continue
            if str(value).strip():
                return False
        return True

    def __repr__(self):
        return f"<Row line={self.line_number} {self.raw!r}>"


def _looks_like_header(cells):
    matches = sum(1 for c in cells if c and normalize_header(c) in _KNOWN_HEADERS)
    return matches >= _MIN_HEADER_MATCHES


def _dedupe_headers(cells):
    """Make blank/duplicate headers addressable without collapsing columns.

    QBO reports emit empty spacer columns and can repeat a name; keeping every
    column distinct means a later column never silently shadows an earlier one.
    """
    seen = {}
    out = []
    for i, cell in enumerate(cells):
        name = (str(cell).strip() if cell is not None else "") or f"column_{i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        out.append(name)
    return out


def _rows_from_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for cells in csv.reader(fh):
            yield [c.strip() if isinstance(c, str) else c for c in cells]


def _rows_from_xlsx(path):
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - dependency is in base.txt
        raise QBOFormatError(
            "Reading .xlsx exports needs openpyxl (it is in "
            "apps/erp/requirements/base.txt). Install it, or re-export as CSV."
        ) from exc

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook[workbook.sheetnames[0]]
        for cells in sheet.iter_rows(values_only=True):
            yield [c.strip() if isinstance(c, str) else c for c in cells]
    finally:
        workbook.close()


def read_table(path, context=None):
    """Yield ``Row`` objects from a .csv or .xlsx QBO export.

    Skips the preamble, treats the first header-looking row as the header, and
    drops fully blank rows (QBO pads reports with them).

    ``context`` selects the alias overlay — "coa" or "transactions". Pass it:
    it is what stops a journal export's "Name" column being read as the
    account. See ``CONTEXT_ALIASES``.
    """
    path = Path(path)
    aliases = aliases_for(context)
    suffix = path.suffix.casefold()
    if suffix == ".csv":
        raw_rows = _rows_from_csv(path)
    elif suffix in (".xlsx", ".xlsm"):
        raw_rows = _rows_from_xlsx(path)
    else:
        raise QBOFormatError(
            f"Unsupported export format {path.suffix!r} for {path.name}; "
            f"expected .csv or .xlsx."
        )

    header = None
    for line_number, cells in enumerate(raw_rows, start=1):
        if header is None:
            if _looks_like_header(cells):
                header = _dedupe_headers(cells)
            continue
        if not cells or all(c in (None, "") for c in cells):
            continue
        # Pad/truncate so zip() never silently drops trailing columns.
        padded = list(cells)[: len(header)]
        padded += [""] * (len(header) - len(padded))
        row = Row(dict(zip(header, padded)), line_number, aliases)
        if row.is_blank():
            continue
        yield row

    if header is None:
        raise QBOFormatError(
            f"No header row found in {path.name}. Expected a row containing at "
            f"least {_MIN_HEADER_MATCHES} recognized column names such as "
            f"'Account', 'Type', 'Date', 'Debit' or 'Credit'."
        )


# --- value coercion -------------------------------------------------------

#: Decoration a money cell is allowed to carry: currency symbols, thousands
#: separators, whitespace, and the accounting parentheses.
_MONEY_DECORATION = re.compile(r"[\s, $€£¥]")
#: What must remain once the decoration is gone. Anchored on purpose — a
#: permissive "strip everything non-numeric" turns "12x34" into 1234 and
#: "not a number" into 0.00, both silently wrong on a ledger.
_MONEY_BODY = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)$")
_HAS_DIGIT = re.compile(r"\d")


def parse_decimal(value, default=Decimal("0.00")):
    """Parse a QBO money cell into a Decimal.

    Handles currency symbols, thousands separators, and the accounting
    convention where parentheses mean negative: "(1,234.56)" → -1234.56.

    A cell with no digits at all ("", "-", "n/a") is a placeholder and yields
    ``default``. A cell that *has* digits but is not a clean number raises,
    rather than being silently coerced.
    """
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))

    text = str(value).strip()
    if not text:
        return default
    if not _HAS_DIGIT.search(text):
        return default

    negative = text.startswith("(") and text.endswith(")")
    body = text[1:-1] if negative else text

    # "1.234,56" is 1234.56 in a European export and meaningless in a US one.
    # Stripping commas blindly turns it into 1.23456, which then rounds to
    # 1.23 — a ~1000x understatement with no error anywhere. Refuse instead;
    # this parser only claims to read the US format.
    if "," in body and "." in body and body.rfind(",") > body.rfind("."):
        raise QBOFormatError(
            f"{value!r} looks like European decimal notation (comma as the "
            f"decimal separator). This importer reads US-format exports; "
            f"re-export with a period decimal separator."
        )
    body = _MONEY_DECORATION.sub("", body)
    if body.startswith("+"):
        body = body[1:]

    if not _MONEY_BODY.match(body):
        raise QBOFormatError(f"Cannot parse {value!r} as an amount.")
    try:
        amount = Decimal(body)
    except InvalidOperation:  # pragma: no cover - regex already guarantees this
        raise QBOFormatError(f"Cannot parse {value!r} as an amount.") from None
    return -amount if negative else amount


def parse_date(value, dayfirst=False):
    """Parse a QBO date cell into a ``datetime.date``.

    ``dayfirst`` picks the reading of ambiguous slash dates. It matters:
    03/04/2025 is 4 March under a US export and 3 April under a UK one, and
    nothing in the file itself disambiguates them — hence the explicit flag,
    surfaced as ``--dayfirst`` on the management command.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value

    text = str(value).strip()
    if not text:
        return None

    ambiguous = _DAY_FIRST + _MONTH_FIRST if dayfirst else _MONTH_FIRST + _DAY_FIRST
    for fmt in _ISO_FORMATS + _UNAMBIGUOUS_FORMATS + ambiguous:
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise QBOFormatError(f"Cannot parse {value!r} as a date.")


def parse_bool(value, default=True):
    """Parse QBO's assorted truthy spellings ('Yes', 'Active', 'TRUE', 1)."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = normalize(value)
    if text in ("yes", "y", "true", "1", "active", "enabled"):
        return True
    if text in ("no", "n", "false", "0", "inactive", "deleted", "disabled"):
        return False
    return default
