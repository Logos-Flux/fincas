"""General-ledger models — the Django replacement for QuickBooks Online.

Four models, standard double-entry:

    Account       chart of accounts, one tree per company
    FiscalPeriod  the calendar the entries are reported in
    JournalEntry  a balanced transaction header
    JournalLine   one debit-or-credit leg against one account

Two axes describe an account, and they are deliberately separate:

  * ``account_type`` is the accounting classification (asset / liability /
    equity / income / expense). Five values, constrained, and the only thing
    that decides an account's normal balance. Reports key off this.
  * ``account_subtype`` is the finer bucket (bank, accounts receivable,
    cost of goods sold, …). Curated rather than exhaustive.

QuickBooks' own vocabulary is kept verbatim alongside them in the
``qbo_account_type`` / ``qbo_detail_type`` provenance fields, so an import is
lossless even when QBO ships a detail type this file has never heard of. See
``accounting/qbo/mapping.py`` for the translation.

Balance is enforced in application code (``JournalEntry.validate_balanced``),
not by a database constraint — a constraint cannot see sibling rows. Every
writer in this package checks it: the QBO importer calls it directly, and the
admin enforces the same rule in ``JournalLineInlineFormSet.clean()``. A new
write path must do likewise; nothing below this layer will catch it.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction

from core.managers import CompanyScopedManager
from core.models import BaseModel

#: Money columns. 14 digits total is ~1 trillion at 2dp — well clear of
#: anything the ERP will post, and matches the wider ERP's DecimalField scale.
_MAX_DIGITS = 14
_DECIMAL_PLACES = 2

ZERO = Decimal("0.00")


class Account(BaseModel):
    """One node of a company's chart of accounts.

    Accounts form a tree via ``parent``. ``full_name`` carries the flattened
    colon-delimited path QBO uses ("Expenses:Utilities:Electric"), which is what
    the transaction exports reference accounts by — so it is the practical
    lookup key during an import, not ``name``.
    """

    class AccountType(models.TextChoices):
        ASSET = "asset", "Asset"
        LIABILITY = "liability", "Liability"
        EQUITY = "equity", "Equity"
        INCOME = "income", "Income"
        EXPENSE = "expense", "Expense"

    class AccountSubType(models.TextChoices):
        # Asset
        BANK = "bank", "Bank"
        ACCOUNTS_RECEIVABLE = "accounts_receivable", "Accounts Receivable"
        OTHER_CURRENT_ASSET = "other_current_asset", "Other Current Asset"
        INVENTORY = "inventory", "Inventory"
        UNDEPOSITED_FUNDS = "undeposited_funds", "Undeposited Funds"
        FIXED_ASSET = "fixed_asset", "Fixed Asset"
        ACCUMULATED_DEPRECIATION = "accumulated_depreciation", "Accumulated Depreciation"
        OTHER_ASSET = "other_asset", "Other Asset"
        # Liability
        ACCOUNTS_PAYABLE = "accounts_payable", "Accounts Payable"
        CREDIT_CARD = "credit_card", "Credit Card"
        OTHER_CURRENT_LIABILITY = "other_current_liability", "Other Current Liability"
        SALES_TAX_PAYABLE = "sales_tax_payable", "Sales Tax Payable"
        LONG_TERM_LIABILITY = "long_term_liability", "Long Term Liability"
        # Equity
        EQUITY = "equity", "Equity"
        RETAINED_EARNINGS = "retained_earnings", "Retained Earnings"
        OPENING_BALANCE_EQUITY = "opening_balance_equity", "Opening Balance Equity"
        # Income
        SALES_INCOME = "sales_income", "Sales Income"
        SERVICE_INCOME = "service_income", "Service Income"
        SHIPPING_INCOME = "shipping_income", "Shipping Income"
        DISCOUNTS_REFUNDS = "discounts_refunds", "Discounts / Refunds Given"
        OTHER_INCOME = "other_income", "Other Income"
        # Expense
        COST_OF_GOODS_SOLD = "cost_of_goods_sold", "Cost of Goods Sold"
        OPERATING_EXPENSE = "operating_expense", "Operating Expense"
        PAYROLL_EXPENSE = "payroll_expense", "Payroll Expense"
        SHIPPING_EXPENSE = "shipping_expense", "Shipping & Freight Expense"
        PROCESSING_FEE_EXPENSE = "processing_fee_expense", "Merchant Processing Fees"
        TAX_EXPENSE = "tax_expense", "Tax Expense"
        DEPRECIATION_EXPENSE = "depreciation_expense", "Depreciation Expense"
        OTHER_EXPENSE = "other_expense", "Other Expense"

    #: Which side increases the balance, per classification. Asset and expense
    #: accounts are debit-normal; everything else is credit-normal.
    DEBIT_NORMAL_TYPES = frozenset({AccountType.ASSET, AccountType.EXPENSE})

    #: Which classification each subtype belongs to. Without this the two
    #: fields drift independently and an "income / bank" account is storable —
    #: nonsense that no report can render sensibly.
    SUBTYPE_FAMILY = {
        AccountSubType.BANK: AccountType.ASSET,
        AccountSubType.ACCOUNTS_RECEIVABLE: AccountType.ASSET,
        AccountSubType.OTHER_CURRENT_ASSET: AccountType.ASSET,
        AccountSubType.INVENTORY: AccountType.ASSET,
        AccountSubType.UNDEPOSITED_FUNDS: AccountType.ASSET,
        AccountSubType.FIXED_ASSET: AccountType.ASSET,
        AccountSubType.ACCUMULATED_DEPRECIATION: AccountType.ASSET,
        AccountSubType.OTHER_ASSET: AccountType.ASSET,
        AccountSubType.ACCOUNTS_PAYABLE: AccountType.LIABILITY,
        AccountSubType.CREDIT_CARD: AccountType.LIABILITY,
        AccountSubType.OTHER_CURRENT_LIABILITY: AccountType.LIABILITY,
        AccountSubType.SALES_TAX_PAYABLE: AccountType.LIABILITY,
        AccountSubType.LONG_TERM_LIABILITY: AccountType.LIABILITY,
        AccountSubType.EQUITY: AccountType.EQUITY,
        AccountSubType.RETAINED_EARNINGS: AccountType.EQUITY,
        AccountSubType.OPENING_BALANCE_EQUITY: AccountType.EQUITY,
        AccountSubType.SALES_INCOME: AccountType.INCOME,
        AccountSubType.SERVICE_INCOME: AccountType.INCOME,
        AccountSubType.SHIPPING_INCOME: AccountType.INCOME,
        AccountSubType.DISCOUNTS_REFUNDS: AccountType.INCOME,
        AccountSubType.OTHER_INCOME: AccountType.INCOME,
        AccountSubType.COST_OF_GOODS_SOLD: AccountType.EXPENSE,
        AccountSubType.OPERATING_EXPENSE: AccountType.EXPENSE,
        AccountSubType.PAYROLL_EXPENSE: AccountType.EXPENSE,
        AccountSubType.SHIPPING_EXPENSE: AccountType.EXPENSE,
        AccountSubType.PROCESSING_FEE_EXPENSE: AccountType.EXPENSE,
        AccountSubType.TAX_EXPENSE: AccountType.EXPENSE,
        AccountSubType.DEPRECIATION_EXPENSE: AccountType.EXPENSE,
        AccountSubType.OTHER_EXPENSE: AccountType.EXPENSE,
    }

    company = models.ForeignKey(
        "core.Company", on_delete=models.CASCADE, related_name="accounts"
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
    )

    account_number = models.CharField(max_length=50, blank=True, db_index=True)
    name = models.CharField(max_length=255)
    #: Colon-delimited path from the root ("Expenses:Utilities:Electric").
    full_name = models.CharField(max_length=1024, blank=True, db_index=True)
    description = models.TextField(blank=True)

    account_type = models.CharField(max_length=20, choices=AccountType.choices)
    account_subtype = models.CharField(
        max_length=40, choices=AccountSubType.choices, blank=True
    )

    is_active = models.BooleanField(default=True)
    currency = models.CharField(max_length=3, default="USD")

    #: ⚠ PROVENANCE ONLY — not posted to the ledger. This is the balance QBO
    #: reported at export time, kept so an import can be reconciled against the
    #: source. It is NOT represented by any JournalLine, so a trial balance
    #: computed from journal lines alone will not equal QBO's balance sheet
    #: until opening entries are posted (a deliberate separate step: inventing
    #: Opening Balance Equity entries during an import would silently
    #: manufacture history nobody reviewed).
    opening_balance = models.DecimalField(
        max_digits=_MAX_DIGITS, decimal_places=_DECIMAL_PLACES, default=ZERO
    )
    opening_balance_date = models.DateField(null=True, blank=True)

    # --- QBO provenance. Raw, unmapped, never interpreted by reports. ---
    qbo_id = models.CharField(max_length=100, blank=True, db_index=True)
    qbo_account_type = models.CharField(max_length=100, blank=True)
    qbo_detail_type = models.CharField(max_length=100, blank=True)

    objects = CompanyScopedManager()

    class Meta:
        ordering = ["account_number", "full_name", "name"]
        constraints = [
            # Account numbers are optional in QBO (the setting is off by
            # default), so uniqueness only binds on the rows that have one.
            models.UniqueConstraint(
                fields=["company", "account_number"],
                condition=~models.Q(account_number=""),
                name="uniq_account_number_per_company",
            ),
            # The flattened path is what transaction exports reference, so it
            # has to be unambiguous within a company.
            models.UniqueConstraint(
                fields=["company", "full_name"],
                condition=~models.Q(full_name=""),
                name="uniq_account_full_name_per_company",
            ),
            models.UniqueConstraint(
                fields=["company", "qbo_id"],
                condition=~models.Q(qbo_id=""),
                name="uniq_account_qbo_id_per_company",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "account_type"]),
            models.Index(fields=["company", "account_subtype"]),
        ]

    def __str__(self):
        label = self.full_name or self.name
        return f"{self.account_number} {label}".strip()

    @property
    def is_debit_normal(self):
        """True when a debit increases this account's balance."""
        return self.account_type in self.DEBIT_NORMAL_TYPES

    @property
    def normal_balance(self):
        return "debit" if self.is_debit_normal else "credit"

    def build_full_name(self):
        """Recompute the colon path from the parent chain.

        Walks upward with a visited-set guard so a cycle introduced by bad data
        raises instead of recursing forever.
        """
        parts = [self.name]
        seen = {self.pk} if self.pk else set()
        node = self.parent
        while node is not None:
            if node.pk in seen:
                raise ValidationError(
                    f"Account parent chain contains a cycle at {node.pk}."
                )
            seen.add(node.pk)
            parts.append(node.name)
            node = node.parent
        return ":".join(reversed(parts))

    def clean(self):
        super().clean()
        if self.account_subtype:
            family = self.SUBTYPE_FAMILY.get(self.account_subtype)
            if family is not None and family != self.account_type:
                raise ValidationError(
                    {
                        "account_subtype": (
                            f"Subtype {self.account_subtype!r} belongs to "
                            f"{family} accounts, not {self.account_type}."
                        )
                    }
                )
        if self.parent is not None:
            if self.parent.pk == self.pk:
                raise ValidationError({"parent": "An account cannot be its own parent."})
            if self.parent.company_id != self.company_id:
                raise ValidationError(
                    {"parent": "Parent account belongs to a different company."}
                )
            if self.parent.account_type != self.account_type:
                raise ValidationError(
                    {
                        "parent": (
                            "Sub-accounts must share the parent's account type "
                            f"({self.parent.account_type})."
                        )
                    }
                )

    def save(self, *args, **kwargs):
        # full_name is the importer's lookup key AND a unique constraint, so a
        # rename that leaves it stale makes the next import miss this account
        # and build a duplicate tree beside it. Recompute on any change to the
        # inputs, and carry the new prefix down to the descendants.
        previous = None
        if self.pk:
            previous = (
                type(self)._base_manager.filter(pk=self.pk)
                .values("name", "parent_id", "full_name")
                .first()
            )

        renamed = previous is not None and (
            previous["name"] != self.name or previous["parent_id"] != self.parent_id
        )
        if not self.full_name or renamed:
            self.full_name = self.build_full_name()

        moved = bool(
            previous
            and previous["full_name"]
            and previous["full_name"] != self.full_name
        )
        if not moved:
            super().save(*args, **kwargs)
            return

        # The rename and the descendant rewrite are one change. Split across
        # two units of work, a unique-path collision partway down leaves the
        # parent renamed and half the subtree pointing at a path that no longer
        # exists — a corrupt tree that the next import then duplicates.
        with transaction.atomic():
            super().save(*args, **kwargs)
            self._rewrite_descendant_paths()

    def _rewrite_descendant_paths(self):
        """Rebuild every descendant's ``full_name`` after this one moved.

        Walks top-down and derives each path as ``parent.full_name + ":" +
        name``, which is the definition of the field. String surgery on the old
        prefix looks equivalent and is not: a descendant whose stored path had
        drifted (hand-edited, or written by an older code path) fails the
        prefix test, and the fallback would rebuild it from the moved node plus
        the leaf name alone — silently discarding every intermediate segment.
        Deriving from the chain cannot do that, and it repairs drift instead of
        propagating it.

        Iterative so a deep chart cannot blow the stack. Writes ``full_name``
        only; no other field is touched.
        """
        manager = type(self)._base_manager
        queue = [(self.pk, self.full_name)]
        while queue:
            parent_pk, parent_path = queue.pop()
            for child in manager.filter(parent_id=parent_pk):
                child_path = f"{parent_path}:{child.name}"
                if child_path != child.full_name:
                    manager.filter(pk=child.pk).update(full_name=child_path)
                queue.append((child.pk, child_path))


