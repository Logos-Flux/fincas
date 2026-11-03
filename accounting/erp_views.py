"""Accounting import — staff-only upload surface for QBO exports."""

import os
import tempfile

from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.db import transaction
from django.views.generic import FormView

from core.views.mixins import ERPViewMixin

from .forms import AccountingImportForm
from .qbo.importer import (
    ChartOfAccountsImporter,
    ImportReport,
    import_chart_of_accounts,
    import_transactions,
)
from .qbo.readers import QBOFormatError


class _Rollback(Exception):
    """Internal signal used to unwind a dry-run transaction."""


def _run_import(company, coa_file, txn_file, dayfirst, dry_run):
    """Run the import against temporary copies of the uploaded files.

    Returns ``(report, dry_run_text, error_text)`` where ``report`` is an
    ``ImportReport`` and the other two are optional strings for display.
    ``QBOFormatError`` is surfaced as ``error_text``; the caller's transaction
    is always left in a consistent state.

    Uploaded files are written to temp files whose suffix matches the upload
    name — the reader dispatches on extension, and giving it a bare
    ``/tmp/tmpXXXX`` with no suffix makes it refuse. Temp files are removed
    before returning.
    """
    report = ImportReport()
    temp_paths = []
    error_text = None

    try:
        # Materialise uploads into temp files with the right extension.
        for uploaded, label in ((coa_file, "coa"), (txn_file, "txn")):
            if not uploaded:
                continue
            _, ext = os.path.splitext(uploaded.name or "")
            ext = ext.lower()
            if ext not in AccountingImportForm.ALLOWED_EXTENSIONS:
                # Should be caught by form validation; keep as a guard.
                raise QBOFormatError(
                    f"Unsupported export format {ext!r} for {uploaded.name}; "
                    f"expected .csv or .xlsx."
                )
            tf = tempfile.NamedTemporaryFile(
                suffix=ext, delete=False, prefix=f"qbo_{label}_"
            )
            for chunk in uploaded.chunks():
                tf.write(chunk)
            tf.close()
            temp_paths.append((tf.name, label))

        # Map label -> path for the two import phases.
        coa_path = next((p for p, l in temp_paths if l == "coa"), None)
        txn_path = next((p for p, l in temp_paths if l == "txn"), None)

        dry_run_text = None

        # Mirror the management command's transaction shape: the whole import
        # runs inside one atomic block, and a dry run unwinds it with an
        # internal exception so nothing commits while still exercising every
        # write path and constraint check. A QBOFormatError also arrives
        # inside the block so it rolls back too.
        try:
            with transaction.atomic():
                accounts = None
                if coa_path:
                    accounts, report = import_chart_of_accounts(
                        company, coa_path, report
                    )
                if txn_path:
                    if accounts is None:
                        accounts = ChartOfAccountsImporter(company, report)
                        accounts.load_existing()
                    import_transactions(
                        company,
                        txn_path,
                        accounts=accounts,
                        report=report,
                        dayfirst=dayfirst,
                    )
                if dry_run:
                    raise _Rollback
        except _Rollback:
            dry_run_text = "DRY RUN — rolled back, nothing written."
        except QBOFormatError as exc:
            error_text = f"{exc} Nothing was written — the run was rolled back."
        # Any other exception escapes so the outer handler can surface it as
        # error_text — per-row problems are already in report.errors.

    except Exception as exc:  # noqa: BLE001
        if not error_text and not isinstance(exc, QBOFormatError):
            # Fatal / environment failure — the atomic block already rolled back.
            error_text = (
                f"Import aborted: {type(exc).__name__}: {exc}. "
                f"Nothing was written — the run was rolled back and re-running is safe."
            )
        if not isinstance(exc, (QBOFormatError, _Rollback)):
            # Also collect into the report so the template shows something.
            if "report" in locals() and error_text:
                report.errors.append(error_text)

    finally:
        for p, _ in temp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    return report, dry_run_text if "dry_run_text" in locals() else None, error_text


class StaffRequiredMixin(UserPassesTestMixin):
    login_url = "/erp/login/"

    def test_func(self):
        return self.request.user.is_staff


