"""Build Accounts and balanced JournalEntries from parsed QBO export rows.

Two passes, both idempotent and both scoped to a single ``Company``:

    import_chart_of_accounts()   the Lists → Account export
    import_transactions()        Journal / Transaction Detail by Account

Design points worth knowing before changing anything here:

* **Every write carries ``company``.** ``Account``, ``FiscalPeriod`` and
  ``JournalEntry`` all have a non-null company FK, and every lookup is scoped
  too — an unscoped ``Account.objects.filter(name=…)`` would happily resolve to
  another tenant's account and silently post to the wrong books.

* **Idempotency is a content fingerprint, not a row number.** ``qbo_id`` is
  used when the export carries one; otherwise ``entry_fingerprint`` hashes the
  date, reference, type and the full sorted set of legs. Re-running an import
  over the same file updates in place instead of duplicating the ledger.

* **Unbalanced entries are never written.** They are collected into the
  ``ImportReport`` and skipped, so a malformed source group cannot corrupt the
  GL. Same for zero-amount legs, which the DB constraint rejects by design.

* **Negatives are folded, not stored.** QBO writes reversals as a negative in
  the debit column; a negative debit is a credit. ``_split_sides`` normalizes
  that before anything reaches the database.
"""

import hashlib
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from ..models import ZERO, Account, FiscalPeriod, JournalEntry, JournalLine
from .mapping import (
    QBO_DETAIL_TYPE_TO_SUBTYPE,
    UnknownAccountTypeError,
    normalize,
    resolve_account_subtype,
    resolve_account_type,
)
from .readers import (
    QBOFormatError,
    parse_bool,
    parse_date,
    parse_decimal,
    read_table,
)

#: Accounts the transaction pass invents for references the COA never defined.
#: Numbered in a reserved band so they are obvious in a trial balance, and
#: flagged inactive so they cannot be picked in the UI by accident.
PLACEHOLDER_PREFIX = "9999"


class AmbiguousAccountError(ValueError):
    """A bare account reference matches more than one account.

    Raised rather than resolved: picking one silently posts money to an
    account the source never named, and the two candidates are typically in
    different statements (cost of sales vs operating expense), so it shifts
    reported gross margin.
    """


def _q2(value):
    """Quantize to the 2dp scale of the money columns."""
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class ImportReport:
    """What an import pass did, and what it refused to do."""

    accounts_created: int = 0
    accounts_updated: int = 0
    entries_created: int = 0
    entries_updated: int = 0
    lines_created: int = 0
    placeholders_created: list = field(default_factory=list)
    unmapped_detail_types: set = field(default_factory=set)
    skipped_unbalanced: list = field(default_factory=list)
    skipped_closed_period: list = field(default_factory=list)
    skipped_zero: int = 0
    skipped_annotation: int = 0
    skipped_status: int = 0
    errors: list = field(default_factory=list)

    def as_lines(self):
        """Human-readable summary, one string per line."""
        out = [
            f"accounts:  {self.accounts_created} created, {self.accounts_updated} updated",
            f"entries:   {self.entries_created} created, {self.entries_updated} updated",
            f"lines:     {self.lines_created} written, {self.skipped_zero} zero-amount skipped",
        ]
        if self.skipped_status:
            out.append(
                f"status filter: {self.skipped_status} line(s) skipped — not "
                f"carrying an approved status"
            )
        if self.skipped_annotation:
            out.append(
                f"annotations: {self.skipped_annotation} single-cell divider/"
                f"banner row(s) ignored"
            )
        if self.placeholders_created:
            out.append(
                f"placeholder accounts ({len(self.placeholders_created)}): "
                + ", ".join(sorted(self.placeholders_created)[:20])
            )
        if self.unmapped_detail_types:
            out.append(
                f"unmapped QBO detail types ({len(self.unmapped_detail_types)}, "
                f"kept as provenance): " + ", ".join(sorted(self.unmapped_detail_types)[:20])
            )
        if self.skipped_unbalanced:
            out.append(f"UNBALANCED, not written ({len(self.skipped_unbalanced)}):")
            out.extend(f"  - {s}" for s in self.skipped_unbalanced[:20])
        if self.skipped_closed_period:
            out.append(
                f"CLOSED/LOCKED period, not written ({len(self.skipped_closed_period)}):"
            )
            out.extend(f"  - {s}" for s in self.skipped_closed_period[:20])
        if self.errors:
            out.append(f"ERRORS ({len(self.errors)}):")
            out.extend(f"  - {e}" for e in self.errors[:20])
        return out


