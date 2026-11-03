"""The CRM import report page (docs/crm/phase5-importer.md 4.4; step 8).

``/erp/imports/crm/<job>/report/`` (step 9 moved it from ``/erp/imports/<job>/report/``, which redirects): the job's report (``crm_import.report.build``)
as it stands, rendered with the ERP base template. Staff only
(``StaffOnlyERPMiddleware`` and the view's own check); the job is looked up
through ``ImportJob.objects.for_request``, so another tenant's job is a 404.
The report holds counts, codes and names only, so the page needs no PII
permission. A dry run's report is a file (``import-report.dry-run.json``);
nothing of it is stored, so this page always shows the database.

Object types are shown through the label layer (``{% label %}``) where one
exists (deals are the tenant's "engagement" word, contacts its "person"...).
The review queues and actions are step 9's.
"""

from __future__ import annotations

from django.contrib.auth.mixins import UserPassesTestMixin
from django.shortcuts import get_object_or_404
from django.views.generic import TemplateView

from core.views.mixins import ERPViewMixin

from . import report
from .models import ImportJob

#: Object type → (label key, plain fallback when the label layer has no key).
TYPE_LABELS: dict[str, tuple[str, str]] = {
    "owner": ("", "Owners"),
    "pipeline": ("crm.pipeline", ""),
    "company": ("parties.organisation", ""),
    "contact": ("parties.person", ""),
    "deal": ("crm.engagement", ""),
    "association": ("", "Associations"),
    "relationship": ("", "Partner relationships"),
    "task": ("crm.task", ""),
    "communication": ("communications.communication", ""),
    "consent": ("", "Consent facts"),
}

STATE_TEXT = {
    "applied": "Applied", "ready": "Ready to apply", "blocked": "Waiting for earlier steps",
    "in_review": "In review", "failed": "Has failed rows", "empty": "No rows",
}


class ImportReportView(ERPViewMixin, UserPassesTestMixin, TemplateView):
    template_name = "erp/crm_import/report.html"
    current_section = "crm_import"  # the CRM Imports nav entry (step 9)
    page_title = "Import report"

    def test_func(self):
        return self.request.user.is_staff

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        job = get_object_or_404(ImportJob.objects.for_request(self.request), pk=self.kwargs["pk"])
        built = report.build(job)
        rows = []
        for otype, item in built["types"].items():
            key, text = TYPE_LABELS[otype]
            rows.append({
                "object_type": otype, "label_key": key, "text": text, "item": item,
                "state_text": STATE_TEXT.get(item["state"], item["state"]),
                "skipped": sum(item["skipped"].values()),
                "failed_or_pending": item["failed"]["total"] + item["dependency_pending"],
            })
        context.update(job=job, built=built, rows=rows, deals=built["deals"], consent=built["consent"],
                       consequence=built["consequence"], totals=built["totals"],
                       uncovered=[r for r in rows if not r["item"]["coverage"]["exact"]])
        return context