class AccountingImportView(ERPViewMixin, StaffRequiredMixin, FormView):
    """Staff-only upload for chart-of-accounts and transaction exports.

    Defaults to dry-run so the first real import can be previewed. The
    importer's report is shown rather than a success flash: created/updated
    counts, placeholders, skipped-unbalanced groups, errors and unknown account
    types (refused, not guessed).
    """

    template_name = "erp/accounting_import.html"
    form_class = AccountingImportForm
    current_section = "accounting_import"
    page_title = "Accounting — Import"

    def get_initial(self):
        initial = super().get_initial()
        # Dry-run default lives here so an unbound GET shows it checked and a
        # POST that omits the checkbox correctly yields False.
        initial.setdefault("dry_run", True)
        return initial

    def form_valid(self, form):
        company = form.cleaned_data["company"]
        coa_file = form.cleaned_data.get("chart_of_accounts")
        txn_file = form.cleaned_data.get("transactions")
        dayfirst = form.cleaned_data.get("dayfirst", False)
        dry_run = form.cleaned_data.get("dry_run", False)

        report, dry_run_text, error_text = _run_import(
            company, coa_file, txn_file, dayfirst, dry_run
        )

        # Always render the same template with the result inline — no redirect,
        # no flash. The report is the deliverable; a redirect would lose it or
        # force it through messages (which truncation-slices at 20 lines by design).
        return self.render_to_response(
            self.get_context_data(
                form=form,
                report=report,
                report_lines=report.as_lines(),
                dry_run=dry_run,
                dry_run_text=dry_run_text,
                error_text=error_text,
                import_company=company,
            )
        )

    def form_invalid(self, form):
        # Re-render with errors; no report to show.
        return self.render_to_response(self.get_context_data(form=form))


# ---------------------------------------------------------------------------
# Read-only reporting surfaces
#
# The import command and the upload page were, until now, the only things that
# touched this app — so a finished import had nowhere to be looked at except
# the Django admin changelist. These are the minimum needed to answer "did the
# books land, and do they tie?": a summary, a trial balance, an account
# register, and the journal itself.
#
# Every total here is computed by the database. A Python-side ``sum()`` over a
# ledger this size (8,847 lines and growing) pulls every row into memory to
# produce one number, which is what commit 8b425f6 removed from the reporting
# app; don't reintroduce it here.
# ---------------------------------------------------------------------------

import csv
import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import (
    Case,
    Count,
    DecimalField,
    F,
    Max,
    Min,
    Q,
    Sum,
    Value,
    When,
    Window,
)
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import DetailView, ListView, TemplateView

from core.csv_safety import csv_safe, csv_safe_row
from core.models import Company

from .models import ZERO, Account, FiscalPeriod, JournalEntry, JournalLine

#: Entries in these states are excluded from every balance on these pages.
#: Draft entries are not yet part of the ledger and void ones have been undone;
#: including either makes a trial balance that cannot be tied to anything.
#: The pages report how many they excluded rather than dropping them silently.
EXCLUDED_STATUSES = (JournalEntry.Status.DRAFT, JournalEntry.Status.VOID)

_MONEY = DecimalField(max_digits=18, decimal_places=2)

#: Half a cent. An entry is "unbalanced" only if its debits and credits differ
#: by at least one cent — every amount here is 2-decimal money, so nothing
#: smaller than that is a real difference.
#:
#: This is not slack in the accounting; it is protection from the backend.
#: Comparing two summed DecimalFields with ``=`` in SQL flags entries that
#: balance exactly: SQLite aggregates decimals through float, so 84 of these
#: 3,370 entries compared unequal while Python's own Decimal arithmetic on the
#: identical rows found every one of them balanced. Postgres gets it right, but
#: a correctness check that silently depends on which database is underneath is
#: not a check. Comparing the summed *difference* to a tolerance is exact on
#: both.
BALANCE_TOLERANCE = Decimal("0.005")


def _money_sum(field, filter=None):
    """``Sum`` that yields 0.00 instead of NULL for an empty set.

    An un-coalesced Sum returns None for an account with no lines, and None
    then propagates through every subtotal and comparison downstream — an
    account with no activity would break the balance check rather than
    contributing nothing to it.
    """
    return Coalesce(
        Sum(field, filter=filter, output_field=_MONEY),
        Value(ZERO, output_field=_MONEY),
        output_field=_MONEY,
    )