# --- helpers --------------------------------------------------------------


def split_full_name(full_name):
    """Split "Expenses:Utilities:Electric" into its segments."""
    return [part.strip() for part in str(full_name).split(":") if part.strip()]


def entry_fingerprint(company_id, entry_date, source_reference, txn_type, legs):
    """Deterministic id for a transaction group, used when QBO gives no id.

    ``legs`` is an iterable of ``(account_key, debit, credit, entity)``. They
    are sorted before hashing so row order in the export cannot produce two
    different fingerprints for the same economic transaction.

    ``account_key`` must be a *stable* identifier — the account's path, not its
    primary key. A UUID changes if the chart is deleted and reloaded, which
    would silently break idempotency for exactly the entries that depend on the
    fingerprint (those with no document number).

    Unlike a plain ``hash()``, this is stable across processes — ``hash()`` is
    salted by PYTHONHASHSEED, so using it here would make re-runs create
    duplicates instead of matching.
    """
    payload = "|".join(
        [
            str(company_id),
            entry_date.isoformat() if entry_date else "",
            str(source_reference or ""),
            str(txn_type or ""),
        ]
        + sorted(
            f"{acct}~{_q2(debit)}~{_q2(credit)}~{entity or ''}"
            for acct, debit, credit, entity in legs
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _split_sides(debit, credit):
    """Fold a (debit, credit) pair into exactly one non-negative side.

    QBO uses a negative in either column for reversals, and occasionally fills
    both. Netting first means the result always satisfies the
    ``journal_line_exactly_one_side`` constraint, or is zero and gets dropped.
    """
    net = _q2(debit or ZERO) - _q2(credit or ZERO)
    if net > 0:
        return net, ZERO
    if net < 0:
        return ZERO, -net
    return ZERO, ZERO


# --- chart of accounts ----------------------------------------------------


class ChartOfAccountsImporter:
    """Loads the QBO account list into ``Account`` rows for one company."""

    def __init__(self, company, report=None):
        self.company = company
        self.report = report or ImportReport()
        # full_name (casefolded) -> Account, for parent resolution and for the
        # transaction pass. Scoped to this company by construction.
        self._by_full_name = {}
        self._by_name = {}
        self._ambiguous_names = set()

    def load_existing(self):
        """Index the company's current accounts so re-runs resolve parents."""
        for account in Account.objects.for_company(self.company):
            self._index(account)

    def _index(self, account):
        if account.full_name:
            self._by_full_name[account.full_name.casefold()] = account
        key = account.name.casefold()
        if key in self._by_name and self._by_name[key].pk != account.pk:
            # Two accounts share a leaf name ("Insurance" under both COGS and
            # Expenses is an ordinary QBO chart). Resolving a bare reference to
            # whichever was indexed first moves money between cost of sales and
            # opex with no error and no report entry.
            self._ambiguous_names.add(key)
        self._by_name.setdefault(key, account)

    def lookup(self, reference, leaf_fallback=True):
        """Find an account by full path, within this company.

        ``leaf_fallback`` allows matching on the bare leaf name — useful for
        transaction rows, which reference accounts loosely. It is switched OFF
        for parent resolution: binding a parent to any account that merely
        shares a leaf name can attach a sub-account to a parent of a different
        ``account_type``, which is exactly what ``Account.clean()`` forbids.
        """
        if not reference:
            return None
        key = str(reference).strip().casefold()
        if key in self._by_full_name:
            return self._by_full_name[key]
        if not leaf_fallback:
            return None
        leaf = key
        if key not in self._by_name:
            # "Expenses:Utilities:Electric" referenced as just "Electric".
            segments = split_full_name(reference)
            leaf = segments[-1].casefold() if segments else key
        if leaf in self._ambiguous_names:
            raise AmbiguousAccountError(
                f"{reference!r} matches more than one account in this chart. "
                f"Re-export with the Full Name column so the reference is "
                f"unambiguous."
            )
        return self._by_name.get(leaf)

    def import_rows(self, rows):
        """Create/update accounts from parsed rows.

        Rows are sorted by path depth so a parent always exists before the
        child that points at it — QBO exports are not guaranteed to be ordered.
        """
        staged = []
        for row in rows:
            try:
                staged.append(self._stage(row))
            except UnknownAccountTypeError as exc:
                self.report.errors.append(f"line {row.line_number}: {exc}")
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                self.report.errors.append(
                    f"line {row.line_number}: {type(exc).__name__}: {exc}"
                )
        staged = [s for s in staged if s is not None]
        staged.sort(key=lambda s: len(s["segments"]))

        seen_paths = {}
        for item in staged:
            # F8: two rows collapsing to one full_name would silently overwrite
            # each other (QBO's default export has no Full Name column, so
            # "Insurance" under COGS and under Expenses both flatten to
            # "Insurance"). Report it instead of losing one.
            previous = seen_paths.get(item["full_name"].casefold())
            if previous is not None:
                self.report.errors.append(
                    f"duplicate account path {item['full_name']!r} "
                    f"(QBO types {previous!r} and {item['qbo_account_type']!r}) — "
                    f"re-export with the Full Name or Parent column so the two "
                    f"can be told apart"
                )
                continue
            seen_paths[item["full_name"].casefold()] = item["qbo_account_type"]

            # F6: a unique-constraint violation here (duplicate account number)
            # would otherwise escape as an IntegrityError and roll back the
            # caller's whole transaction with a traceback instead of a report.
            try:
                with transaction.atomic():
                    self._write(item)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                number = item["account_number"] or "(no number)"
                self.report.errors.append(
                    f"account {item['full_name']!r} (number {number}): "
                    f"{type(exc).__name__}: {exc}"
                )
        return self.report

    def _stage(self, row):
        name = str(row["account_name"] or "").strip()
        explicit_full = str(row["account_full_name"] or "").strip()
        parent_ref = str(row["parent"] or "").strip()

        full_name = explicit_full or name
        if not explicit_full and parent_ref:
            full_name = f"{parent_ref}:{name}"

        segments = split_full_name(full_name) or ([name] if name else [])
        if not segments:
            return None

        qbo_type = str(row["account_type"] or "").strip()
        qbo_detail = str(row["detail_type"] or "").strip()

        account_type = resolve_account_type(qbo_type)
        subtype = resolve_account_subtype(qbo_type, qbo_detail)
        # Report the detail type whenever *it* was unrecognised — even though
        # the coarse type-level fallback filled the subtype in. Silence here
        # would hide Intuit adding a detail type we should be mapping.
        if qbo_detail and normalize(qbo_detail) not in QBO_DETAIL_TYPE_TO_SUBTYPE:
            self.report.unmapped_detail_types.add(qbo_detail)

        return {
            "segments": segments,
            "name": segments[-1],
            "full_name": ":".join(segments),
            "parent_path": ":".join(segments[:-1]),
            # Did the SOURCE carry hierarchy at all? QBO's default account-list
            # export has neither a Full Name nor a Parent column, so every row
            # flattens to its bare name. Writing that over an existing account
            # would null its parent and shorten its path — silently
            # dismantling a chart that a previous, richer export built.
            "has_hierarchy": bool(explicit_full or parent_ref),
            "account_number": str(row["account_number"] or "").strip(),
            "description": str(row["description"] or "").strip(),
            "account_type": account_type,
            "account_subtype": subtype,
            "qbo_account_type": qbo_type,
            "qbo_detail_type": qbo_detail,
            "qbo_id": str(row["qbo_id"] or "").strip(),
            "currency": (str(row["currency"] or "USD").strip() or "USD")[:3].upper(),
            "opening_balance": _q2(parse_decimal(row["balance"])),
            "is_active": parse_bool(row["active"], default=True),
        }

    def _write(self, item):
        parent = (
            self.lookup(item["parent_path"], leaf_fallback=False)
            if item["parent_path"]
            else None
        )

        defaults = {
            "name": item["name"],
            "account_number": item["account_number"],
            "description": item["description"],
            "account_type": item["account_type"],
            "account_subtype": item["account_subtype"],
            "qbo_account_type": item["qbo_account_type"],
            "qbo_detail_type": item["qbo_detail_type"],
            "currency": item["currency"],
            "opening_balance": item["opening_balance"],
            "is_active": item["is_active"],
        }

        # Prefer QBO's own id as the key; fall back to the path. Both lookups
        # are company-scoped, so a match can never come from another tenant.
        if item["qbo_id"]:
            lookup = {"company": self.company, "qbo_id": item["qbo_id"]}
        elif not item["has_hierarchy"] and item["account_number"]:
            # No path to match on, but a number is unique within the company.
            lookup = {
                "company": self.company,
                "account_number": item["account_number"],
            }
            defaults["qbo_id"] = item["qbo_id"]
        else:
            lookup = {"company": self.company, "full_name": item["full_name"]}
            defaults["qbo_id"] = item["qbo_id"]

        existing = Account.objects.filter(**lookup).first()

        # An account's QBO id is its durable identity for later imports, so it
        # must never be blanked or quietly reassigned by a poorer export. A
        # flat re-export with no ID column would otherwise wipe it, and the
        # next rich import — carrying the original id — would no longer match
        # and would build a duplicate account beside it.
        if "qbo_id" in defaults and existing is not None and existing.qbo_id:
            incoming = item["qbo_id"]
            if not incoming:
                defaults["qbo_id"] = existing.qbo_id
            elif incoming != existing.qbo_id:
                raise ValueError(
                    f"QBO id conflict: account {existing.full_name or existing.name!r} "
                    f"already carries id {existing.qbo_id!r} but this export gives "
                    f"{incoming!r}. Resolve it in QBO rather than letting the import "
                    f"pick one."
                )

        # Only write the hierarchy when the source actually described one.
        # Otherwise keep whatever the account already has (and fall back to the
        # flat name for a genuinely new account).
        if item["has_hierarchy"] or existing is None:
            defaults["full_name"] = item["full_name"]
            defaults["parent"] = parent
        elif existing.full_name:
            defaults["full_name"] = existing.full_name

        # full_clean() is what enforces the parent/subtype invariants; an
        # update_or_create() alone bypasses every one of them.
        probe = Account(company=self.company, **defaults)
        probe.full_clean(exclude=["id", "company"], validate_unique=False)

        account, created = Account.objects.update_or_create(
            **lookup, defaults=defaults
        )
        if item["qbo_id"] and account.qbo_id != item["qbo_id"]:
            account.qbo_id = item["qbo_id"]
            account.save(update_fields=["qbo_id"])

        if created:
            self.report.accounts_created += 1
        else:
            self.report.accounts_updated += 1
        self._index(account)
        return account

    def get_or_create_placeholder(self, reference, account_type=None):
        """Mint an inactive stand-in for an account the COA never defined.

        Numbered ``9999-<8 hex of sha256(path)>`` — deterministic across runs
        and processes, so a second import reuses the same placeholder instead
        of colliding with the unique constraint on (company, account_number).
        """
        if not reference or not str(reference).strip():
            raise ValueError(
                "Refusing to create a placeholder account with no name — the "
                "caller failed to resolve an account column."
            )
        existing = self.lookup(reference)
        if existing is not None:
            return existing

        segments = split_full_name(reference) or [str(reference).strip()]
        full_name = ":".join(segments)
        digest = hashlib.sha256(full_name.casefold().encode("utf-8")).hexdigest()[:8]

        account, created = Account.objects.update_or_create(
            company=self.company,
            full_name=full_name,
            defaults={
                "name": segments[-1],
                "account_number": f"{PLACEHOLDER_PREFIX}-{digest}",
                "account_type": account_type or Account.AccountType.EXPENSE,
                "account_subtype": "",
                "description": (
                    "Auto-created during QBO import: referenced by a transaction "
                    "but absent from the chart-of-accounts export. Reclassify."
                ),
                "is_active": False,
            },
        )
        if created:
            self.report.accounts_created += 1
            self.report.placeholders_created.append(full_name)
        self._index(account)
        return account


# --- transactions ---------------------------------------------------------


class TransactionImporter:
    """Groups transaction rows into balanced ``JournalEntry`` records."""

    def __init__(
        self, company, accounts, report=None, dayfirst=False, require_status=None
    ):
        self.company = company
        self.accounts = accounts  # a ChartOfAccountsImporter
        self.report = report or ImportReport()
        self.dayfirst = dayfirst
        self._periods = {}
        # Kept in both forms: normalized for matching, original for messages.
        self.require_status_labels = list(require_status or [])
        self.require_status = {normalize(s) for s in self.require_status_labels}

    def import_rows(self, rows):
        groups = self._group(rows)
        for key, legs in groups.items():
            try:
                self._write_group(key, legs)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                self.report.errors.append(f"entry {key}: {type(exc).__name__}: {exc}")
        return self.report

    def _group(self, rows):
        """Bucket rows into transactions.

        The key is (date, doc number, transaction type). QBO repeats all three
        on every leg of a transaction, so this reassembles the entry.

        Blank ``Num`` is common and must still import: deposits, bank fees,
        transfers and journal entries routinely ship without one. Those group
        on (date, type, entity) instead — the tightest identity available.
        A per-row key would make every such leg a one-leg group, therefore
        unbalanced by construction, therefore silently dropped: an export with
        a sparse Num column would import as nothing at all.

        Rows with no doc number AND no entity keep a per-row key, because
        nothing distinguishes two unrelated same-day transactions there and
        fusing them would invent a transaction that never happened.
        """
        # A status filter the file cannot answer must be fatal, never a no-op.
        # Silently importing everything because the column is missing is the
        # worst possible reading of "only post the approved rows".
        if self.require_status and rows and not rows[0].has("entry_status"):
            raise QBOFormatError(
                f"--require-status was given "
                f"({', '.join(self.require_status_labels)}) but the file has "
                f"no status column to check it against. Expected a header such "
                f"as 'Status' or 'Posting Status'; found: "
                f"{', '.join(sorted(rows[0].raw))}. Refusing to import rather "
                f"than posting every row."
            )

        groups = {}
        dated = 0
        undated = 0
        for index, row in enumerate(rows):
            # Section dividers carry their banner text in whichever column
            # happens to be first, which is usually the date column — so this
            # has to come before the date is parsed, or every divider is
            # reported as an unparseable date and the run exits non-zero on a
            # file that imported perfectly. An exit code that is always 1
            # stops being read.
            if row.is_annotation():
                self.report.skipped_annotation += 1
                continue
            try:
                entry_date = parse_date(row["date"], dayfirst=self.dayfirst)
            except Exception as exc:  # noqa: BLE001
                self.report.errors.append(f"line {row.line_number}: {exc}")
                continue
            if entry_date is None:
                undated += 1
                continue  # subtotal / spacer row
            dated += 1

            # Amounts get the same per-row treatment as dates. Parsed outside
            # this guard, one malformed money cell anywhere in the file aborts
            # the entire pass and rolls back every good row with it.
            try:
                debit, credit = _split_sides(
                    parse_decimal(row["debit"]), parse_decimal(row["credit"])
                )
                if debit == ZERO and credit == ZERO and row.has("amount"):
                    # Some exports carry a single signed "Amount" instead.
                    amount = _q2(parse_decimal(row["amount"]))
                    debit, credit = _split_sides(amount, ZERO)
            except Exception as exc:  # noqa: BLE001
                self.report.errors.append(f"line {row.line_number}: {exc}")
                continue
            if debit == ZERO and credit == ZERO:
                self.report.skipped_zero += 1
                continue

            account_ref = str(row["account_name"] or "").strip()
            if not account_ref:
                self.report.errors.append(
                    f"line {row.line_number}: no account column resolved — the "
                    f"export needs an 'Account' (or 'Distribution Account') "
                    f"column. Columns seen: {sorted(row.raw)}"
                )
                continue

            doc_number = str(row["doc_number"] or "").strip()
            txn_type = str(row["transaction_type"] or "").strip()
            entity = str(row["entity_name"] or "").strip()
            if doc_number:
                # Resolved in a second pass — see _split_shared_doc_numbers.
                # A document number is normally unique, but check numbers and
                # reference numbers do get reused, and fusing two vendors'
                # payments into one entry produces a *balanced* entry that
                # nothing downstream can tell is wrong.
                discriminator = ""
            elif entity:
                discriminator = f"entity:{entity.casefold()}"
            else:
                discriminator = f"row{index}"
            key = (entry_date, doc_number, txn_type, discriminator)
            groups.setdefault(key, []).append(
                {
                    "account_ref": account_ref,
                    "row_entity": entity,
                    "debit": debit,
                    "credit": credit,
                    "memo": str(row["memo"] or "").strip(),
                    "entity": str(row["entity_name"] or "").strip(),
                    "qbo_id": str(row["qbo_id"] or "").strip(),
                    "status": str(row["entry_status"] or "").strip(),
                    "line_number": row.line_number,
                }
            )
        if rows and dated == 0:
            # Every row was treated as a spacer. Overwhelmingly this means the
            # date column header is not in the alias table — in which case the
            # import writes nothing, reports nothing, and exits 0, which an
            # automated caller cannot tell apart from a real no-op.
            self.report.errors.append(
                f"No dated rows found in {len(rows)} data row(s) — the date "
                f"column was not recognized. Expected a header such as 'Date', "
                f"'Transaction Date' or 'Posting Date'."
            )
        return self._split_shared_doc_numbers(self._apply_status_filter(groups))

    def _apply_status_filter(self, groups):
        """Keep only entries whose every leg carries an approved status.

        An entry is approved as a whole or not at all. Filtering individual
        legs would leave the survivors unbalanced, and the entry would then be
        refused for an imbalance the source does not actually have — blaming
        the file for the flag. A *mixed* entry is therefore an error, not a
        silent partial: half an approved correcting entry is worse than none.

        ⚠ Matching is exact on the normalized value, NEVER a substring.
        "POST" is a substring of "PROPOSED - NOT POSTED", so a substring test
        would post precisely the rows a reviewer marked as not for posting.
        """
        if not self.require_status:
            return groups

        kept = {}
        for key, legs in groups.items():
            seen = {normalize(leg["status"]) for leg in legs}
            if seen <= self.require_status:
                kept[key] = legs
            elif seen & self.require_status:
                entry_date, doc_number, txn_type, _ = key
                self.report.errors.append(
                    f"{entry_date} {txn_type or '?'} {doc_number or '(no ref)'}: "
                    f"mixed status across the entry's legs "
                    f"({', '.join(sorted(repr(leg['status']) for leg in legs))}) — "
                    f"an entry is approved as a whole or not at all. "
                    f"Not imported."
                )
            else:
                self.report.skipped_status += len(legs)
        return kept

    @classmethod
    def _split_shared_doc_numbers(cls, groups):
        """Separate transactions that a shared grouping key ran together.

        Two opposite ways to get this wrong, and both are silent:

        * **Under-splitting.** Document numbers get reused — two vendors paid
          with the same check number on one day — and the fused entry still
          balances, so nothing downstream can tell it is one transaction where
          there were two.
        * **Over-splitting.** A genuine journal entry may name a different
          customer or vendor on each line (inter-customer reclass, payroll
          JEs). Cutting that on entity leaves one-leg fragments that are
          unbalanced by construction, so the whole entry is dropped and
          reported as a source imbalance that does not exist — sending the
          operator after a non-bug while the transaction is missing from the GL.

        The discriminator is whether the parts stand on their own: split by
        entity only when every leg names one and **each entity's legs balance
        independently**. Two vendors' payments do; the legs of a single
        multi-party journal entry do not.

        Groups with no document number get a second pass — see
        ``_split_balanced_runs``.
        """
        out = {}
        for key, legs in groups.items():
            for sub_key, sub_legs in cls._split_by_entity(key, legs):
                if sub_key[1]:  # has a document number
                    out[sub_key] = sub_legs
                    continue
                # No reference to separate them: fall back to source order.
                for run_key, run_legs in cls._split_balanced_runs(sub_key, sub_legs):
                    out[run_key] = run_legs
        return out

    @staticmethod
    def _split_by_entity(key, legs):
        entities = {leg["row_entity"] for leg in legs}
        if len(entities) <= 1 or not all(leg["row_entity"] for leg in legs):
            return [(key, legs)]

        by_entity = {}
        for leg in legs:
            by_entity.setdefault(leg["row_entity"].casefold(), []).append(leg)

        balanced = all(
            _q2(sum((x["debit"] for x in group), ZERO))
            == _q2(sum((x["credit"] for x in group), ZERO))
            for group in by_entity.values()
        )
        if not balanced:
            # One transaction that happens to name several parties.
            return [(key, legs)]
        return [
            ((*key[:3], f"entity:{entity}"), group)
            for entity, group in by_entity.items()
        ]

    @staticmethod
    def _split_balanced_runs(key, legs):
        """Cut a reference-less group into consecutive balanced runs.

        QBO writes a transaction's legs contiguously, so a run that balances is
        a complete transaction. Without this, two same-day bank fees from the
        same payee — neither carrying a Num — merge into one four-line entry
        that balances and therefore raises nothing, while per-transaction bank
        reconciliation quietly stops working.

        Anything left over at the end is emitted as-is, so a genuinely
        unbalanced tail is still reported rather than hidden.
        """
        runs = []
        current = []
        debit = credit = ZERO
        for leg in legs:
            current.append(leg)
            debit += leg["debit"]
            credit += leg["credit"]
            if _q2(debit) == _q2(credit) and debit != ZERO:
                runs.append(current)
                current = []
                debit = credit = ZERO
        if current:
            runs.append(current)
        if len(runs) <= 1:
            return [(key, legs)]
        return [((*key[:3], f"{key[3]}#run{i}"), run) for i, run in enumerate(runs)]

    def _fiscal_period(self, entry_date):
        """Resolve the company's period containing ``entry_date``.

        Periods are not auto-created: inventing them would fabricate a
        reporting calendar nobody chose. An entry outside every defined period
        is written with ``fiscal_period=None``, which is valid.
        """
        # Keyed by the exact date. A (year, month) key silently assigns every
        # entry in a month to whichever period the first lookup happened to
        # hit — wrong for semi-monthly or 4-4-5 fiscal calendars, and wrong
        # without any error.
        cache_key = entry_date
        if cache_key in self._periods:
            return self._periods[cache_key]
        period = (
            FiscalPeriod.objects.for_company(self.company)
            .filter(start_date__lte=entry_date, end_date__gte=entry_date)
            .order_by("start_date")
            .first()
        )
        self._periods[cache_key] = period
        return period

    def _write_group(self, key, legs):
        entry_date, doc_number, txn_type, _ = key

        # Closed-period guard first: resolving here (rather than letting the
        # model's save() raise) keeps one backdated group from aborting the
        # whole import — it is skipped and reported like unbalanced groups.
        period = self._fiscal_period(entry_date)
        if period is not None and not period.is_editable:
            self.report.skipped_closed_period.append(
                f"{entry_date} {txn_type or '?'} {doc_number or '(no ref)'}: "
                f"period {period.name} is {period.status}"
            )
            return None

        # One entity for the whole entry, when the legs agree on one.
        entities = {leg["row_entity"] for leg in legs if leg["row_entity"]}
        entity_name = entities.pop() if len(entities) == 1 else ""

        total_debit = sum((leg["debit"] for leg in legs), ZERO)
        total_credit = sum((leg["credit"] for leg in legs), ZERO)
        if _q2(total_debit) != _q2(total_credit):
            self.report.skipped_unbalanced.append(
                f"{entry_date} {txn_type or '?'} {doc_number or '(no ref)'}: "
                f"debits {_q2(total_debit)} != credits {_q2(total_credit)} "
                f"(source lines {', '.join(str(leg['line_number']) for leg in legs)})"
            )
            return None

        resolved = []
        for leg in legs:
            try:
                account = self.accounts.lookup(leg["account_ref"])
            except AmbiguousAccountError as exc:
                self.report.errors.append(f"line {leg['line_number']}: {exc}")
                return None
            if account is None:
                account = self.accounts.get_or_create_placeholder(leg["account_ref"])
            resolved.append((account, leg))

        fingerprint = entry_fingerprint(
            self.company.pk,
            entry_date,
            doc_number,
            txn_type,
            [
                (
                    account.full_name or account.name,
                    leg["debit"],
                    leg["credit"],
                    leg["entity"],
                )
                for account, leg in resolved
            ],
        )
        qbo_id = next((leg["qbo_id"] for leg in legs if leg["qbo_id"]), "")

        with transaction.atomic():
            # Match on the transaction's stable IDENTITY, never on its amounts.
            # `import_hash` covers the amounts, so keying on it means a
            # corrected export (same invoice, new total) fails to match and
            # inserts a SECOND entry beside the stale one — double-counting the
            # revenue. It stays on the model as a cheap change-detector and as
            # the fallback key for rows that carry no identity at all.
            if qbo_id:
                lookup = {"company": self.company, "qbo_id": qbo_id}
            elif doc_number:
                lookup = {
                    "company": self.company,
                    "source_reference": doc_number,
                    "entry_date": entry_date,
                    "qbo_txn_type": txn_type,
                    # Without the entity, two vendors sharing a check number on
                    # one day resolve to the same key and the second silently
                    # overwrites the first.
                    "entity_name": entity_name,
                    "entry_source": JournalEntry.EntrySource.QBO_IMPORT,
                }
            else:
                lookup = {"company": self.company, "import_hash": fingerprint}

            entry, created = JournalEntry.objects.update_or_create(
                **lookup,
                defaults={
                    k: v
                    for k, v in {
                        "entry_date": entry_date,
                        "fiscal_period": period,
                        "memo": next((leg["memo"] for leg in legs if leg["memo"]), ""),
                        "entry_source": JournalEntry.EntrySource.QBO_IMPORT,
                        "source_reference": doc_number,
                        "qbo_txn_type": txn_type,
                        "entity_name": entity_name,
                        "import_hash": fingerprint,
                        "status": JournalEntry.Status.POSTED,
                    }.items()
                    if k not in lookup
                },
            )

            # Rewrite the legs wholesale. An update path that tried to diff
            # individual lines would have to decide what "the same line" means
            # without a stable per-line id from QBO; replacing is simpler and
            # ends in the same state.
            entry.lines.all().delete()
            for sort_order, (account, leg) in enumerate(resolved):
                JournalLine.objects.create(
                    entry=entry,
                    account=account,
                    sort_order=sort_order,
                    description=leg["memo"][:500],
                    debit_amount=leg["debit"],
                    credit_amount=leg["credit"],
                    entity_name=leg["entity"][:255],
                )

            # Belt and braces: the group arithmetic above already proved this,
            # but re-checking from the DB catches a rounding or coercion bug
            # between here and the column scale.
            entry.validate_balanced()

        # Counted only now: incrementing inside the atomic block keeps the
        # phantom lines in the summary when validation rolls the savepoint back.
        self.report.lines_created += len(resolved)

        if created:
            self.report.entries_created += 1
        else:
            self.report.entries_updated += 1
        return entry


# --- top-level entry points ----------------------------------------------


def import_chart_of_accounts(company, path, report=None):
    report = report or ImportReport()
    importer = ChartOfAccountsImporter(company, report)
    importer.load_existing()
    importer.import_rows(list(read_table(path, context="coa")))
    return importer, report


def import_transactions(
    company, path, accounts=None, report=None, dayfirst=False, require_status=None
):
    report = report or ImportReport()
    if accounts is None:
        accounts = ChartOfAccountsImporter(company, report)
        accounts.load_existing()
    else:
        # One report for the whole run. Without this the accounts importer
        # keeps writing placeholder/error entries into the report it was
        # constructed with, and the caller's copy silently under-reports.
        accounts.report = report
    TransactionImporter(
        company,
        accounts,
        report,
        dayfirst=dayfirst,
        require_status=require_status,
    ).import_rows(list(read_table(path, context="transactions")))
    return report
