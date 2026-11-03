from django.urls import path

from . import erp_views

urlpatterns = [
    path(
        "",
        erp_views.AccountingDashboardView.as_view(),
        name="erp-accounting-dashboard",
    ),
    path(
        "trial-balance/",
        erp_views.TrialBalanceView.as_view(),
        name="erp-accounting-trial-balance",
    ),
    path(
        "accounts/<uuid:pk>/",
        erp_views.AccountRegisterView.as_view(),
        name="erp-accounting-account-register",
    ),
    path(
        "entries/",
        erp_views.JournalEntryListView.as_view(),
        name="erp-accounting-entries",
    ),
    path(
        "entries/<uuid:pk>/",
        erp_views.JournalEntryDetailView.as_view(),
        name="erp-accounting-entry-detail",
    ),
    path(
        "import/",
        erp_views.AccountingImportView.as_view(),
        name="erp-accounting-import",
    ),
    path(
        "periods/",
        erp_views.FiscalPeriodListView.as_view(),
        name="erp-accounting-periods",
    ),
    path(
        "reconciliation/",
        erp_views.ReconciliationView.as_view(),
        name="erp-accounting-reconciliation",
    ),
]
