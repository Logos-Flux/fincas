"""``/erp/imports/`` routes of the CRM importer; mounted ahead of ``unify.erp_urls``.

Every job page lives under ``crm/<job>/`` (spec 4.6): the list, the overview,
the queues and their POSTs (step 9, ``crm_import.views.review``) and the
report (step 8, ``erp_views.ImportReportView``). Step 8 first served the
report at ``<job>/report/``; that path redirects, so links printed by an
earlier CLI run keep working.
"""

from django.urls import path
from django.views.generic import RedirectView

from . import erp_views
from .views import review as review_views

urlpatterns = [
    path("<uuid:pk>/report/", RedirectView.as_view(pattern_name="erp-crm-import-report", query_string=True),
         name="erp-crm-import-report-old"),
    path("crm/", review_views.JobListView.as_view(), name="erp-crm-import-job-list"),
    path("crm/<uuid:pk>/", review_views.JobOverviewView.as_view(), name="erp-crm-import-job"),
    path("crm/<uuid:pk>/report/", erp_views.ImportReportView.as_view(), name="erp-crm-import-report"),
    path("crm/<uuid:pk>/progress/", review_views.JobProgressView.as_view(), name="erp-crm-import-progress"),
    path("crm/<uuid:pk>/action/", review_views.JobActionView.as_view(), name="erp-crm-import-action"),
    path("crm/<uuid:pk>/<str:object_type>/", review_views.QueueView.as_view(), name="erp-crm-import-queue"),
    path("crm/<uuid:pk>/<str:object_type>/bulk/", review_views.BulkView.as_view(), name="erp-crm-import-bulk"),
    path("crm/<uuid:pk>/<str:object_type>/rows/<uuid:row>/decide/", review_views.DecideView.as_view(),
         name="erp-crm-import-decide"),
]
