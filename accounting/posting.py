"""Operational-to-GL posting choke-point (Step 1a, AD-2/ED-2/ED-3/ED-6).

Every new GL writer calls :func:`post_balanced_entry` — nothing below this
layer catches imbalance, closed periods, or cross-company legs (see
``accounting/models.py``). Dependency direction is one-way: operational apps
(orders/payments/purchasing) build legs and call in; this module never imports
them back.

Idempotency (ED-6): ``posting_key`` is exactly
``f"{entry_source}:{source_reference}"``, unique per company when set. A repeat
post returns the existing entry instead of duplicating.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Iterable, Tuple

from django.db import IntegrityError, transaction

from core.models import Company, DocumentSequence

from .models import Account, FiscalPeriod, JournalEntry, JournalLine

#: A leg is (account, debit, credit, description).
Leg = Tuple[Account, Decimal, Decimal, str]

_POST_RETRIES = 3


class PostingError(Exception):
    """Base class — a posting was refused before anything was written."""


class NoPostingRule(PostingError):
    """No DocumentPostingRule for this (company, doc_type)."""


def _period_for(company: Company, when: date):
    """Return the fiscal period containing ``when``, any status.

    A closed containing period must attach so ``save()`` rejects the post —
    returning None instead would silently post around the close.
    """
    return (
        FiscalPeriod.objects.filter(
            company=company, start_date__lte=when, end_date__gte=when
        )
        .order_by("-start_date")
        .first()
    )


def closed_period_for(company: Company, when: date):
    """The fiscal period containing ``when`` when it is closed or locked, else None.

    A document that will post (an invoice leaving draft, a payment, a
    receipt) asks this *before* it does anything irreversible: the model
    guard (``JournalEntry._reject_closed_period``) runs inside the posting
    transaction, after an email has gone out or a form has been accepted.
    """
    period = _period_for(company, when)
    if period is not None and period.status != FiscalPeriod.Status.OPEN:
        return period
    return None


def assert_period_open(company: Company, when: date, what: str = "This document") -> None:
    """Raise ``ValidationError`` naming the closed period ``when`` falls in."""
    from django.core.exceptions import ValidationError

    period = closed_period_for(company, when)
    if period is not None:
        raise ValidationError(
            f"{what} is dated {when}, in fiscal period {period.name}, which is "
            f"{period.get_status_display().lower()}: nothing was posted. Reopen the "
            "period under Accounting > Periods, or date the document in an open one."
        )


def _next_entry_number(company: Company) -> str:
    num = DocumentSequence.get_next_number(
        company, JournalEntry, "entry_number", "JE-"
    )
    return str(num)


def post_balanced_entry(
    *,
    company: Company,
    entry_date,
    entry_source: str,
    source_reference: str,
    legs: Iterable[Leg],
    memo: str = "",
    entity_name: str = "",
) -> JournalEntry:
    """Create one balanced entry + lines atomically, or return the existing one.

    Raises :class:`PostingError` (cross-company leg, imbalance via
    ``validate_balanced``) or :class:`ValidationError` (closed period, from the
    model guard). Retries ``entry_number`` collisions from concurrent posters.
    """
    legs = [
        (account, Decimal(debit or 0), Decimal(credit or 0), desc or "")
        for account, debit, credit, desc in legs
    ]
    # Zero-zero legs violate the exactly-one-side DB constraint and carry no
    # information — drop them. An entry left with no legs is rejected by
    # validate_balanced below, so empty entries are still impossible.
    legs = [leg for leg in legs if leg[1] != 0 or leg[2] != 0]
    for account, debit, credit, _desc in legs:
        if account.company_id != company.id:
            raise PostingError(
                f"Refusing to post {entry_source}:{source_reference}: account "
                f"{account} belongs to {account.company_id}, not {company.id}."
            )
    posting_key = f"{entry_source}:{source_reference}"

    for _attempt in range(_POST_RETRIES):
        try:
            with transaction.atomic():
                # Serialize postings per company (ED-2): balance is read then
                # written, so two concurrent posters must not interleave.
                Company.objects.select_for_update().get(pk=company.pk)
                existing = JournalEntry.objects.filter(
                    company=company, posting_key=posting_key
                ).first()
                if existing is not None:
                    return existing
                entry = JournalEntry(
                    company=company,
                    fiscal_period=_period_for(company, entry_date),
                    entry_number=_next_entry_number(company),
                    entry_date=entry_date,
                    memo=memo,
                    entry_source=entry_source,
                    source_reference=source_reference,
                    entity_name=entity_name,
                    posting_key=posting_key,
                    status=JournalEntry.Status.POSTED,
                )
                entry.save()  # closed-period guard runs here
                JournalLine.objects.bulk_create(
                    [
                        JournalLine(
                            entry=entry,
                            account=account,
                            debit_amount=debit,
                            credit_amount=credit,
                            description=desc,
                            sort_order=i,
                        )
                        for i, (account, debit, credit, desc) in enumerate(legs)
                    ]
                )
                entry.validate_balanced()
                return entry
        except IntegrityError:
            continue
    raise PostingError(
        f"Could not post {entry_source}:{source_reference} after "
        f"{_POST_RETRIES} attempts (entry_number contention)."
    )


def post_opening_balance(
    *,
    company: Company,
    account: Account,
    amount,
    entry_date=None,
    memo: str = "",
) -> JournalEntry:
    """Explicit human-confirmed opening-balance posting (ED-6).

    DR/CR the account against Opening Balance Equity — never part of any
    automatic flow. Positive amounts debit the account (assets/expenses);
    pass a negative amount to credit it (liabilities/equity/income).
    """
    from datetime import date as _date

    amount = Decimal(amount or 0)
    if amount == 0:
        raise PostingError("Opening balance of zero posts nothing.")
    equity = Account.objects.filter(
        company=company,
        account_subtype=Account.AccountSubType.OPENING_BALANCE_EQUITY,
    ).first()
    if equity is None:
        raise PostingError(
            f"No Opening Balance Equity account at {company}: create one "
            "(account_subtype=opening_balance_equity) before booking "
            "opening balances."
        )
    if account.company_id != company.id:
        raise PostingError(
            f"Refusing opening balance: account {account} belongs to "
            f"{account.company_id}, not {company.id}."
        )
    if amount > 0:
        legs = [
            (account, amount, Decimal("0"), "Opening balance"),
            (equity, Decimal("0"), amount, "Opening balance equity"),
        ]
    else:
        legs = [
            (equity, -amount, Decimal("0"), "Opening balance equity"),
            (account, Decimal("0"), -amount, "Opening balance"),
        ]
    return post_balanced_entry(
        company=company,
        entry_date=entry_date or _date.today(),
        entry_source=JournalEntry.EntrySource.SYSTEM,
        source_reference=f"opening:{account.account_number or account.pk}",
        memo=memo or f"Opening balance {account}",
        legs=legs,
    )


def reverse_entry(entry: JournalEntry, *, memo: str = "") -> JournalEntry:
    """Post the exact counter-entry. Never edits; refuses double reversals."""
    if entry.source_reference.startswith("reversal:"):
        raise PostingError(f"Refusing to reverse reversal {entry}.")
    mirror = []
    for line in entry.lines.all().order_by("sort_order"):
        mirror.append(
            (
                line.account,
                line.credit_amount,
                line.debit_amount,
                f"Reversal of {entry}: {line.description}",
            )
        )
    return post_balanced_entry(
        company=entry.company,
        entry_date=date.today(),
        entry_source=JournalEntry.EntrySource.SYSTEM,
        source_reference=f"reversal:{entry.posting_key or entry.pk}",
        memo=memo or f"Reversal of {entry}",
        entity_name=entry.entity_name,
        legs=mirror,
    )


def payment_accounts(company: Company, doc_type: str, provider: str = ""):
    """The (rule, cash_account, fee_account) a payment or refund posts with.

    The rule is the company's ``payment``/``refund`` DocumentPostingRule; a
    ``PaymentProviderPostingOverride`` for ``provider`` swaps the cash
    account, and the fee account when it names one. No override: the rule's
    accounts, unchanged. Missing rule still raises ``NoPostingRule``.
    """
    from .models import PaymentProviderPostingOverride

    rule = rule_for(company, doc_type)
    cash, fee = rule.cash_account, rule.fee_account
    if provider:
        override = PaymentProviderPostingOverride.objects.filter(
            company=company, provider=provider
        ).select_related("cash_account", "fee_account").first()
        if override is not None:
            cash = override.cash_account
            fee = override.fee_account or fee
    return rule, cash, fee


def rule_for(company: Company, doc_type: str):
    """Fetch the posting rule or fail loudly (AD-13 — never guess accounts)."""
    from .models import DocumentPostingRule

    try:
        return DocumentPostingRule.objects.get(company=company, doc_type=doc_type)
    except DocumentPostingRule.DoesNotExist:
        raise NoPostingRule(
            f"No posting rule for {doc_type} at {company}: create a "
            f"DocumentPostingRule (doc_type={doc_type}) naming the debit/credit "
            f"accounts before posting. Postings never guess accounts."
        )
