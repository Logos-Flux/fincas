"""Data & Migration back office (Steps 3-4): upload, triage, approve, apply.

The batch detail page is a triage worklist (DD-1), not a table: rows sort
confidence-ascending within kind, the bulk bar names what is
auto-approvable vs needs review, and apply runs one server-side chunk per
POST (DD-8) so refresh/tab-close resumes instead of corrupting.
"""

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import DetailView, FormView, ListView

from core.managers import scope_to_company
from core.views.mixins import ERPViewMixin

from .appliers import get_applier
from .apply import apply_chunk, cancel_run, start_apply_run, undo_run
from .ingest import apply_template, dry_run, ingest_csv
from .models import ImportBatch, StagedRow
from .propose import (
    ProposalError,
    approve_high_confidence,
    approve_rows,
    propose_dedupes,
    propose_mappings,
)

UPLOAD_LIMIT_BYTES = 5 * 1024 * 1024
# Actions that write to the domain: gated by the applier's permission, if any.
APPLY_ACTIONS = {"start-apply", "apply-chunk", "cancel-run", "undo-run"}


class StaffRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_staff


class UnifyBaseMixin(ERPViewMixin, StaffRequiredMixin):
    current_section = "unify"

    def get_company_batches(self):
        return scope_to_company(
            ImportBatch.objects.all(), self.request
        ).order_by("-created_at")


class BatchUploadForm(forms.Form):
    source_name = forms.CharField(
        max_length=255, help_text="Where this export came from."
    )
    csv_file = forms.FileField(help_text="UTF-8 CSV.")

    def clean_csv_file(self):
        uploaded = self.cleaned_data["csv_file"]
        if uploaded.size > UPLOAD_LIMIT_BYTES:
            raise forms.ValidationError(
                f"File is {uploaded.size:,} bytes — the slice cap is "
                f"{UPLOAD_LIMIT_BYTES:,} bytes. Split the export and upload "
                "in parts; each part gets its own reviewable batch."
            )
        if not uploaded.name.lower().endswith(".csv"):
            raise forms.ValidationError("Only .csv uploads in this slice.")
        return uploaded


class BatchListView(UnifyBaseMixin, ListView):
    template_name = "erp/unify_batch_list.html"
    context_object_name = "batches"
    page_title = "Imports"
    paginate_by = 50

    def get_queryset(self):
        return self.get_company_batches()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["upload_form"] = BatchUploadForm()
        return context


class BatchUploadView(UnifyBaseMixin, FormView):
    # POST is the real entry point (the Imports list posts here); a GET, e.g.
    # a bookmarked or reloaded URL, renders the same form on its own page.
    form_class = BatchUploadForm
    template_name = "erp/unify_batch_upload.html"
    page_title = "Stage an import"

    def form_valid(self, form):
        company = self._company()
        if company is None:
            messages.error(self.request, "No company resolved for uploads.")
            return redirect("erp-unify-batch-list")
        try:
            batch = ingest_csv(
                company,
                form.cleaned_data["source_name"],
                form.cleaned_data["csv_file"].read(),
            )
        except ValueError as exc:
            messages.error(self.request, str(exc))
            return redirect("erp-unify-batch-list")
        apply_template(batch)
        return redirect("erp-unify-batch-detail", pk=batch.pk)

    def form_invalid(self, form):
        for error in form.errors.values():
            messages.error(self.request, error)
        return redirect("erp-unify-batch-list")

    def _company(self):
        # Uploads need a company to stage into: the staffer's own, or the
        # single company on a single-tenant install for superusers.
        profile = getattr(
            getattr(self.request.user, "userprofile", None), "company", None
        )
        if profile is not None:
            return profile
        if self.request.user.is_superuser:
            from core.models import Company

            if Company.objects.count() == 1:
                return Company.objects.first()
        return None