class FiscalPeriod(BaseModel):
    """A reporting window. Entries are stamped with the period they fall in."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        LOCKED = "locked", "Locked"

    company = models.ForeignKey(
        "core.Company", on_delete=models.CASCADE, related_name="fiscal_periods"
    )
    name = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.OPEN
    )
    closed_at = models.DateTimeField(null=True, blank=True)

    objects = CompanyScopedManager()

    class Meta:
        ordering = ["-start_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "name"], name="uniq_fiscal_period_name_per_company"
            ),
            models.UniqueConstraint(
                fields=["company", "start_date", "end_date"],
                name="uniq_fiscal_period_range_per_company",
            ),
            models.CheckConstraint(
                # Django 6 removed the `check=` kwarg — it is `condition=` now.
                # Passing `check=` is a TypeError at django.setup(), i.e. the
                # whole ERP fails to import. Do not "fix" this back.
                condition=models.Q(end_date__gte=models.F("start_date")),
                name="fiscal_period_end_after_start",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "start_date", "end_date"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.start_date} – {self.end_date})"

    @property
    def is_editable(self):
        return self.status == self.Status.OPEN

    def contains(self, when):
        return self.start_date <= when <= self.end_date


class JournalEntry(BaseModel):
    """A balanced transaction header. Its legs live on ``lines``."""

    class EntrySource(models.TextChoices):
        MANUAL = "manual", "Manual"
        QBO_IMPORT = "qbo_import", "QBO Import"
        ORDER = "order", "Sales Order"
        PAYMENT = "payment", "Payment"
        REFUND = "refund", "Refund"
        DISCOUNT = "discount", "Discount"
        LANDED_COST = "landed_cost", "Landed Cost"
        PAYROLL = "payroll", "Payroll"
        PURCHASE = "purchase", "Purchase"
        SYSTEM = "system", "System"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        POSTED = "posted", "Posted"
        VOID = "void", "Void"

    company = models.ForeignKey(
        "core.Company", on_delete=models.CASCADE, related_name="journal_entries"
    )
    fiscal_period = models.ForeignKey(
        FiscalPeriod,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="journal_entries",
    )

    entry_number = models.CharField(max_length=50, blank=True, db_index=True)
    entry_date = models.DateField(db_index=True)
    memo = models.TextField(blank=True)

    entry_source = models.CharField(
        max_length=20, choices=EntrySource.choices, default=EntrySource.MANUAL
    )
    #: Free-text pointer back to whatever produced this — a QBO doc number, an
    #: order number, an invoice number. Not unique; several entries can share it.
    source_reference = models.CharField(max_length=255, blank=True, db_index=True)
    #: The customer/vendor/payee this entry is for, when every line agrees on
    #: one. Part of the entry's identity, not decoration: document numbers do
    #: get reused (two vendors paid with the same check number on the same
    #: day), and without this the two transactions collapse into one entry that
    #: still balances — so nothing downstream can tell it is wrong.
    entity_name = models.CharField(max_length=255, blank=True, db_index=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.POSTED
    )

    created_by = models.ForeignKey(
        "core.UserProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="journal_entries",
    )

    # --- QBO provenance + import idempotency ---
    qbo_id = models.CharField(max_length=100, blank=True, db_index=True)
    qbo_txn_type = models.CharField(max_length=100, blank=True)
    #: Deterministic fingerprint of the source row-group, so re-running an
    #: import updates rather than duplicates even when QBO exports carry no id.
    #: Computed by ``accounting.qbo.importer`` — see ``entry_fingerprint``.
    import_hash = models.CharField(max_length=64, blank=True, db_index=True)
    #: Idempotency key for operational postings (ED-6): exactly
    #: ``f"{entry_source}:{source_reference}"``, unique per company when set.
    #: QBO flows keep using ``import_hash``; ``source_reference`` alone stays
    #: non-unique by design (check numbers get reused).
    posting_key = models.CharField(max_length=320, blank=True, db_index=True)

    objects = CompanyScopedManager()

    class Meta:
        ordering = ["-entry_date", "-created_at"]
        verbose_name_plural = "journal entries"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "entry_number"],
                condition=~models.Q(entry_number=""),
                name="uniq_journal_entry_number_per_company",
            ),
            models.UniqueConstraint(
                fields=["company", "qbo_id"],
                condition=~models.Q(qbo_id=""),
                name="uniq_journal_entry_qbo_id_per_company",
            ),
            models.UniqueConstraint(
                fields=["company", "import_hash"],
                condition=~models.Q(import_hash=""),
                name="uniq_journal_entry_import_hash_per_company",
            ),
            models.UniqueConstraint(
                fields=["company", "posting_key"],
                condition=~models.Q(posting_key=""),
                name="uniq_journal_entry_posting_key_per_company",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "entry_date"]),
            models.Index(fields=["company", "entry_source"]),
        ]

    def __str__(self):
        return f"{self.entry_number or self.import_hash[:8]} — {self.entry_date}"

    # --- fiscal-period guard ---

    def _reject_closed_period(self):
        """Raise unless the entry's fiscal period (if any) is open.

        Split out of ``clean()``/``save()`` so both paths share one check.
        Reads the status straight from the database rather than trusting an
        attached instance, which may predate a close.
        """
        if self.fiscal_period_id is None:
            return
        row = (
            FiscalPeriod._base_manager.filter(pk=self.fiscal_period_id)
            .values_list("name", "status")
            .first()
        )
        if row is not None and row[1] != FiscalPeriod.Status.OPEN:
            name, status = row
            raise ValidationError(
                f"Journal entry {self} falls in fiscal period "
                f"{name}, which is {status} — "
                "post to an open period instead."
            )

    def clean(self):
        super().clean()
        self._reject_closed_period()

    def save(self, *args, **kwargs):
        # ORM saves never call clean(), so enforce here too — otherwise any
        # direct writer (importer, shell, data fix) posts into closed periods
        # silently. full_clean() wholesale would also run uniqueness checks
        # that import flows deliberately handle themselves; the targeted
        # period check is the only validation a save must guarantee.
        self._reject_closed_period()
        super().save(*args, **kwargs)

    # --- balance ---

    def totals(self):
        """Return ``(total_debit, total_credit)`` over this entry's lines."""
        agg = self.lines.aggregate(
            debit=models.Sum("debit_amount"), credit=models.Sum("credit_amount")
        )
        return (agg["debit"] or ZERO), (agg["credit"] or ZERO)

    @property
    def is_balanced(self):
        debit, credit = self.totals()
        return debit == credit

    def validate_balanced(self):
        """Raise unless debits equal credits and at least one line exists.

        A database CHECK cannot span sibling rows, so this is the enforcement
        point. Every writer in this package calls it inside the same
        transaction that creates the lines.
        """
        debit, credit = self.totals()
        if debit == ZERO and credit == ZERO:
            raise ValidationError(
                f"Journal entry {self} has no lines with a non-zero amount."
            )
        if debit != credit:
            raise ValidationError(
                f"Journal entry {self} is unbalanced: "
                f"debits {debit} != credits {credit}."
            )