def resolve_company(request):
    """Pick the company whose books these pages show.

    Single Company row -> that one. Otherwise only a superuser may name one
    with ``?company=<uuid>``; anyone else gets ``None`` and the page asks
    rather than guessing, because showing one company's ledger under another's
    name is worse than showing nothing.

    ``?company=`` is deliberately NOT a general selector. There is no
    user-to-company binding to check it against: ``UserProfile`` keyed off a
    legacy Supabase uid and had no FK to ``auth.User`` (``core/models.py``), so
    ``request.user.userprofile`` never resolves for a session-authenticated ERP
    user — a fact this repo already records at ``orders/erp_views.py``. An
    honoured parameter would therefore be an unscoped tenant selector: every
    staff user could read every company's complete general ledger by pasting a
    UUID. Superusers are exempt because they are cross-company by definition,
    which is the same line ``core/managers.scope_to_company`` draws.

    When a real user-to-company link exists, this is the one function to
    change.
    """
    companies = list(Company.objects.all()[:2])
    if len(companies) == 1:
        return companies[0]

    # Step 0 (AD-12): session users now link to a UserProfile with a company.
    # Their own company wins over any hand-pasted ``?company=`` — honouring
    # the parameter here would re-open the unscoped-selector hole this
    # function was written to prevent.
    if getattr(request.user, "is_authenticated", False):
        profile_company = getattr(
            getattr(request.user, "userprofile", None), "company", None
        )
        if profile_company is not None:
            return profile_company

    requested = request.GET.get("company")
    if requested and request.user.is_superuser:
        try:
            return Company.objects.get(pk=requested)
        except Company.DoesNotExist:
            raise Http404("No such company.")
        except (ValidationError, ValueError, TypeError):
            # A malformed UUID is a hand-edited URL, not a server fault.
            # UUIDField.to_python raises ValidationError, which is neither
            # ValueError nor TypeError — catching only those turns a typo into
            # a 500, the exact outcome _parse_date below was written to avoid.
            raise Http404("Malformed company id.")
    return None


def _parse_date(raw):
    """Read a ``YYYY-MM-DD`` query parameter, or None if absent/malformed.

    A bad date falls back to "no filter" rather than raising: these are
    read-only pages reached by hand-edited URLs, and a 500 on a typo is worse
    than a wider date range.
    """
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


class AccountingBaseView(ERPViewMixin, StaffRequiredMixin):
    """Shared company resolution + date-range handling for the GL pages."""

    current_section = "accounting"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company = resolve_company(self.request)
        context["company"] = company
        context["companies"] = Company.objects.order_by("name")
        # Every link this page renders has to carry the company forward, or it
        # lands on a page that cannot resolve one and 404s. Empty on the normal
        # single-tenant install so URLs stay clean.
        context["q_company"] = (
            f"company={company.pk}"
            if company is not None and Company.objects.count() > 1
            else ""
        )
        context["date_from"] = self.request.GET.get("date_from", "")
        context["date_to"] = self.request.GET.get("date_to", "")
        return context

    def date_filter(self, prefix="entry__"):
        """Build the posted-only, date-bounded Q for line-level aggregates."""
        q = ~Q(**{f"{prefix}status__in": EXCLUDED_STATUSES})
        date_from = _parse_date(self.request.GET.get("date_from"))
        date_to = _parse_date(self.request.GET.get("date_to"))
        if date_from:
            q &= Q(**{f"{prefix}entry_date__gte": date_from})
        if date_to:
            q &= Q(**{f"{prefix}entry_date__lte": date_to})
        return q


