# `accounting` — general ledger

A double-entry general ledger with an import pipeline for books kept in
QuickBooks Online.

## Models

| Model | What it is |
|---|---|
| `Account` | Chart of accounts, one tree per company |
| `FiscalPeriod` | Reporting window entries are stamped against |
| `JournalEntry` | A balanced transaction header |
| `JournalLine` | One debit-or-credit leg against one account |
| `DocumentPostingRule` | Which accounts a sales invoice, payment, refund or purchase receipt posts to, per company |
| `PaymentProviderPostingOverride` | Per-provider cash (and fee) account for payments and refunds, overriding the rule |

All four are company-scoped (`CompanyScopedManager`, non-null `company` FK)
except `JournalLine`, which scopes through its parent — the same shape as
`OrderLineItem`/`QuoteLineItem` elsewhere in this ERP. Query lines with
`JournalLine.objects.filter(entry__company=…)`.

### Two axes on an account, deliberately separate

- **`account_type`** — the accounting classification: asset / liability /
  equity / income / expense. Five values, constrained. The *only* thing that
  decides normal balance (`Account.is_debit_normal`), and what reports key off.
- **`account_subtype`** — the finer bucket (bank, accounts receivable, cost of
  goods sold…). Curated, not exhaustive, and allowed to be blank.

QBO's own vocabulary is preserved verbatim next to them in `qbo_account_type`
and `qbo_detail_type`, so an import is lossless even when Intuit ships a detail
type this codebase has never seen.

### Balance is enforced in code, not by a constraint

A database `CHECK` cannot see sibling rows, so `JournalEntry.validate_balanced()`
is the enforcement point. **Every write path must check it.** Today that means
the QBO importer (which calls it directly) and the Django admin (which enforces
the same rule in `JournalLineInlineFormSet.clean()`). A new write path that
skips it will happily store an unbalanced ledger — nothing underneath will
catch it. The admin also rejects a line whose account belongs to a different
company than the entry, which is likewise invisible to the database: the
company sits two FK hops away.

What the DB *does* enforce, per line:

- both amounts `>= 0` (`journal_line_amounts_non_negative`)
- exactly one side strictly positive (`journal_line_exactly_one_side`)

So a zero-on-both-sides line is impossible by construction. The importer drops
such rows rather than trying to write them.

> **Django 6 note.** These are `CheckConstraint(condition=…)`. The `check=`
> kwarg was **removed** in Django 6 — passing it is a `TypeError` at
> `django.setup()`, i.e. the whole ERP fails to import. Do not "fix" it back.

## QBO import

    python manage.py import_qbo_export \
        --company "Example Supply, Inc." \
        --chart-of-accounts exports/accounts.xlsx \
        --transactions exports/journal.xlsx \
        --dry-run

`--company` is required and never guessed — accounts and entries are
company-scoped, and the wrong one posts an entire ledger to the wrong books.
It accepts a name (case-insensitive) or a UUID, and refuses to proceed on an
ambiguous match.

`--dry-run` runs the real import inside a transaction and rolls it back, so the
printed report is exactly what a live run would produce.

`--dayfirst` reads ambiguous slash dates as DD/MM/YYYY. This matters: `03/04/2025`
is 4 March in a US export and 3 April in a UK one, and **nothing in the file
says which it is**. ISO dates are never reinterpreted.

### Three layers

    readers.py   .csv/.xlsx → normalized, header-keyed rows
    mapping.py   QBO account vocabulary → model choices
    importer.py  rows → Accounts and balanced JournalEntries

`readers.py` skips the report preamble (QBO leads with company name, report
title and date range before the real header) and resolves logical field names
through an alias table, so the importer never hard-codes a column spelling.

### Guarantees

- **Idempotent, including for corrections.** Re-running over the same file
  updates in place. Matching is on the transaction's stable *identity* — QBO's
  own id when present, otherwise `(date, doc number, type)` — never on its
  amounts. Keying on the amounts would mean a corrected export (same invoice,
  new total) failed to match and inserted a second entry beside the stale one,
  double-counting the revenue. `import_hash` (a sha256 over the sorted legs)
  remains as a change-detector and as the fallback key for rows with no
  identity at all. It is deliberately *not* `hash()`, which is
  PYTHONHASHSEED-salted and would make every re-run duplicate the ledger.
- **Blank `Num` still imports.** Deposits, bank fees, transfers and journal
  entries routinely ship without a document number; those group on
  `(date, type, entity)`. Only rows with neither a reference nor an entity stay
  per-row — nothing distinguishes two unrelated same-day transactions there,
  and fusing them would invent a transaction that never happened.
- **Never writes an unbalanced entry.** A source group whose debits and credits
  disagree is collected into the report and skipped; the rest of the import
  still lands, and the command exits non-zero.
- **Never crosses tenants.** Every lookup and every create is company-scoped.
- **Negatives are folded, not stored.** QBO writes reversals as a negative in
  the debit column; a negative debit is a credit.
- **Fails loudly on an unknown account *type*, quietly on an unknown *detail
  type*.** An unclassified account cannot be reported on, so guessing would
  corrupt the P&L silently. An unmapped detail type loses nothing — the raw
  string survives on the model and the report lists it.

### Placeholder accounts

A transaction referencing an account the chart-of-accounts export never defined
gets an inactive stand-in numbered `9999-<8 hex>`, listed in the report. The
suffix is a sha256 of the path, so a second run reuses it instead of colliding
on the unique constraint.