class JournalLine(BaseModel):
    """One leg of a journal entry: an amount on exactly one side of one account.

    Line items elsewhere in this ERP (``OrderLineItem``, ``QuoteLineItem``) hang
    off their parent with no company FK of their own; this follows that. Scope
    a query with ``JournalLine.objects.filter(entry__company=…)``.
    """

    entry = models.ForeignKey(
        JournalEntry, on_delete=models.CASCADE, related_name="lines"
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="journal_lines"
    )

    sort_order = models.IntegerField(default=0)
    description = models.CharField(max_length=500, blank=True)

    debit_amount = models.DecimalField(
        max_digits=_MAX_DIGITS, decimal_places=_DECIMAL_PLACES, default=ZERO
    )
    credit_amount = models.DecimalField(
        max_digits=_MAX_DIGITS, decimal_places=_DECIMAL_PLACES, default=ZERO
    )

    #: QBO's "Name" column — the customer/vendor/employee on the line. Stored as
    #: text rather than an FK: the GL must survive a customer record being
    #: merged or deleted, and an import runs before those records necessarily
    #: exist.
    entity_name = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["entry", "sort_order"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(debit_amount__gte=0)
                & models.Q(credit_amount__gte=0),
                name="journal_line_amounts_non_negative",
            ),
            # Exactly one side carries the amount. A line with both sides zero
            # is meaningless in double-entry and would silently unbalance a
            # report; importers drop such rows rather than writing them.
            models.CheckConstraint(
                condition=(
                    models.Q(debit_amount__gt=0, credit_amount=0)
                    | models.Q(debit_amount=0, credit_amount__gt=0)
                ),
                name="journal_line_exactly_one_side",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "entry"]),
        ]

    def clean(self):
        """Reject a line whose account belongs to a different company.

        Nothing at the database level can catch this — the company lives two
        hops away, on ``entry`` and on ``account`` — so a cross-tenant line is
        storable unless something checks. It posts one company's money into
        another's books and both ledgers are then wrong.
        """
        super().clean()
        entry = self.entry if self.entry_id else None
        account = self.account if self.account_id else None
        if entry and account and entry.company_id != account.company_id:
            raise ValidationError(
                {
                    "account": (
                        "Account belongs to a different company than the "
                        "journal entry."
                    )
                }
            )

    def __str__(self):
        side = f"DR {self.debit_amount}" if self.debit_amount else f"CR {self.credit_amount}"
        return f"{self.account} {side}"

    @property
    def signed_amount(self):
        """Debit-positive amount: ``debit - credit``."""
        return (self.debit_amount or ZERO) - (self.credit_amount or ZERO)