class AccountingDashboardView(AccountingBaseView, TemplateView):
    """Landing page: does the ledger exist, and does it tie?

    Deliberately answers the import-verification question first — row counts,
    the debit/credit identity, and anything that would make the books
    untrustworthy (unbalanced entries, placeholder accounts the importer had to
    invent) — before any of the accounting itself.
    """

    template_name = "erp/accounting_dashboard.html"
    page_title = "Accounting"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company = context["company"]
        if company is None:
            return context

        entries = JournalEntry.objects.filter(company=company)
        lines = JournalLine.objects.filter(entry__company=company)
        accounts = Account.objects.filter(company=company)

        totals = lines.filter(self.date_filter()).aggregate(
            debit=_money_sum("debit_amount"), credit=_money_sum("credit_amount")
        )
        context["total_debit"] = totals["debit"]
        context["total_credit"] = totals["credit"]
        context["out_of_balance"] = totals["debit"] - totals["credit"]

        context["account_count"] = accounts.count()
        context["entry_count"] = entries.count()
        context["line_count"] = lines.count()

        # Health signals. Each of these is silent by nature — an unbalanced
        # entry still renders fine on its own page, and a placeholder account
        # looks like any other account — so they get counted up front.
        # Posted entries only, exactly like every figure above. A draft is
        # allowed to be half-written; counting one as "unbalanced" raises the
        # books-are-broken alarm for a document that is not in the books yet,
        # and the same entry is simultaneously reported as excluded two lines
        # down — the page contradicting itself.
        posted = entries.exclude(status__in=EXCLUDED_STATUSES)
        context["unbalanced_count"] = (
            posted.annotate(
                diff=_money_sum(
                    F("lines__debit_amount") - F("lines__credit_amount")
                )
            )
            .exclude(
                diff__gt=-BALANCE_TOLERANCE, diff__lt=BALANCE_TOLERANCE
            )
            .count()
        )
        context["placeholder_count"] = accounts.filter(is_active=False).count()
        context["excluded_count"] = entries.filter(
            status__in=EXCLUDED_STATUSES
        ).count()

        span = entries.aggregate(
            first=Min("entry_date"), last=Max("entry_date")
        )
        context["first_entry_date"] = span["first"]
        context["last_entry_date"] = span["last"]

        context["by_source"] = list(
            entries.values("entry_source")
            .annotate(n=Count("id"))
            .order_by("-n")
        )
        source_labels = dict(JournalEntry.EntrySource.choices)
        for row in context["by_source"]:
            row["label"] = source_labels.get(
                row["entry_source"], row["entry_source"]
            )

        # Balance-sheet-shaped rollup: one line per classification, in the
        # order a human reads a set of books.
        type_rows = (
            accounts.values("account_type")
            .annotate(
                debit=_money_sum("journal_lines__debit_amount",
                                 filter=self.date_filter("journal_lines__entry__")),
                credit=_money_sum("journal_lines__credit_amount",
                                  filter=self.date_filter("journal_lines__entry__")),
                n=Count("id", distinct=True),
            )
        )
        by_type = {r["account_type"]: r for r in type_rows}
        ordered = []
        for value, label in Account.AccountType.choices:
            row = by_type.get(value, {"debit": ZERO, "credit": ZERO, "n": 0})
            debit, credit = row["debit"], row["credit"]
            natural = (
                debit - credit
                if value in Account.DEBIT_NORMAL_TYPES
                else credit - debit
            )
            ordered.append({
                "type": value,
                "label": label,
                "debit": debit,
                "credit": credit,
                "balance": natural,
                "account_count": row["n"],
                "normal_side": (
                    "DR" if value in Account.DEBIT_NORMAL_TYPES else "CR"
                ),
            })
        context["by_type"] = ordered

        context["recent_entries"] = (
            posted.select_related("company")
            .annotate(
                d=_money_sum("lines__debit_amount"),
            )
            .order_by("-entry_date", "-created_at")[:10]
        )
        return context