class BatchDetailView(UnifyBaseMixin, DetailView):
    template_name = "erp/unify_batch_detail.html"
    context_object_name = "batch"
    page_title = "Import batch"

    def get_object(self, queryset=None):
        return get_object_or_404(
            self.get_company_batches(), pk=self.kwargs["pk"]
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        batch = self.object
        rows = batch.rows.all()
        state = self.request.GET.get("state")
        if state in StagedRow.MappingState.values:
            rows = rows.filter(mapping_state=state)
        # Triage order (DD-1): needs-review first, confidence ascending;
        # approved/applied/NA sink to the bottom.
        context["rows"] = sorted(
            rows.select_related("batch")[:1000],
            key=lambda r: (
                _triage_rank(r.mapping_state),
                float(r.mapping_confidence or 0),
                r.row_number,
            ),
        )
        context["state_filter"] = state or ""
        context["states"] = StagedRow.MappingState.choices
        context["report"] = dry_run(batch)
        context["runs"] = batch.approval_runs.order_by("-created_at")[:10]
        auto = batch.rows.filter(
            mapping_state=StagedRow.MappingState.PROPOSED,
            mapping_confidence__gte=0.95,
        ).count()
        review = batch.rows.filter(
            mapping_state__in=(
                StagedRow.MappingState.PROPOSED,
                StagedRow.MappingState.UNMAPPED,
            )
        ).count()
        context["auto_approvable"] = auto
        context["needs_review"] = review
        return context

    def post(self, request, *args, **kwargs):
        batch = self.get_object()
        action = request.POST.get("action")
        actor = request.user.get_username()
        try:
            self._dispatch(batch, action, request, actor)
        except (ProposalError, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect("erp-unify-batch-detail", pk=batch.pk)

    def _dispatch(self, batch, action, request, actor):
        if action in APPLY_ACTIONS:
            permission = get_applier(batch.entity_kind).permission
            if permission and not request.user.has_perm(permission):
                messages.error(
                    request,
                    f"Applying {batch.entity_kind} rows needs the {permission} permission.",
                )
                return
            refusal = get_applier(batch.entity_kind).refusal(
                batch, "undo" if action == "undo-run" else "apply"
            )
            if refusal:
                messages.error(request, refusal)
                return
        if action == "propose-mappings":
            out = propose_mappings(batch)
            messages.success(
                request,
                f"Mapping proposals: {out['proposed']} proposed, "
                f"{out['skipped']} skipped.",
            )
        elif action == "propose-dedupes":
            out = propose_dedupes(batch)
            messages.success(
                request, f"Dedupe review: {out['proposed']} proposed."
            )
        elif action == "approve-selected":
            count = approve_rows(
                batch, request.POST.getlist("rows"), actor
            )
            messages.success(request, f"Approved {count} rows.")
        elif action == "approve-high-confidence":
            count = approve_high_confidence(
                batch, actor=f"{actor} (band ≥0.95)"
            )
            messages.success(
                request, f"Auto-approved {count} high-confidence rows."
            )
        elif action == "reject-selected" and batch.entity_kind != "product":
            messages.error(request, "Decide these rows on their own review screen.")
        elif action == "reject-selected":
            count = _reject_rows(batch, request.POST.getlist("rows"), actor)
            messages.success(
                request, f"Sent {count} rows back to unmapped."
            )
        elif action == "start-apply":
            run = start_apply_run(batch, actor=actor)
            messages.success(
                request, f"Apply run opened ({run.pk}). Apply by chunk below."
            )
        elif action == "apply-chunk":
            run = batch.approval_runs.get(pk=request.POST.get("run"))
            out = apply_chunk(
                str(run.pk), request.POST.getlist("rows"), chunk_size=200
            )
            applied = sum(
                1 for r in out["results"] if r["state"] == "applied"
            )
            failed = sum(
                1 for r in out["results"] if r["state"] == "failed"
            )
            messages.success(
                request,
                f"Chunk: {applied} applied, {failed} failed, "
                f"{len(out['results']) - applied - failed} skipped.",
            )
        elif action == "cancel-run":
            run = batch.approval_runs.get(pk=request.POST.get("run"))
            cancel_run(str(run.pk))
            messages.success(request, "Run canceled — applied rows stay.")
        elif action == "undo-run":
            run = batch.approval_runs.get(pk=request.POST.get("run"))
            out = undo_run(str(run.pk), request.POST.get("undo_token", ""))
            noun = "products deactivated" if batch.entity_kind == "product" else "rows reverted"
            messages.success(request, f"Undone: {out['undone']} {noun}.")
            for refused in out.get("refused", []):
                messages.error(
                    request, f"Row {refused['row']} not undone: {refused['error']}"
                )
        else:
            messages.error(request, f"Unknown action: {action}.")


def _triage_rank(state: str) -> int:
    return {
        StagedRow.MappingState.UNMAPPED: 0,
        StagedRow.MappingState.PROPOSED: 1,
        StagedRow.MappingState.FAILED: 2,
        StagedRow.MappingState.APPROVED: 3,
        StagedRow.MappingState.APPLIED: 4,
        StagedRow.MappingState.NA: 5,
    }.get(state, 6)


def _reject_rows(batch, row_ids, actor) -> int:
    wanted = {str(pk) for pk in row_ids}
    count = 0
    for row in batch.rows.filter(
        mapping_state=StagedRow.MappingState.PROPOSED
    ):
        if str(row.pk) not in wanted:
            continue
        row.mapping_state = StagedRow.MappingState.UNMAPPED
        row.proposed_mapping = {}
        row.mapping_confidence = None
        row.mapping_actor = actor
        row.error = ""
        row.save(
            update_fields=[
                "mapping_state", "proposed_mapping", "mapping_confidence",
                "mapping_actor", "error", "updated_at",
            ]
        )
        count += 1
    return count
