"""Django admin for the general ledger.

The inline formset carries real validation rather than the usual field-level
defaults, because the admin is a *writer* of journal entries and the two
invariants that matter here span rows:

  * an entry's debits must equal its credits
  * every line's account must belong to the entry's company

Neither can be a database constraint (both need sibling or two-hop context), so
without this the admin is a hole straight through the guarantees the importer
maintains — you can save a ledger the importer would have refused to write.
"""

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db.models import Sum

from .models import ZERO, Account, FiscalPeriod, JournalEntry, JournalLine


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = [
        "account_number", "full_name", "account_type",
        "account_subtype", "is_active", "company",
    ]
    list_filter = ["account_type", "account_subtype", "is_active", "company"]
    search_fields = ["account_number", "name", "full_name", "qbo_id"]
    readonly_fields = ["id", "created_at", "updated_at"]
    autocomplete_fields = ["parent"]
    ordering = ["account_number", "full_name"]


@admin.register(FiscalPeriod)
class FiscalPeriodAdmin(admin.ModelAdmin):
    list_display = ["name", "start_date", "end_date", "status", "company"]
    list_filter = ["status", "company"]
    search_fields = ["name"]
    readonly_fields = ["id", "created_at", "updated_at"]


class JournalLineInlineFormSet(forms.BaseInlineFormSet):
    """Enforces the two cross-row invariants the database cannot."""

    def clean(self):
        super().clean()
        if any(self.errors):
            # Field-level errors already failed the save; a totals check on
            # half-parsed rows would only add noise.
            return

        company_id = self.instance.company_id if self.instance else None
        total_debit = ZERO
        total_credit = ZERO
        live_lines = 0

        for form in self.forms:
            # `cleaned_data` is absent on a form that never got validated, so
            # read it defensively — an AttributeError here would surface as a
            # 500 on save instead of a validation message.
            cleaned = getattr(form, "cleaned_data", None)
            if not isinstance(cleaned, dict) or cleaned.get("DELETE"):
                continue
            account = cleaned.get("account")
            if account is None:
                continue
            live_lines += 1
            total_debit += cleaned.get("debit_amount") or ZERO
            total_credit += cleaned.get("credit_amount") or ZERO

            if company_id and account.company_id != company_id:
                raise ValidationError(
                    f"Line account {account} belongs to a different company "
                    f"than this journal entry."
                )

        if live_lines == 0:
            raise ValidationError(
                "A journal entry needs at least one line."
            )
        if total_debit != total_credit:
            raise ValidationError(
                f"Journal entry does not balance: debits {total_debit} != "
                f"credits {total_credit}."
            )


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    formset = JournalLineInlineFormSet
    extra = 0
    fields = [
        "sort_order", "account", "description",
        "debit_amount", "credit_amount", "entity_name",
    ]
    ordering = ["sort_order"]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """Offer only the relevant company's accounts.

        An unscoped dropdown lists every tenant's chart, which makes a
        cross-company line a two-click mistake rather than something you have
        to work at. On the change view the company comes from the entry; on the
        add view there is no entry yet, so it comes from whatever company the
        form is being submitted with.

        This is a usability guard, not the boundary — a hand-crafted POST can
        still name any account id. ``JournalLineInlineFormSet.clean()`` is what
        actually rejects it, on both flows.
        """
        if db_field.name == "account":
            company_id = None
            entry = getattr(request, "_journalentry_being_edited", None)
            if entry is not None and entry.company_id:
                company_id = entry.company_id
            else:
                # Add flow: the chosen company is in the submitted data.
                company_id = (
                    request.POST.get("company") or request.GET.get("company") or None
                )
            if company_id:
                kwargs["queryset"] = Account.objects.filter(company_id=company_id)
            else:
                kwargs["queryset"] = Account.objects.all()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = [
        "entry_number", "entry_date", "entry_source",
        "source_reference", "status", "balanced", "company",
    ]
    list_filter = ["entry_source", "status", "company", "entry_date"]
    search_fields = ["entry_number", "source_reference", "memo", "qbo_id"]
    readonly_fields = ["id", "created_at", "updated_at", "import_hash"]
    date_hierarchy = "entry_date"
    inlines = [JournalLineInline]

    def get_queryset(self, request):
        # Annotate the totals once for the whole page. Aggregating per row in
        # `balanced()` is an extra query per entry on every changelist load.
        return (
            super()
            .get_queryset(request)
            .annotate(
                _total_debit=Sum("lines__debit_amount"),
                _total_credit=Sum("lines__credit_amount"),
            )
        )

    @admin.display(boolean=True, description="Balanced")
    def balanced(self, obj):
        debit = getattr(obj, "_total_debit", None)
        credit = getattr(obj, "_total_credit", None)
        if debit is None and credit is None:
            # Not annotated (e.g. the change form) — fall back to a query.
            debit, credit = obj.totals()
        return (debit or ZERO) == (credit or ZERO)

    def get_form(self, request, obj=None, **kwargs):
        # Stashed so the inline can scope its account dropdown to this entry's
        # company; the inline has no other route to the parent object.
        request._journalentry_being_edited = obj
        return super().get_form(request, obj, **kwargs)
