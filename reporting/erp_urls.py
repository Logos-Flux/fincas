from django.urls import path

from .erp_views import CRM_REPORTS, CrmReportView, ReportListView

urlpatterns = [
    path("", ReportListView.as_view(), name="erp-report-list"),
] + [
    path(f"crm/{key.removeprefix('crm_').replace('_', '-')}/",
         CrmReportView.as_view(report_key=key, current_section=section), name=url_name)
    for key, url_name, section, _ in CRM_REPORTS
]