class DocumentPostingRule(BaseModel):
    """Deterministic chart-of-accounts mapping (AD-13).

    One row per (company, doc_type) naming the accounts each posting function
    debits/credits. The LLM CoA-mapping purpose accelerates this explicit
    schema — it never replaces it: a posting with no rule fails loudly with a
    named error instead of guessing.

    Required accounts vary by doc_type; ``clean()`` enforces the set. All
    accounts must belong to ``company`` (same two-hop rationale as
    ``JournalLine.clean``).
    """

    class DocType(models.TextChoices):
        SALES_INVOICE = "sales_invoice", "Sales Invoice"
        PAYMENT = "payment", "Payment"
        REFUND = "refund", "Refund"
        PURCHASE_RECEIPT = "purchase_receipt", "Purchase Receipt"

    company = models.ForeignKey(
        "core.Company", on_delete=models.CASCADE, related_name="posting_rules"
    )
    doc_type = models.CharField(max_length=20, choices=DocType.choices)

    receivable_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, blank=True,
        related_name="posting_rules_receivable",
    )
    revenue_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, blank=True,
        related_name="posting_rules_revenue",
    )
    tax_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, blank=True,
        related_name="posting_rules_tax",
    )
    discount_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, blank=True,
        related_name="posting_rules_discount",
    )
    shipping_income_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, blank=True,
        related_name="posting_rules_shipping_income",
    )
    cash_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, blank=True,
        related_name="posting_rules_cash",
    )
    fee_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, blank=True,
        related_name="posting_rules_fee",
    )
    inventory_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, blank=True,
        related_name="posting_rules_inventory",
    )
    payable_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, blank=True,
        related_name="posting_rules_payable",
    )

    objects = CompanyScopedManager()

    REQUIRED = {
        DocType.SALES_INVOICE: (
            "receivable_account", "revenue_account", "tax_account",
            "discount_account", "shipping_income_account",
        ),
        DocType.PAYMENT: ("receivable_account", "cash_account", "fee_account"),
        DocType.REFUND: ("receivable_account", "cash_account"),
        DocType.PURCHASE_RECEIPT: ("inventory_account", "payable_account"),
    }

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "doc_type"],
                name="uniq_posting_rule_per_company_doc",
            ),
        ]

    def __str__(self):
        return f"{self.company} {self.get_doc_type_display()} rule"

    def clean(self):
        super().clean()
        missing = [
            f for f in self.REQUIRED.get(self.doc_type, ())
            if getattr(self, f"{f}_id", None) is None
        ]
        if missing:
            raise ValidationError(
                f"Posting rule for {self.doc_type} is missing accounts: "
                + ", ".join(missing)
            )
        for f in self.REQUIRED.get(self.doc_type, ()):
            account = getattr(self, f, None)
            if account is not None and account.company_id != self.company_id:
                raise ValidationError(
                    {f: "Account belongs to a different company than the rule."}
                )