class TrialBalanceView(AccountingBaseView, TemplateView):
    """Every account's debit and credit totals, with the balance identity.

    This is the report that proves an import: if total debits equal total
    credits and each account's balance matches the source, the ledger arrived
    intact. Accounts with no activity in the period are hidden by default
    (``?show_empty=1`` brings them back) — 187 accounts of which ~90 are
    dormant makes the ones that moved hard to find.
    """

    template_name = "erp/accounting_trial_balance.html"
    current_section = "accounting_trial_balance"
    page_title = "Trial Balance"

    def get_rows(self, company):
        line_filter = self.date_filter("journal_lines__entry__")
        qs = (
            Account.objects.filter(company=company)
            .annotate(
                debit=_money_sum("journal_lines__debit_amount", filter=line_filter),
                credit=_money_sum("journal_lines__credit_amount", filter=line_filter),
            )
            .annotate(
                # Natural balance: debit-positive for asset/expense accounts,
                # credit-positive for the rest. Computed in SQL so it can be
                # ordered and filtered on without pulling rows into Python.
                balance=Case(
                    When(
                        account_type__in=list(Account.DEBIT_NORMAL_TYPES),
                        then=F("debit") - F("credit"),
                    ),
                    default=F("credit") - F("debit"),
                    output_field=_MONEY,
                )
            )
            .order_by("account_number", "full_name")
        )
        if not self.request.GET.get("show_empty"):
            qs = qs.exclude(debit=ZERO, credit=ZERO)
        account_type = self.request.GET.get("account_type")
        if account_type in Account.AccountType.values:
            qs = qs.filter(account_type=account_type)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company = context["company"]
        if company is None:
            return context

        rows = list(self.get_rows(company))
        context["rows"] = rows

        # Totals come from the database over the *unpaginated, unfiltered-by-
        # type* set, not from adding up `rows` — a per-page or per-type sum
        # that calls itself the trial balance total is how a report quietly
        # stops tying out.
        totals = JournalLine.objects.filter(
            entry__company=company
        ).filter(self.date_filter()).aggregate(
            debit=_money_sum("debit_amount"), credit=_money_sum("credit_amount")
        )
        context["total_debit"] = totals["debit"]
        context["total_credit"] = totals["credit"]
        context["out_of_balance"] = totals["debit"] - totals["credit"]
        context["show_empty"] = bool(self.request.GET.get("show_empty"))
        context["account_type"] = self.request.GET.get("account_type", "")
        context["account_types"] = Account.AccountType.choices
        return context

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") == "csv":
            return self.export_csv()
        return super().get(request, *args, **kwargs)

    def export_csv(self):
        company = resolve_company(self.request)
        if company is None:
            raise Http404("No company selected.")
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            'attachment; filename="trial-balance.csv"'
        )
        writer = csv.writer(response)

        writer.writerow([
            "Account Number", "Full Name", "Type", "Subtype",
            "Debit", "Credit", "Balance",
        ])
        for row in self.get_rows(company).iterator():
            writer.writerow([
                csv_safe(row.account_number), csv_safe(row.full_name),
                row.get_account_type_display(),
                row.get_account_subtype_display() if row.account_subtype else "",
                row.debit, row.credit, row.balance,
            ])
        return response


