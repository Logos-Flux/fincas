"""Spreadsheet-formula neutralisation for every CSV golden writes.

Excel, LibreOffice and Google Sheets evaluate a cell that starts with ``=``,
``+``, ``-`` or ``@`` as a formula, and a leading tab or carriage return can
smuggle one past a naive check (CSV injection). Names, titles, notes and
account numbers come from storefront sign-ups, lead forms and third-party
imports, so every writer passes its cells through :func:`csv_safe`.

The one rule, applied by every CSV writer in golden (``core.list_export``,
``ERPListView.export_csv``, the report and accounting exports, the export
management commands):

* a **string** cell whose first character is one of ``= + - @ \\t \\r`` is
  written with a single quote in front (``'=HYPERLINK(...)``: readable, inert),
  **unless** the whole string is a plain number (``-?digits[.digits]``), which
  no spreadsheet treats as a formula, so a canonical ``-12.50`` keeps its sign;
* any other value (``int``, ``Decimal``, ``date``, ``None``, ``bool``...) is
  written as is: the ``csv`` module formats it, and a number written as a
  number is never a formula.

Writers that format money or dates themselves must therefore either pass the
number through, or format to text and accept the prefix on a negative
formatted amount (``'-$12.50``) - never bypass the helper for "known safe"
columns, because a column is only safe until an import fills it.
"""

from __future__ import annotations

import re

FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

# A plain number is never a formula, so a canonical negative amount keeps its sign.
_PLAIN_NUMBER_RE = re.compile(r"\A-?\d+(\.\d+)?\Z")


def csv_safe(value):
    """The value to hand to ``csv.writer`` for one cell (rule in the module doc)."""
    if isinstance(value, str) and value[:1] in FORMULA_TRIGGERS and not _PLAIN_NUMBER_RE.match(value):
        return "'" + value
    return value


def csv_safe_row(cells):
    """``csv_safe`` over every cell of one row."""
    return [csv_safe(cell) for cell in cells]