class PaymentProviderPostingOverride(BaseModel):
    """Per-provider cash (and fee) accounts for payment and refund postings.

    The ``payment`` and ``refund`` rules name one cash account per company.
    That is right for a holding account (card captures and marketplace sales
    are not spendable until the payout lands), but money a provider holds as
    cash — a PayPal balance, a bank transfer — is cash the moment it arrives.
    An override says: payments through ``provider`` debit (refunds credit)
    ``cash_account`` instead, and charge their processing fee to
    ``fee_account`` when one is set (else the rule's). ``provider`` is a
    ``payments.Payment.PaymentProvider`` value; a provider with no override
    uses the rule unchanged.
    """

    company = models.ForeignKey(
        "core.Company", on_delete=models.CASCADE,
        related_name="provider_posting_overrides",
    )
    provider = models.CharField(max_length=20)
    cash_account = models.ForeignKey(
        Account, on_delete=models.PROTECT,
        related_name="provider_overrides_cash",
    )
    fee_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, blank=True,
        related_name="provider_overrides_fee",
    )

    objects = CompanyScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "provider"],
                name="uniq_provider_posting_override_per_company",
            ),
        ]

    def __str__(self):
        return f"{self.company} {self.provider} cash → {self.cash_account}"

    @staticmethod
    def known_providers():
        from payments.models import Payment

        return [value for value, _label in Payment.PaymentProvider.choices]

    def clean(self):
        super().clean()
        if self.provider not in self.known_providers():
            raise ValidationError(
                {"provider": f"Unknown payment provider {self.provider!r}; "
                             f"one of {', '.join(self.known_providers())}."}
            )
        for f in ("cash_account", "fee_account"):
            account = getattr(self, f, None)
            if account is not None and account.company_id != self.company_id:
                raise ValidationError(
                    {f: "Account belongs to a different company than the override."}
                )
