"""Read-only data-integrity checks behind ``manage.py check_money`` and
``manage.py check_numbering`` (docs/release-hardening-plan.md 7.2 and 7.3).

Both are comparators over the current database, written so a release
candidate can be checked on a clone of a real database before the cutover:

- :func:`compare_totals` recomputes every quote, order, sales invoice and
  purchase order with the model's own ``recalculate_totals()`` inside one
  transaction that is always rolled back, and buckets each difference by a
  named cause (:data:`CAUSES`). It also checks the trial balance per company
  and every invoice's balance against its payments.
- :func:`numbering_report` checks document numbers per company and
  document type: uniqueness, monotonic order by ``created_at`` where the
  number carries a sequence, and gaps as a statistic.

Severity follows the plan: a posted invoice whose recomputed total differs by
more than one minor unit is S1 (``ROLL BACK``) unless a *historical* cause
explains it; any other difference is S2 (``DEGRADED``, listed for the release
notes); duplicates in numbering are ``ROLL BACK``; order violations are
``DEGRADED``; gaps never fail. The exit codes are the shared table in
``core/management/release_exit_codes.py``.

Nothing here writes: the recomputation runs inside ``transaction.atomic()``
and ends with ``transaction.set_rollback(True)``; on_commit hooks (lifecycle
signals, outbox rows) are discarded with it.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, F, Q, Sum

from core.management import release_exit_codes as codes

MINOR_UNIT = Decimal("0.01")
ZERO = Decimal("0")

# --- money -----------------------------------------------------------------------


@dataclass(frozen=True)
class Cause:
    """A named reason a stored total can differ from the recomputed one."""

    key: str
    #: A named cause: S2, listed for the release notes.
    explained: bool
    #: Rows written before a named change in the code, or copied from a
    #: previous platform: never S1, even on a posted invoice.
    historical: bool
    description: str


CAUSES: dict[str, Cause] = {
    c.key: c
    for c in (
        Cause(
            "legacy_import", True, True,
            "Imported from a previous platform (an external_source / bc_id / erpnext_name / "
            "d1_id marker, metadata.bc_import, or a number outside the DocumentSequence "
            "pattern): totals were copied, not computed by golden.",
        ),
        Cause(
            "finalized_snapshot", True, True,
            "Paid, void or canceled invoice whose order's totals changed later; "
            "SalesInvoice.recalculate_totals() refuses to re-snapshot a finalized invoice.",
        ),
        Cause(
            "rounding", True, True,
            "Within one minor unit per line: the writer rounded differently (half-even, float "
            "or client-side) from the model's ROUND_HALF_UP.",
        ),
        Cause(
            "coupon_excess", True, True,
            "Order balance_due written before the coupon fix (WP #572 BUG-06): the "
            "non-mirrored coupon excess max(coupon - discount, 0) was not deducted.",
        ),
        Cause(
            "line_totals_stale", True, False,
            "A line's stored line total or discount is not qty x unit - discount; the header "
            "inherited the stale line.",
        ),
        Cause(
            "discount_percent_rederived", True, False,
            "The header discount_percent is set but discount_amount was not derived from it.",
        ),
        Cause("no_lines", True, False, "Stored totals on a document with no line items."),
        Cause(
            "discount_double_applied", True, False,
            "grand_total is off by exactly the header discount_amount: the writer applied the "
            "discount at a different level (twice, or not at all) from recalculate_totals().",
        ),
        Cause(
            "tax_stale", True, False,
            "Only tax_total / subtotal_inc_tax differ: the line taxes were recomputed or "
            "removed after the header was written.",
        ),
        Cause(
            "balance_not_refreshed", True, False,
            "balance_due differs on its own (not as a consequence of grand_total): "
            "amount_paid, a refund or store credit changed without a recalculation.",
        ),
        Cause(
            "cancelled_balance_zeroed", True, False,
            "A canceled order stores balance_due 0 while grand_total - amount_paid is not 0; "
            "recalculate_totals() would restore the arithmetic balance.",
        ),
        Cause(
            "invoice_order_drift", False, False,
            "An open (not finalized) invoice's snapshot no longer matches its order's totals.",
        ),
        Cause("unexplained", False, False, "No named cause matches."),
        # Invoice balance checks (not recomputed totals; see _invoice_balances).
        Cause(
            "balance_arithmetic", False, False,
            "balance_due != grand_total - amount_paid on the stored row.",
        ),
        Cause(
            "ledger_mismatch", False, False,
            "amount_paid != the captured/settled payments applied to the invoice "
            "(amount - convenience_fee).",
        ),
        Cause(
            "no_payment_rows", True, True,
            "amount_paid is set but no Payment row is linked to the invoice (a manual or "
            "imported settlement).",
        ),
        Cause(
            "deposit_overapplied", False, False,
            "A customer-deposit account (a payment rule's deposit_account) has a debit "
            "balance: more was applied to invoices or refunded out of it than payments "
            "put in.",
        ),
    )
}

INVOICE_BALANCE_CHECKS = ("balance_arithmetic", "ledger_mismatch", "no_payment_rows")

#: Invoice statuses that are not "posted": no GL entry, nothing sent.
UNPOSTED_INVOICE_STATUSES = ("draft", "void", "cancelled")


@dataclass
class DocSpec:
    key: str
    model: type
    number_field: str
    prefix: str
    fields: tuple[str, ...]
    #: Reverse accessor of the line items and the line's total field, or None
    #: for a document whose totals are a snapshot of another (the invoice).
    lines: str | None = None
    line_total: str | None = None


def money_specs() -> list[DocSpec]:
    """The documents ``compare_totals`` recomputes (imported lazily: this module
    is imported by management commands, which must not load models at import)."""
    from orders.models import Order, Quote, SalesInvoice
    from purchasing.models import PurchaseOrder

    return [
        DocSpec(
            "quote", Quote, "quote_number", "QT",
            ("subtotal_ex_tax", "tax_total", "discount_amount", "grand_total"),
            "line_items", "line_total_ex_tax",
        ),
        DocSpec(
            "order", Order, "order_number", "SO",
            ("subtotal_ex_tax", "subtotal_inc_tax", "tax_total", "discount_amount",
             "grand_total", "balance_due"),
            "line_items", "line_total_ex_tax",
        ),
        DocSpec(
            "invoice", SalesInvoice, "invoice_number", "INV",
            ("subtotal_ex_tax", "tax_total", "shipping_cost", "grand_total", "balance_due"),
        ),
        DocSpec(
            "purchase_order", PurchaseOrder, "po_number", "PO",
            ("subtotal", "tax_total", "discount_amount", "grand_total", "balance_due"),
            "line_items", "line_total",
        ),
    ]


@dataclass
class Bucket:
    doc_type: str
    cause: str
    count: int = 0
    #: Posted invoices in this bucket with a stored amount more than one minor
    #: unit from the recomputed one (the S1 rule). Always 0 for other types.
    posted_over_unit: int = 0
    max_abs_diff: Decimal = ZERO
    samples: list[str] = field(default_factory=list)
    #: How many documents in the bucket differ on each field.
    fields: Counter = field(default_factory=Counter)

    def add(self, number: str, diffs: dict[str, Decimal], *, posted: bool, limit: int) -> None:
        self.count += 1
        self.fields.update(diffs.keys())
        worst = max((abs(d) for d in diffs.values()), default=ZERO).quantize(MINOR_UNIT)
        self.max_abs_diff = max(self.max_abs_diff, worst)
        if posted and worst > MINOR_UNIT:
            self.posted_over_unit += 1
        if len(self.samples) < limit:
            self.samples.append(number)

    @property
    def explained(self) -> bool:
        return CAUSES[self.cause].explained

    @property
    def historical(self) -> bool:
        return CAUSES[self.cause].historical

    @property
    def status(self) -> str:
        if self.posted_over_unit and not self.historical:
            return codes.STATUS_ROLL_BACK
        return codes.STATUS_DEGRADED

    def as_dict(self) -> dict:
        return {
            "doc_type": self.doc_type, "cause": self.cause, "explained": self.explained,
            "historical": self.historical, "count": self.count,
            "posted_over_unit": self.posted_over_unit, "max_abs_diff": str(self.max_abs_diff),
            "status": self.status, "fields": dict(sorted(self.fields.items())),
            "samples": list(self.samples),
        }


@dataclass
class MoneyResult:
    examined: dict[str, int] = field(default_factory=dict)
    buckets: dict[tuple[str, str], Bucket] = field(default_factory=dict)
    trial_balance: list[dict] = field(default_factory=list)
    invoice_balances: dict[str, Bucket] = field(default_factory=dict)

    def bucket(self, doc_type: str, cause: str) -> Bucket:
        key = (doc_type, cause)
        if key not in self.buckets:
            self.buckets[key] = Bucket(doc_type, cause)
        return self.buckets[key]

    @property
    def status(self) -> str:
        statuses = [b.status for b in self.buckets.values()]
        statuses += [b.status for b in self.invoice_balances.values()]
        statuses += [row["status"] for row in self.trial_balance]
        return codes.worst(statuses)

    @property
    def exit_code(self) -> int:
        return codes.EXIT_FOR_STATUS[self.status]

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "examined": dict(self.examined),
            "buckets": [b.as_dict() for b in sorted(self.buckets.values(),
                                                    key=lambda b: (b.doc_type, b.cause))],
            "trial_balance": [
                {**row, "debits": str(row["debits"]), "credits": str(row["credits"]),
                 "difference": str(row["difference"])}
                for row in self.trial_balance
            ],
            "invoice_balances": [b.as_dict() for _, b in sorted(self.invoice_balances.items())],
        }


def _dec(value) -> Decimal:
    return Decimal(value or 0)


def _snapshot(doc, fields) -> dict[str, Decimal]:
    return {f: _dec(getattr(doc, f)) for f in fields}


def _lines(doc, spec: DocSpec) -> dict:
    if spec.lines is None:
        return {}
    return {
        pk: (_dec(total), _dec(discount))
        for pk, total, discount in getattr(doc, spec.lines)
        .values_list("pk", spec.line_total, "discount_amount")
    }


def sequence_pattern(prefix: str) -> re.Pattern:
    return re.compile(rf"{re.escape(prefix)}-(\d+)")


def _is_legacy(spec: DocSpec, doc) -> bool:
    """Imported from a previous platform: a number outside the sequence
    pattern, or (orders, and the invoices of orders) an import marker."""
    number = getattr(doc, spec.number_field) or ""
    if not sequence_pattern(spec.prefix).fullmatch(number):
        return True
    if spec.key == "invoice":
        return _is_legacy_order(doc.order)
    return spec.key == "order" and _is_legacy_order(doc)


#: ``Order.external_source`` values written by an import from a previous
#: platform. The storefront's own ``web_checkout`` is native, not legacy.
LEGACY_SOURCES = frozenset({"bigcommerce", "erpnext", "d1"})


def _is_legacy_order(order) -> bool:
    if not sequence_pattern("SO").fullmatch(order.order_number or ""):
        return True
    if (order.external_source or "").lower() in LEGACY_SOURCES:
        return True
    markers = (order.bc_id, order.erpnext_name, order.d1_id)
    return any(markers) or bool((order.metadata or {}).get("bc_import"))


def _classify(spec: DocSpec, doc, before, after, diffs, lines_before, lines_after) -> list:
    """``[(cause, its differences)]``: at most one cause for the header totals
    and one for ``balance_due`` when it moved on its own (a balance that moved
    by exactly the grand_total difference is a consequence, not a cause). A
    document is counted under each cause it has."""
    header = {f: d for f, d in diffs.items() if f != "balance_due"}
    balance = diffs.get("balance_due")
    if balance is not None and balance == header.get("grand_total"):
        header["balance_due"] = balance  # follows grand_total
        balance = None
    if _is_legacy(spec, doc):
        return [("legacy_import", diffs)]
    if spec.key == "invoice":
        return [("invoice_order_drift", diffs)]
    causes = []
    if header:
        causes.append(
            (_classify_header(spec, doc, after, header, lines_before, lines_after), header)
        )
    if balance is not None:
        causes.append(
            (_classify_balance(spec, doc, before, after, balance), {"balance_due": balance})
        )
    return causes


def _classify_header(spec, doc, after, header, lines_before, lines_after) -> str:
    if not lines_before:
        return "no_lines"
    changed = {
        pk: (lines_before[pk], lines_after.get(pk)) for pk in lines_before
        if lines_after.get(pk) != lines_before[pk]
    }
    if changed:
        if all(
            after_line is not None
            and abs(after_line[0] - before_line[0]) <= MINOR_UNIT
            and abs(after_line[1] - before_line[1]) <= MINOR_UNIT
            for before_line, after_line in changed.values()
        ):
            return "rounding"
        return "line_totals_stale"
    discount = _dec(getattr(doc, "discount_amount", 0))
    if _dec(getattr(doc, "discount_percent", 0)) > 0 and "discount_amount" in header:
        return "discount_percent_rederived"
    if set(header) <= {"tax_total", "subtotal_inc_tax"}:
        return "tax_stale"
    if (
        discount > 0 and "grand_total" in header
        and abs(abs(header["grand_total"]) - discount) <= MINOR_UNIT
        and set(header) <= {"grand_total", "subtotal_inc_tax", "balance_due"}
    ):
        return "discount_double_applied"
    if all(abs(d) <= MINOR_UNIT * len(lines_before) for d in header.values()):
        return "rounding"
    return "unexplained"


def _classify_balance(spec, doc, before, after, balance) -> str:
    if spec.key == "order":
        excess = max(_dec(doc.coupon_discount) - after["discount_amount"], ZERO)
        if excess and abs(balance + excess) <= MINOR_UNIT:
            return "coupon_excess"
        if doc.status == "cancelled" and before["balance_due"] == ZERO:
            return "cancelled_balance_zeroed"
    return "balance_not_refreshed"


def _compare_documents(result: MoneyResult, spec: DocSpec, *, limit, samples) -> None:
    """Recompute one document type.

    Orders run before invoices inside the same (rolled-back) transaction, so
    an invoice is compared with its order's *recomputed* totals: the reference
    is what the order should say, not what a tampered order row says. With
    ``--limit`` an order outside the cap is not recomputed first, and its
    invoice is compared with the stored order instead.
    """
    from orders.models import SalesInvoice, _q2

    qs = spec.model.objects.all().order_by("-created_at", "pk")
    if spec.key == "invoice":
        qs = qs.select_related("order")
    if limit:
        qs = qs[:limit]
    examined = 0
    for doc in qs:
        examined += 1
        before = _snapshot(doc, spec.fields)
        lines_before = _lines(doc, spec)
        doc.recalculate_totals()
        after = _snapshot(doc, spec.fields)
        lines_after = _lines(doc, spec)
        diffs = {f: after[f] - before[f] for f in spec.fields if after[f] != before[f]}
        number = getattr(doc, spec.number_field) or str(doc.pk)
        posted = spec.key == "invoice" and doc.status not in UNPOSTED_INVOICE_STATUSES
        if diffs:
            for cause, part in _classify(spec, doc, before, after, diffs, lines_before,
                                         lines_after):
                result.bucket(spec.key, cause).add(number, part, posted=posted, limit=samples)
        elif spec.key == "invoice" and doc.status in (
            SalesInvoice.Status.PAID, SalesInvoice.Status.VOID, SalesInvoice.Status.CANCELLED,
        ):
            # The method refused to re-snapshot; say how far the order moved.
            order = doc.order
            drift = {
                "subtotal_ex_tax": _q2(order.subtotal_ex_tax or 0) - before["subtotal_ex_tax"],
                "tax_total": _q2(order.tax_total or 0) - before["tax_total"],
                "shipping_cost": _q2(order.shipping_cost_ex_tax or 0) - before["shipping_cost"],
                "grand_total": _q2(order.grand_total or 0) - before["grand_total"],
            }
            drift = {k: v for k, v in drift.items() if v}
            if drift:
                cause = "legacy_import" if _is_legacy(spec, doc) else "finalized_snapshot"
                result.bucket(spec.key, cause).add(number, drift, posted=posted, limit=samples)
    result.examined[spec.key] = examined


def _trial_balance(result: MoneyResult) -> None:
    from accounting.models import JournalEntry, JournalLine
    from core.models import Company

    posted = JournalEntry.Status.POSTED
    sums = {
        row["entry__company_id"]: row
        for row in JournalLine.objects.filter(entry__status=posted)
        .values("entry__company_id")
        .annotate(debits=Sum("debit_amount"), credits=Sum("credit_amount"),
                  entries=Count("entry_id", distinct=True))
    }
    unbalanced = Counter(
        JournalEntry.objects.filter(status=posted)
        .annotate(_debit=Sum("lines__debit_amount"), _credit=Sum("lines__credit_amount"))
        .filter(Q(lines__isnull=True) | ~Q(_debit=F("_credit")))
        .values_list("company_id", flat=True)
        .distinct()
    )
    unbalanced_samples = defaultdict(list)
    for company_id, pk, number in (
        JournalEntry.objects.filter(status=posted)
        .annotate(_debit=Sum("lines__debit_amount"), _credit=Sum("lines__credit_amount"))
        .filter(Q(lines__isnull=True) | ~Q(_debit=F("_credit")))
        .order_by("entry_date")
        .values_list("company_id", "pk", "entry_number")[:400]
    ):
        # The line join repeats an entry once per line.
        seen = unbalanced_samples[company_id]
        label = number or str(pk)
        if label not in seen and len(seen) < 5:
            seen.append(label)
    for company in Company.objects.order_by("created_at"):
        row = sums.get(company.pk, {})
        debits = _dec(row.get("debits")).quantize(MINOR_UNIT)
        credits = _dec(row.get("credits")).quantize(MINOR_UNIT)
        bad = unbalanced.get(company.pk, 0)
        difference = debits - credits
        status = codes.STATUS_ROLL_BACK if (difference or bad) else codes.STATUS_OK
        result.trial_balance.append({
            "company": str(company.pk), "posted_entries": row.get("entries", 0),
            "debits": debits, "credits": credits, "difference": difference,
            "unbalanced_entries": bad, "unbalanced_samples": unbalanced_samples.get(company.pk, []),
            "status": status,
        })


def _invoice_balances(result: MoneyResult, *, samples) -> None:
    from orders.models import SalesInvoice
    from payments.models import Payment

    settled = (Payment.Status.CAPTURED, Payment.Status.SETTLED)
    applied = {
        row["invoice_id"]: _dec(row["applied"])
        for row in Payment.objects.filter(invoice__isnull=False, status__in=settled)
        .values("invoice_id").annotate(applied=Sum(F("amount") - F("convenience_fee")))
    }
    linked = set(
        Payment.objects.filter(invoice__isnull=False).values_list("invoice_id", flat=True).distinct()
    )
    checks = {key: Bucket("invoice", key) for key in INVOICE_BALANCE_CHECKS}
    for pk, number, status, grand, paid, balance in SalesInvoice.objects.order_by(
        "-created_at"
    ).values_list("pk", "invoice_number", "status", "grand_total", "amount_paid", "balance_due"):
        posted = status not in UNPOSTED_INVOICE_STATUSES
        grand, paid, balance = _dec(grand), _dec(paid), _dec(balance)
        if balance != grand - paid:
            checks["balance_arithmetic"].add(
                number, {"balance_due": balance - (grand - paid)}, posted=posted, limit=samples,
            )
        ledger = applied.get(pk, ZERO)
        if paid != ledger:
            if pk in linked:
                checks["ledger_mismatch"].add(
                    number, {"amount_paid": paid - ledger}, posted=posted, limit=samples,
                )
            else:
                checks["no_payment_rows"].add(number, {"amount_paid": paid}, posted=posted,
                                              limit=samples)
    result.invoice_balances = {key: b for key, b in checks.items() if b.count}
    deposits = _deposit_balances(samples=samples)
    if deposits.count:
        result.invoice_balances["deposit_overapplied"] = deposits


def _deposit_balances(*, samples) -> Bucket:
    """Deposits applied (or refunded) never exceed deposits received: every
    account a payment rule names as ``deposit_account`` keeps a credit (or
    zero) balance over posted entries."""
    from accounting.models import DocumentPostingRule, JournalEntry, JournalLine

    bucket = Bucket("deposit", "deposit_overapplied")
    accounts = (
        DocumentPostingRule.objects.filter(deposit_account__isnull=False)
        .values_list("deposit_account_id", "deposit_account__account_number",
                     "deposit_account__name")
        .distinct()
    )
    for pk, number, name in accounts:
        totals = JournalLine.objects.filter(
            account_id=pk, entry__status=JournalEntry.Status.POSTED,
        ).aggregate(debit=Sum("debit_amount"), credit=Sum("credit_amount"))
        over = _dec(totals["debit"]) - _dec(totals["credit"])
        if over > 0:
            bucket.add(f"{number} {name}".strip(), {"balance": over}, posted=True, limit=samples)
    return bucket


def compare_totals(*, limit: int | None = None, samples: int = 5) -> MoneyResult:
    """Recompute every document's totals without keeping anything.

    ``limit`` caps the documents examined per type (newest first); ``samples``
    is how many document numbers each bucket keeps. The whole pass runs in one
    transaction that is rolled back at the end, so the per-line ``update()``
    and header ``save()`` calls inside ``recalculate_totals()`` never land.
    """
    result = MoneyResult()
    with transaction.atomic():
        try:
            for spec in money_specs():
                _compare_documents(result, spec, limit=limit, samples=samples)
        finally:
            transaction.set_rollback(True)
    _trial_balance(result)
    _invoice_balances(result, samples=samples)
    return result


# --- numbering ---------------------------------------------------------------------


@dataclass
class NumberedSpec:
    key: str
    model: type
    number_field: str
    prefix: str


def numbered_specs() -> list[NumberedSpec]:
    from crm.models import Engagement, Lead
    from orders.models import Order, Quote, SalesInvoice
    from payments.models import Payment
    from purchasing.models import PurchaseOrder

    return [
        NumberedSpec("quote", Quote, "quote_number", "QT"),
        NumberedSpec("order", Order, "order_number", "SO"),
        NumberedSpec("invoice", SalesInvoice, "invoice_number", "INV"),
        NumberedSpec("purchase_order", PurchaseOrder, "po_number", "PO"),
        NumberedSpec("engagement", Engagement, "number", "EN"),
        NumberedSpec("lead", Lead, "lead_number", "LD"),
        NumberedSpec("payment", Payment, "payment_number", "PAY"),
    ]


@dataclass
class NumberingRow:
    company: str
    doc_type: str
    rows: int = 0
    blank: int = 0
    duplicates: int = 0  # numbers held by more than one row
    duplicate_samples: list[str] = field(default_factory=list)
    non_sequence: int = 0  # numbers outside PREFIX-<digits>
    non_sequence_samples: list[str] = field(default_factory=list)
    order_violations: int = 0  # a later row (created_at) carries a lower sequence
    order_violation_samples: list[str] = field(default_factory=list)
    lowest: int | None = None
    highest: int | None = None
    gaps_missing: int = 0  # sequence values never used between lowest and highest
    gaps_runs: int = 0
    gaps_largest: int = 0

    @property
    def status(self) -> str:
        if self.duplicates:
            return codes.STATUS_ROLL_BACK
        if self.order_violations or self.blank:
            return codes.STATUS_DEGRADED
        return codes.STATUS_OK

    def as_dict(self) -> dict:
        data = {k: v for k, v in self.__dict__.items()}
        data["status"] = self.status
        return data


@dataclass
class NumberingResult:
    rows: list[NumberingRow] = field(default_factory=list)

    @property
    def status(self) -> str:
        return codes.worst(row.status for row in self.rows)

    @property
    def exit_code(self) -> int:
        return codes.EXIT_FOR_STATUS[self.status]

    def as_dict(self) -> dict:
        return {"status": self.status, "exit_code": self.exit_code,
                "rows": [row.as_dict() for row in self.rows]}


def _numbering_for(company_id, spec: NumberedSpec, values, *, samples: int) -> NumberingRow:
    """``values`` are ``(number, created_at, pk)`` for one company, any order."""
    row = NumberingRow(str(company_id), spec.key, rows=len(values))
    pattern = sequence_pattern(spec.prefix)
    numbers = Counter(number for number, _, _ in values if number)
    row.blank = sum(1 for number, _, _ in values if not number)
    dupes = sorted(number for number, n in numbers.items() if n > 1)
    row.duplicates = len(dupes)
    row.duplicate_samples = dupes[:samples]
    parsed = []  # (created_at, seq, number)
    for number, created_at, _pk in values:
        if not number:
            continue
        match = pattern.fullmatch(number)
        if match is None:
            row.non_sequence += 1
            if len(row.non_sequence_samples) < samples:
                row.non_sequence_samples.append(number)
            continue
        parsed.append((created_at, int(match.group(1)), number))
    # Ties on created_at (rows minted in one transaction) sort by sequence,
    # so they never read as inversions.
    parsed.sort(key=lambda item: (item[0], item[1]))
    highest_seen = None
    for _created_at, seq, number in parsed:
        if highest_seen is not None and seq < highest_seen:
            row.order_violations += 1
            if len(row.order_violation_samples) < samples:
                row.order_violation_samples.append(number)
        else:
            highest_seen = seq
    seqs = sorted({seq for _, seq, _ in parsed})
    if seqs:
        row.lowest, row.highest = seqs[0], seqs[-1]
        row.gaps_missing = seqs[-1] - seqs[0] + 1 - len(seqs)
        for a, b in zip(seqs, seqs[1:]):
            if b - a > 1:
                row.gaps_runs += 1
                row.gaps_largest = max(row.gaps_largest, b - a - 1)
    return row


def numbering_report(*, samples: int = 5) -> NumberingResult:
    """Per company and document type: uniqueness, order and gaps."""
    result = NumberingResult()
    for spec in numbered_specs():
        by_company = defaultdict(list)
        for company_id, number, created_at, pk in (
            spec.model.objects.values_list("company_id", spec.number_field, "created_at", "pk")
        ):
            by_company[company_id].append((number, created_at, pk))
        for company_id in sorted(by_company, key=str):
            result.rows.append(
                _numbering_for(company_id, spec, by_company[company_id], samples=samples)
            )
    return result


# --- output ------------------------------------------------------------------------


def render_table(headers: list[str], rows: list[list]) -> str:
    """Fixed-width text table; every cell is rendered with ``str``."""
    cells = [[str(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers).rstrip(), fmt.format(*("-" * w for w in widths)).rstrip()]
    lines += [fmt.format(*row).rstrip() for row in cells]
    return "\n".join(lines)