class AccountRegisterView(AccountingBaseView, DetailView):
    """One account's lines in date order, with a running balance.

    The running total is a SQL window function rather than a Python loop, so it
    stays correct under the paginator: page 2 continues from page 1's closing
    figure instead of restarting at zero, which is the classic way a register
    lies.
    """

    template_name = "erp/accounting_account_register.html"
    context_object_name = "account"
    current_section = "accounting_trial_balance"
    page_title = "Account Register"
    paginate_by = 100

    def get_object(self, queryset=None):
        company = resolve_company(self.request)
        if company is None:
            raise Http404("No company selected.")
        return get_object_or_404(
            Account, pk=self.kwargs["pk"], company=company
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        account = self.object
        context["page_title"] = f"Register — {account.full_name or account.name}"

        sign = 1 if account.account_type in Account.DEBIT_NORMAL_TYPES else -1
        signed = (F("debit_amount") - F("credit_amount")) * sign

        lines = (
            JournalLine.objects.filter(account=account)
            .filter(self.date_filter())
            .select_related("entry")
            .annotate(
                running_balance=Window(
                    expression=Sum(signed, output_field=_MONEY),
                    order_by=[
                        F("entry__entry_date").asc(),
                        F("entry__created_at").asc(),
                        F("sort_order").asc(),
                        F("id").asc(),
                    ],
                )
            )
            .order_by("entry__entry_date", "entry__created_at", "sort_order", "id")
        )

        paginator = Paginator(lines, self.paginate_by)
        page = paginator.get_page(self.request.GET.get("page"))
        context["paginator"] = paginator
        context["page_obj"] = page
        context["is_paginated"] = page.has_other_pages()
        context["lines"] = page.object_list

        totals = (
            JournalLine.objects.filter(account=account)
            .filter(self.date_filter())
            .aggregate(
                debit=_money_sum("debit_amount"), credit=_money_sum("credit_amount")
            )
        )

        # Everything above is *period* activity. Without the balance brought
        # forward from before `date_from`, a date-filtered register labels the
        # period's net movement "closing balance" — an account holding 150.00
        # reads as 50.00 because 100.00 was posted before the window, and
        # nothing on the page says otherwise.
        opening = ZERO
        date_from = _parse_date(self.request.GET.get("date_from"))
        if date_from:
            prior = (
                JournalLine.objects.filter(account=account)
                .exclude(entry__status__in=EXCLUDED_STATUSES)
                .filter(entry__entry_date__lt=date_from)
                .aggregate(
                    debit=_money_sum("debit_amount"),
                    credit=_money_sum("credit_amount"),
                )
            )
            opening = sign * (prior["debit"] - prior["credit"])

        # Offset the page's own rows by the brought-forward figure. The
        # aggregate itself stays in SQL (the window above); this only shifts
        # the <=100 rows being displayed by a constant.
        if opening:
            for line in context["lines"]:
                line.running_balance = (line.running_balance or ZERO) + opening

        context["opening_balance"] = opening
        context["total_debit"] = totals["debit"]
        context["total_credit"] = totals["credit"]
        context["period_movement"] = sign * (totals["debit"] - totals["credit"])
        context["closing_balance"] = opening + context["period_movement"]
        context["is_date_filtered"] = bool(date_from)
        context["normal_side"] = "DR" if sign == 1 else "CR"
        return context


class JournalEntryListView(AccountingBaseView, ListView):
    """The journal itself — every entry, newest first, searchable."""

    template_name = "erp/accounting_entries.html"
    context_object_name = "entries"
    current_section = "accounting_entries"
    page_title = "Journal Entries"
    paginate_by = 50

    def get_queryset(self):
        company = resolve_company(self.request)
        if company is None:
            return JournalEntry.objects.none()
        qs = (
            JournalEntry.objects.filter(company=company)
            .annotate(
                total_debit=_money_sum("lines__debit_amount"),
                total_credit=_money_sum("lines__credit_amount"),
                line_count=Count("lines", distinct=True),
            )
            .order_by("-entry_date", "-created_at")
        )
        date_from = _parse_date(self.request.GET.get("date_from"))
        date_to = _parse_date(self.request.GET.get("date_to"))
        if date_from:
            qs = qs.filter(entry_date__gte=date_from)
        if date_to:
            qs = qs.filter(entry_date__lte=date_to)

        source = self.request.GET.get("entry_source")
        if source in JournalEntry.EntrySource.values:
            qs = qs.filter(entry_source=source)

        query = (self.request.GET.get("q") or "").strip()
        if query:
            qs = qs.filter(
                Q(entry_number__icontains=query)
                | Q(source_reference__icontains=query)
                | Q(memo__icontains=query)
                | Q(entity_name__icontains=query)
            )
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search_query"] = self.request.GET.get("q", "")
        context["entry_source"] = self.request.GET.get("entry_source", "")
        context["entry_sources"] = JournalEntry.EntrySource.choices
        return context


class FiscalPeriodListView(AccountingBaseView, ListView):
    """Fiscal periods with their close face (Step 1b, Design §6).

    Open periods show a Close button; a refused close re-renders with the
    named reason (e.g. draft blockers) instead of a bare error page.
    """

    template_name = "erp/accounting_periods.html"
    context_object_name = "periods"
    current_section = "accounting"
    page_title = "Fiscal Periods"
    paginate_by = 50
    status_colors = {"open": "success", "closed": "dark", "locked": "secondary"}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["status_colors"] = self.status_colors
        return context

    def get_queryset(self):
        from django.db.models import Count, F
        from django.db.models import Q as DJQ

        company = resolve_company(self.request)
        if company is None:
            return FiscalPeriod.objects.none()
        return (
            FiscalPeriod.objects.filter(company=company)
            .annotate(
                draft_entry_count=Count(
                    "company__journal_entries",
                    filter=DJQ(
                        company__journal_entries__status=(
                            JournalEntry.Status.DRAFT
                        ),
                        company__journal_entries__entry_date__gte=F(
                            "start_date"
                        ),
                        company__journal_entries__entry_date__lte=F("end_date"),
                    ),
                ),
            )
            .order_by("-start_date")
        )

    def post(self, request, *args, **kwargs):
        from .periods import PeriodCloseError, close_period, reopen_period

        company = resolve_company(request)
        if company is None:
            raise Http404("No company selected.")
        period = get_object_or_404(
            FiscalPeriod, pk=request.POST.get("period"), company=company
        )
        action = request.POST.get("action")
        try:
            if action == "close":
                close_period(period)
                messages.success(
                    request,
                    f"{period.name} is closed. Postings dated inside it are "
                    "refused until it is reopened.",
                )
            elif action == "reopen":
                reopen_period(period)
                messages.success(request, f"{period.name} is open again.")
            else:
                raise Http404("Unknown action.")
        except PeriodCloseError as exc:
            messages.error(request, str(exc))
        return redirect(request.path)


class ReconciliationView(AccountingBaseView, TemplateView):
    """GL-to-subledger reconciliation (Step 1c, AD-7).

    Control balances, all-time per company: the GL side sums posted,
    non-void entry lines per control account; the subledger side sums the
    source documents. Any difference is a real defect (or an understood
    gap, labelled). The headline tile counts entries that fail the balance
    check with a link to the filtered journal.
    """

    template_name = "erp/accounting_recon.html"
    current_section = "accounting"
    page_title = "Reconciliation"

    def _gl_balance(self, account):
        from django.db.models import Sum

        totals = (
            JournalLine.objects.filter(
                account=account,
                entry__company=account.company,
                entry__status=JournalEntry.Status.POSTED,
            ).aggregate(
                debit=Sum("debit_amount"), credit=Sum("credit_amount")
            )
        )
        return (totals["debit"] or ZERO) - (totals["credit"] or ZERO)

    def _rows(self, company):
        from django.db.models import Sum

        from orders.models import SalesInvoice
        from payments.models import Payment
        from purchasing.models import POReceipt

        from .models import DocumentPostingRule

        rows = []
        try:
            invoice_rule = DocumentPostingRule.objects.get(
                company=company,
                doc_type=DocumentPostingRule.DocType.SALES_INVOICE,
            )
            pay_rule = DocumentPostingRule.objects.get(
                company=company, doc_type=DocumentPostingRule.DocType.PAYMENT
            )
            receipt_rule = DocumentPostingRule.objects.get(
                company=company,
                doc_type=DocumentPostingRule.DocType.PURCHASE_RECEIPT,
            )
        except DocumentPostingRule.DoesNotExist as exc:
            return None, str(exc)

        voided = (SalesInvoice.Status.VOID, SalesInvoice.Status.CANCELLED)
        ar_docs = (
            SalesInvoice.objects.filter(company=company)
            .exclude(status__in=voided)
            .aggregate(total=Sum("balance_due"))["total"]
            or ZERO
        )
        ar_gl = self._gl_balance(invoice_rule.receivable_account)
        rows.append(
            {
                "label": "Accounts Receivable",
                "gl": ar_gl,
                "subledger": ar_docs,
                "diff": ar_gl - ar_docs,
                "note": "Open invoice balances",
            }
        )

        settled = Payment.objects.filter(
            company=company,
            status__in=(Payment.Status.CAPTURED, Payment.Status.SETTLED),
        ).exclude(payment_type=Payment.PaymentType.REFUND)
        cash_in = sum(
            (p.amount - p.convenience_fee for p in settled), ZERO
        )
        refunded = Payment.objects.filter(
            company=company,
            payment_type=Payment.PaymentType.REFUND,
            status=Payment.Status.REFUNDED,
        )
        cash_out = sum((p.amount for p in refunded), ZERO)
        # Payments post to the rule's cash account or to a provider override's
        # (PayPal balance, bank transfers, …), so the GL side is the sum over
        # every account that can receive one. A refund rule pointing elsewhere
        # is included for the same reason.
        cash_accounts = {pay_rule.cash_account}
        refund_rule = DocumentPostingRule.objects.filter(
            company=company, doc_type=DocumentPostingRule.DocType.REFUND
        ).select_related("cash_account").first()
        if refund_rule is not None and refund_rule.cash_account is not None:
            cash_accounts.add(refund_rule.cash_account)
        cash_accounts.update(
            Account.objects.filter(provider_overrides_cash__company=company).distinct()
        )
        cash_gl = sum((self._gl_balance(a) for a in cash_accounts), ZERO)
        rows.append(
            {
                "label": "Cash",
                "gl": cash_gl,
                "subledger": cash_in - cash_out,
                "diff": cash_gl - (cash_in - cash_out),
                "note": "Settled payments net of refunds, across every cash account "
                        "the payment rule and provider overrides name",
            }
        )

        receipt_total = ZERO
        receipts = POReceipt.objects.filter(
            purchase_order__company=company,
            status__in=(
                POReceipt.Status.VERIFIED,
                POReceipt.Status.POSTED,
            ),
        ).select_related("purchase_order")
        for receipt in receipts:
            for line in receipt.lines.select_related("po_line_item").all():
                qty = line.quantity_received or ZERO
                ordered = line.po_line_item.quantity_ordered or ZERO
                if ordered > 0:
                    receipt_total += (
                        line.po_line_item.line_total * qty / ordered
                    )
                receipt_total += (
                    (
                        line.po_line_item.landed_cost_per_unit
                        - line.po_line_item.unit_cost
                    )
                    * qty
                )
        ap_gl = self._gl_balance(receipt_rule.payable_account)
        rows.append(
            {
                "label": "Accounts Payable",
                "gl": -ap_gl,  # credit-positive for display
                "subledger": receipt_total,
                "diff": -ap_gl - receipt_total,
                "note": "Received goods + landed, before supplier payments",
            }
        )
        return rows, None

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") == "csv":
            company = resolve_company(request)
            rows, _ = self._rows(company) if company else ([], None)
            return self._csv(rows or [])
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company = resolve_company(self.request)
        context["company"] = company
        if company is None:
            context["rows"] = []
            context["setup_error"] = "No company selected."
            return context
        rows, setup_error = self._rows(company)
        context["rows"] = rows or []
        context["setup_error"] = setup_error
        context["out_of_balance"] = self._out_of_balance(company)
        period_id = self.request.GET.get("fiscal_period")
        context["periods"] = FiscalPeriod.objects.filter(
            company=company
        ).order_by("-start_date")
        context["selected_period"] = None
        if period_id:
            try:
                context["selected_period"] = FiscalPeriod.objects.get(
                    pk=period_id, company=company
                )
            except (FiscalPeriod.DoesNotExist, ValueError, ValidationError):
                pass
        return context

    def _out_of_balance(self, company):
        from django.db.models import F, Q, Sum

        # One query, no row cap: lineless entries match the first branch,
        # mismatched sums the second. Mirrors validate_balanced exactly.
        return list(
            JournalEntry.objects.filter(company=company)
            .exclude(status=JournalEntry.Status.VOID)
            .annotate(
                _debit=Sum("lines__debit_amount"),
                _credit=Sum("lines__credit_amount"),
            )
            .filter(Q(lines__isnull=True) | ~Q(_debit=F("_credit")))
            .distinct()
            .order_by("-entry_date")[:100]
        )

    def _csv(self, rows):
        import csv

        from django.http import HttpResponse

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            "attachment; filename=reconciliation.csv"
        )
        writer = csv.writer(response)
        writer.writerow(["account", "gl", "subledger", "difference", "note"])
        for row in rows:
            writer.writerow(csv_safe_row(
                [row["label"], row["gl"], row["subledger"], row["diff"], row["note"]]
            ))
        return response


class JournalEntryDetailView(AccountingBaseView, DetailView):
    """One entry and its legs, with the balance check shown explicitly."""

    template_name = "erp/accounting_entry_detail.html"
    context_object_name = "entry"
    current_section = "accounting_entries"
    page_title = "Journal Entry"

    def get_object(self, queryset=None):
        company = resolve_company(self.request)
        if company is None:
            raise Http404("No company selected.")
        return get_object_or_404(
            JournalEntry.objects.select_related("company", "fiscal_period"),
            pk=self.kwargs["pk"],
            company=company,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        entry = self.object
        context["lines"] = (
            entry.lines.select_related("account").order_by("sort_order", "id")
        )
        totals = entry.lines.aggregate(
            debit=_money_sum("debit_amount"), credit=_money_sum("credit_amount")
        )
        context["total_debit"] = totals["debit"]
        context["total_credit"] = totals["credit"]
        context["is_balanced"] = totals["debit"] == totals["credit"]
        context["page_title"] = (
            f"Entry {entry.entry_number or entry.source_reference or entry.pk}"
        )
        return context
