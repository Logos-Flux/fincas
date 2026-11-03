"""``/api/v1/crm/reports/``: the CRM reports as JSON (reporting.api)."""

from django.urls import path

from .api import (
    AttributionReportView,
    CrmReportCatalogView,
    FollowUpReportView,
    FunnelReportView,
    StageDurationReportView,
)

urlpatterns = [
    path("", CrmReportCatalogView.as_view(), name="api-crm-reports"),
    path("funnel/", FunnelReportView.as_view(), name="api-crm-report-funnel"),
    path("stage-duration/", StageDurationReportView.as_view(), name="api-crm-report-stage-duration"),
    path("follow-ups/", FollowUpReportView.as_view(), name="api-crm-report-follow-ups"),
    path("attribution/", AttributionReportView.as_view(), name="api-crm-report-attribution"),
]