### Known limits, stated rather than hidden

- **Opening balances are provenance, not postings.** `Account.opening_balance`
  is what QBO reported at export time, kept for reconciliation. No `JournalLine`
  represents it, so a trial balance computed from journal lines alone will not
  equal QBO's balance sheet until opening entries are posted. That is a
  deliberate separate step — manufacturing Opening Balance Equity entries
  during an import would fabricate history nobody reviewed.
- **US number format only.** `1.234,56` (European decimal comma) is *refused*,
  not guessed at — reading it as `1.23456` would understate by ~1000x silently.
  `--dayfirst` handles the date half of the same problem.
- **Grouping can fail in two opposite directions, and both are silent.**
  Under-splitting fuses distinct transactions into one entry that still
  balances, so nothing downstream can tell. Over-splitting shreds a real entry
  into unbalanced fragments and then blames the source for an imbalance that
  does not exist. The discriminators, both pinned by tests:
  split on entity only when **each entity's legs balance independently** (two
  vendors' payments do; the legs of one multi-party journal entry do not), and
  cut a reference-less group into **consecutive balanced runs** (two same-day
  bank fees separate; a three-leg deposit stays whole).
- **A bare account reference that matches several accounts is refused**, not
  resolved by index order. "Insurance" under both COGS and Expenses is an
  ordinary QBO chart, and guessing moves money between cost of sales and
  operating expense — changing reported gross margin with nothing in the report.
- **A flat re-import never flattens an existing tree.** QBO's default
  account-list export has no Full Name or Parent column; writing those rows
  over a chart built from a richer export would null every parent and shorten
  every path, and report it as a clean "3 updated". The hierarchy is only
  written when the source actually described one.
- **A document number alone is not an identity.** Check and reference numbers
  get reused, so an entry's identity is `(date, doc number, type, entity)`.
  Groups split by entity only when *every* leg names one and they differ —
  splitting on a partial match would shred ordinary invoices, where the
  receivable leg carries the customer and the tax leg does not.
- **Re-importing replaces an entry's lines wholesale**, so `JournalLine` primary
  keys are not stable across runs. Nothing references a line by id today; if
  something later does (reconciliation, attachments, the #304 signal wiring),
  this needs revisiting first.

## Posting rules

Operational documents post through `accounting.posting`, and the accounts they
hit come from a `DocumentPostingRule` per `(company, doc_type)`. There is no
default: a company with no rule for a document type cannot save a posting
document of that type (`NoPostingRule`, AD-13 — postings never guess accounts).
`seed_demo_data` builds rules for the demo chart; a real company sets its own:

```bash
python manage.py posting_rules show
python manage.py posting_rules set --doc-type sales_invoice \
    --receivable "Accounts receivable (A/R)" --revenue "Sales of Product Income" \
    --tax "Sales Tax Payable" --discount "Discounts given" --shipping-income "Shipping Income"
python manage.py posting_rules set --doc-type payment \
    --receivable "Accounts receivable (A/R)" --cash "Undeposited Funds" --fee "Stripe processing fees"
python manage.py posting_rules set --doc-type refund \
    --receivable "Accounts receivable (A/R)" --cash "Undeposited Funds"
python manage.py posting_rules set --doc-type purchase_receipt \
    --inventory "Inventory Asset" --payable "Accounts Payable (A/P)"
```

Accounts are referenced by number, full name or bare name; an ambiguous
reference is refused. `set` only replaces the accounts you name and refuses a
rule that would still be incomplete, so nothing half-written reaches the DB.

### Cash is per provider

The `payment`/`refund` rule names one cash account. That is right for money a
processor holds until payout — card captures and marketplace sales belong in a
holding account ("Undeposited Funds") until the payout reaches the bank — but
wrong for a provider whose balance is already spendable. A
`PaymentProviderPostingOverride` keyed on `Payment.payment_provider` swaps the
cash account, and the fee account when it names one:

```bash
python manage.py posting_rules set-provider --provider paypal \
    --cash "PayPal Account" --fee "Channel selling fees:PayPal fees"
python manage.py posting_rules set-provider --provider ebay \
    --cash "Undeposited Funds" --fee "Cost of goods sold:eBay fees - COGS"
python manage.py posting_rules set-provider --provider bank --cash "Mercury Checking (4848)"
```

A provider with no override uses the rule unchanged. Refunds mirror through
the same override, so money goes back out of the account it sat in. The
reconciliation page's Cash row sums the GL across every account the rule and
the overrides name. Golden does not post the payout itself (holding account →
bank); that is a deposit entry, recorded when the bank shows it.

## Not in this app (yet)

Auto-posting journal entries from `Order` / `Payment` / `PurchaseOrder` signals
is separate work. Nothing here hooks those
models, and adding that wiring is a deliberate decision with its own review —
not a drive-by.

## Testing

    cd apps/erp
    DJANGO_SETTINGS_MODULE=config.settings.test \
    DJANGO_SECRET_KEY=django-insecure-test-only \
      .venv/bin/python -m pytest accounting/ -q

⚠ `config.settings.test` **disables migrations**, so `makemigrations --check`
under it always reports "No changes detected" — including when the migration
genuinely drifts from the models. CI runs it that way, so CI cannot catch
drift. Check it properly against real migrations:

    DJANGO_SETTINGS_MODULE=config.settings.spike \
      .venv/bin/python manage.py makemigrations --check --dry-run
