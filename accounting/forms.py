"""Upload form for the accounting QBO import."""

from django import forms

from core.models import Company


class AccountingImportForm(forms.Form):
    """Two uploads, one required company, dry-run defaulting to checked."""

    ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xlsm"}

    company = forms.ModelChoiceField(
        queryset=Company.objects.order_by("name"),
        empty_label="— Select a company —",
        required=True,
        help_text="Accounts and entries are company-scoped; the wrong one corrupts the books.",
    )
    chart_of_accounts = forms.FileField(
        required=False,
        label="Chart of accounts",
        help_text=".csv, .xlsx, or .xlsm — the account list export.",
    )
    transactions = forms.FileField(
        required=False,
        label="Transactions",
        help_text=".csv, .xlsx, or .xlsm — Journal or Transaction Detail export.",
    )
    dayfirst = forms.BooleanField(
        required=False,
        initial=False,
        label="Read slash dates as DD/MM/YYYY",
        help_text="Leave off for US-format (MM/DD/YYYY) exports.",
    )
    dry_run = forms.BooleanField(
        required=False,
        initial=True,
        label="Dry run (preview only — nothing will be written)",
    )

    def clean(self):
        cleaned = super().clean()
        coa = cleaned.get("chart_of_accounts")
        txn = cleaned.get("transactions")
        if not coa and not txn:
            raise forms.ValidationError(
                "Upload at least one file — a chart of accounts and/or transactions."
            )
        for field_name in ("chart_of_accounts", "transactions"):
            uploaded = cleaned.get(field_name)
            if uploaded and not self._allowed(uploaded.name):
                self.add_error(
                    field_name,
                    "Unsupported file type — use .csv, .xlsx, or .xlsm (not legacy .xls).",
                )
        return cleaned

    @classmethod
    def _allowed(cls, filename):
        import os

        _, ext = os.path.splitext(filename or "")
        return ext.lower() in cls.ALLOWED_EXTENSIONS
