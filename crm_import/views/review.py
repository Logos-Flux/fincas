"""The CRM import review screens (docs/crm/phase5-importer.md 4.6-4.9; step 9).

Routes, under ``/erp/imports/crm/`` (``crm_import.erp_urls``):

- ``""``: the job list (``ERPListView``: column chooser, saved filters, sort);
- ``<job>/``: the job overview, one card per object type in step order
  (coverage, counts by state, progress, the step's primary action), the
  apply-run progress (polled from ``<job>/progress/``) and the job's actions;
- ``<job>/<type>/``: the review queue (``crm_import.review``);
- ``<job>/<type>/rows/<row>/decide/``, ``<job>/<type>/bulk/``,
  ``<job>/action/``: POST only.

``/erp/`` is staff-only (``StaffOnlyERPMiddleware``); every job is looked up
through ``ImportJob.objects.for_request``, so another tenant's job, and so
its batches and rows, is a 404. Reading needs staff; deciding, claiming,
applying, retrying, undoing and cancelling need ``crm_import.apply_import``;
unmasked values need ``crm_import.view_import_pii``. Every POST is a Django
form with its CSRF token (the keyboard's ``fetch`` sends the form's token).

Apply, retry and undo run **through the step 8 runner** (``crm_import.runner``)
in the request, the way the merge review applies its runs. A step with more
rows than ``WEB_RUN_LIMIT`` is refused here with the command to run instead
(``crm_import run --job <id> --through <type>``): a request is not the place
for a 20,000-row run. The overview polls the open apply runs either way, so
a run started from the CLI shows the same progress.
"""

from __future__ import annotations

import datetime as dt

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from core.labels import get_label
from core.views.list_view import ERPListView
from core.views.mixins import ERPViewMixin
from crm_import import review
from crm_import.models import STEPS, ImportJob, ImportJobBatch
from unify.models import ApprovalRow, ApprovalRun, StagedRow

SECTION = "crm_import"
MS = StagedRow.MappingState
STEP_ORDER = tuple(sorted(STEPS, key=STEPS.get))
WEB_RUN_LIMIT = 5000
APPLY_PERM = "crm_import.apply_import"
PII_PERM = "crm_import.view_import_pii"

#: Object type → (label key, singular, plural) when the label layer has no key.
TYPE_LABELS: dict[str, tuple[str, str, str]] = {
    "owner": ("", "Owner", "Owners"),
    "pipeline": ("crm.pipeline", "Pipeline", "Pipelines"),
    "company": ("parties.organisation", "Company", "Companies"),
    "contact": ("parties.person", "Contact", "Contacts"),
    "deal": ("crm.engagement", "Deal", "Deals"),
    "association": ("", "Association", "Associations"),
    "relationship": ("", "Partner relationship", "Partner relationships"),
    "task": ("crm.task", "Task", "Tasks"),
    "communication": ("communications.communication", "Communication", "Communications"),
    "consent": ("", "Consent fact", "Consent facts"),
}
STATE_WORD = {"approved": "approved", "na": "skipped (N/A)", "proposed": "still undecided", "failed": "failed",
              "applied": "applied"}
STATUS_COLORS = {
    "validating": "info", "invalid": "danger", "staging": "info", "reviewing": "warning",
    "applying": "info", "applied": "success", "partially_applied": "warning", "undoing": "info",
    "undone": "secondary", "cancelled": "secondary",
}


def type_labels(otype: str, company_id=None) -> tuple[str, str]:
    key, singular, plural = TYPE_LABELS[otype]
    if key:
        try:
            label = get_label(key, company=company_id)
            return label.singular, label.plural
        except Exception:  # noqa: BLE001 — an unregistered key falls back to the plain noun
            pass
    return singular, plural


def scoped_job(request, pk) -> ImportJob:
    return get_object_or_404(ImportJob.objects.for_request(request).select_related("company"), pk=pk)


def job_batch(job, otype: str):
    if otype not in STEPS:
        raise Http404("Not an object type.")
    link = ImportJobBatch._base_manager.select_related("batch").filter(job=job, object_type=otype).first()
    if link is None or link.batch.company_id != job.company_id:
        raise Http404("This job has no batch of that type.")
    return link.batch


def viewer_zone(request):
    from crm.erp_views import viewer_zone as zone

    return zone(request)


def today_start(request, now):
    zone = viewer_zone(request)
    local = now.astimezone(zone)
    return dt.datetime.combine(local.date(), dt.time.min, tzinfo=zone)


def can_apply(request) -> bool:
    return request.user.has_perm(APPLY_PERM)


#: Job states whose rows may be decided. Not while validating or staging (rows
#: are still moving), applying or undoing (a run may be writing them), nor once
#: the job is invalid or cancelled (its staged rows are the record).
REVIEWABLE = (ImportJob.Status.REVIEWING, ImportJob.Status.PARTIALLY_APPLIED, ImportJob.Status.APPLIED,
              ImportJob.Status.UNDONE)


def can_decide(request, job) -> bool:
    return can_apply(request) and job.status in REVIEWABLE


def _require_reviewable(job):
    if job.status not in REVIEWABLE:
        raise review.ReviewError(f"This job is {job.get_status_display().lower()}: its rows cannot be decided now.")


def coverage_of(job, otype: str) -> dict:
    entry = ((job.manifest or {}).get("objects") or {}).get(otype) or {}
    coverage = entry.get("coverage") or "unknown"
    reasons = [r for r in entry.get("reasons") or [] if isinstance(r, dict)]
    return {"coverage": coverage, "exact": coverage == "complete",
            "reasons": reasons, "reason_text": "; ".join(r.get("detail") or r.get("code") or "" for r in reasons)}


# --- the job list -------------------------------------------------------------------------


class JobListView(ERPListView):
    """``/erp/imports/crm/``: every CRM import job of the tenant."""

    model = ImportJob
    template_name = "erp/crm_import/job_list.html"
    current_section = SECTION
    page_title = "CRM imports"
    entity_name = "CRM import"
    list_id = "crm_import.importjob"
    ordering = ["-created_at"]
    search_fields = ["source"]
    list_columns = [
        {"field": "source", "label": "Source", "link": True, "link_url_name": "erp-crm-import-job", "priority": 1},
        {"field": "status", "label": "Status", "badge": True, "priority": 1},
        {"field": "coverage", "label": "Coverage", "priority": 2},
        {"field": "provider", "label": "Provider", "priority": 3},
        {"field": "created_at", "label": "Created", "date_only": True, "priority": 2},
        {"field": "created_by__username", "label": "Created by", "priority": 3},
    ]
    list_filters = [
        {"field": "status", "label": "Status", "choices": ImportJob.Status.choices},
        {"field": "coverage", "label": "Coverage", "choices": ImportJob.Coverage.choices},
    ]
    STATUS_COLORS = STATUS_COLORS

    def get_queryset(self):
        self.queryset = ImportJob.objects.for_request(self.request).select_related("created_by")
        return super().get_queryset()


# --- the job overview -----------------------------------------------------------------------


def open_runs(job) -> list[dict]:
    """The job's apply runs still open, with chunk progress (4.9; one query)."""
    from unify.apply import CHUNK_SIZE

    runs = (ApprovalRun.objects.filter(batch__crm_import_job_batch__job=job, kind=ApprovalRun.Kind.APPLY,
                                       status__in=(ApprovalRun.Status.OPEN, ApprovalRun.Status.APPLYING))
            .annotate(total=Count("rows"),
                      pending=Count("rows", filter=Q(rows__state=ApprovalRow.State.PENDING)))
            .values("pk", "batch__crm_import_job_batch__object_type", "total", "pending", "status")
            .order_by("created_at"))
    out = []
    for run in runs:
        total, done = run["total"], run["total"] - run["pending"]
        chunks = max(1, -(-total // CHUNK_SIZE))
        out.append({"run": str(run["pk"]), "object_type": run["batch__crm_import_job_batch__object_type"],
                    "total": total, "done": done, "chunk": min(chunks, done // CHUNK_SIZE + 1), "chunks": chunks,
                    "percent": int(100 * done / total) if total else 100})
    return out


def merge_hints(job) -> int:
    """Applied people and organisations the importer thinks duplicate each other (3.3)."""
    return StagedRow.objects.filter(
        batch__crm_import_job_batch__job=job, batch__crm_import_job_batch__object_type__in=("contact", "company"),
        mapping_state=MS.APPLIED, proposed_mapping__possible_duplicate_of__isnull=False,
    ).exclude(proposed_mapping__possible_duplicate_of=[]).count()


def step_cards(job, counts, *, company_id) -> list[dict]:
    from crm_import import runner

    cards = []
    for otype in STEP_ORDER:
        c = counts.get(otype, {})
        total = sum(c.values())
        singular, plural = type_labels(otype, company_id)
        proposed = c.get(MS.PROPOSED, 0) + c.get(MS.UNMAPPED, 0)
        approved, applied, failed, na = (c.get(MS.APPROVED, 0), c.get(MS.APPLIED, 0), c.get(MS.FAILED, 0),
                                         c.get(MS.NA, 0))
        gate = runner.gate(job, otype, counts) if approved else {}
        undo_first = runner.undo_blockers(job, otype, counts) if applied else {}
        if proposed:
            primary = "review"
        elif approved:
            primary = "blocked" if gate else "apply"
        elif failed:
            primary = "retry"
        elif applied:
            primary = "undo"
        else:
            primary = ""

        def pct(n, total=total):
            return round(100 * n / total, 1) if total else 0

        cards.append({
            "object_type": otype, "step": STEPS[otype], "singular": singular, "plural": plural,
            "coverage": coverage_of(job, otype), "total": total, "proposed": proposed, "approved": approved,
            "applied": applied, "failed": failed, "skipped": na,
            "decided_percent": int(pct(total - proposed)),
            "bar": [("applied", "Applied", applied, pct(applied)), ("approved", "Approved", approved, pct(approved)),
                    ("skipped", "Skipped", na, pct(na)), ("failed", "Failed", failed, pct(failed)),
                    ("remaining", "To review", proposed, pct(proposed))],
            "gate": sorted(gate), "undo_first": sorted(undo_first), "primary": primary,
            "too_big": approved + failed > WEB_RUN_LIMIT,
        })
    return cards


class JobOverviewView(ERPViewMixin, TemplateView):
    """``/erp/imports/crm/<job>/``: the job, step by step (4.6, 4.9)."""

    template_name = "erp/crm_import/job_overview.html"
    current_section = SECTION
    page_title = "CRM import"

    def get_context_data(self, **kwargs):
        from crm_import import runner

        context = super().get_context_data(**kwargs)
        job = scoped_job(self.request, self.kwargs["pk"])
        counts = runner.state_counts(job)
        cards = step_cards(job, counts, company_id=job.company_id)
        applied_all = job.status == ImportJob.Status.APPLIED
        context.update(
            job=job, cards=cards, runs=open_runs(job), can_apply=can_apply(self.request),
            status_color=STATUS_COLORS.get(job.status, "secondary"),
            uncovered=[c for c in cards if not c["coverage"]["exact"]],
            merge_hints=merge_hints(job) if job.status in (ImportJob.Status.APPLIED,
                                                           ImportJob.Status.PARTIALLY_APPLIED) else 0,
            applied_all=applied_all, web_run_limit=WEB_RUN_LIMIT,
            can_cancel=job.status in runner.CANCELLABLE,
            can_undo_job=job.status in runner.UNDOABLE and any(c["applied"] for c in cards),
            page_title=f"CRM import · {job.source}",
        )
        return context


class JobProgressView(ERPViewMixin, View):
    """``<job>/progress/``: the open apply runs, as JSON, for the overview's poll."""

    def get(self, request, pk):
        job = scoped_job(request, pk)
        return JsonResponse({"status": job.status, "runs": open_runs(job)})


class JobActionView(ERPViewMixin, View):
    """POST ``<job>/action/``: apply / retry / undo a step, undo or cancel the job."""

    ACTIONS = ("apply", "retry", "undo_step", "undo_job", "cancel")

    def post(self, request, pk):
        from crm_import import runner

        job = scoped_job(request, pk)
        back = reverse("erp-crm-import-job", args=[job.pk])
        if not can_apply(request):
            raise PermissionDenied("Applying and undoing CRM imports needs “Can apply and undo CRM imports”.")
        action = request.POST.get("action", "")
        otype = request.POST.get("object_type", "")
        if action not in self.ACTIONS or (action in ("apply", "retry", "undo_step") and otype not in STEPS):
            return HttpResponseBadRequest("Unknown action.")
        actor = request.user.get_username()
        counts = runner.state_counts(job)
        try:
            if action in ("apply", "retry"):
                upto = [t for t in STEP_ORDER if STEPS[t] <= STEPS[otype]]
                rows = sum(counts[t].get(MS.APPROVED if action == "apply" else MS.FAILED, 0) for t in upto)
                if rows > WEB_RUN_LIMIT:
                    messages.warning(request, f"{rows} rows are too many to {action} from the browser (limit "
                                              f"{WEB_RUN_LIMIT}). Run `manage.py crm_import {'run' if action == 'apply' else 'retry'} "
                                              f"--job {job.pk} --through {otype}` on the server; this page shows "
                                              "its progress.")
                    return redirect(back)
                codes = [c for c in request.POST.getlist("code") if c] or None
                result = (runner.run_job(job, actor=actor, through=otype) if action == "apply"
                          else runner.retry_job(job, actor=actor, codes=codes, through=otype))
                applied = sum(s.applied for s in result.steps)
                failed = sum(s.failed for s in result.steps)
                text = f"Applied {applied} row{'s' if applied != 1 else ''}" + (f", {failed} failed" if failed else "")
                if result.blocked is not None:
                    messages.warning(request, f"{text}. {result.blocked}")
                else:
                    messages.success(request, text + ".")
            elif action == "undo_step":
                out = runner.undo_batch(job, otype, actor=actor)
                self._undo_message(request, out)
            elif action == "undo_job":
                out = runner.undo_job(job, actor=actor)
                self._undo_message(request, out)
            else:
                runner.cancel_job(job)
                messages.success(request, "Job canceled. Its staged rows stay as the record.")
        except runner.RunnerError as exc:
            messages.error(request, str(exc))
        return redirect(back)

    @staticmethod
    def _undo_message(request, out):
        text = f"Undid {out.undone_rows} row{'s' if out.undone_rows != 1 else ''}."
        if out.refused:
            messages.warning(request, f"{text} {len(out.refused)} refused (changed since the import); "
                                      "they stay applied. The report lists them.")
        else:
            messages.success(request, text)


# --- the queue ----------------------------------------------------------------------------


def queue_context(request, job, otype: str, batch, filters: review.Filters, *, now=None) -> dict:
    now = now or timezone.now()
    username = request.user.get_username()
    session_start = request.user.last_login
    h = review.header(batch, username=username, now=now, today_start=today_start(request, now),
                      session_start=session_start)
    singular, plural = type_labels(otype, job.company_id)
    can_pii = request.user.has_perm(PII_PERM)
    try:
        rows, nxt = review.queue_rows(batch, filters, username=username, now=now)
    except review.CursorError:
        messages.warning(request, "That page link is not valid; showing the first page.")
        filters.after = ""
        rows, nxt = review.queue_rows(batch, filters, username=username, now=now)
    for row in rows:
        row.batch = batch  # no query per row for row_object_type
    view_count = review.filtered(batch, filters, username=username, now=now).aggregate(
        n=Count("id"), eligible=Count("id", filter=review.eligible_q(username, now)))
    return {
        "job": job, "batch": batch, "object_type": otype, "singular": singular, "plural": plural,
        "header": h, "filters": filters, "items": review.present(rows, job=job, otype=otype, type_label=singular,
                                                                 can_pii=can_pii, username=username, now=now),
        "next_query": filters.query(after=nxt) if nxt else "", "first_query": filters.query(),
        "view_query": filters.query(after=filters.after), "view_count": view_count["n"],
        "eligible": view_count["eligible"], "coverage": coverage_of(job, otype),
        "can_apply": can_decide(request, job), "can_run": can_apply(request), "can_pii": can_pii,
        "frozen": job.status not in REVIEWABLE, "failed_codes": failed_codes(batch) if h["failed"] else [],
        **review.ai_context(batch, job, filters, otype=otype, username=username, now=now),
        "states": list(review.STATE_LABELS.items()), "decisions": list(review.DECISION_LABELS.items()),
        "bands": list(review.BAND_LABELS.items()), "flag_text": review.FLAG_TEXT,
        "skip_reasons": review.SKIP_REASONS, "threshold": review.THRESHOLD,
        "claim_ttl_minutes": int(review.CLAIM_TTL.total_seconds() // 60), "claim_batch": review.CLAIM_BATCH,
        "chip_flags": [(f, review.FLAG_TEXT.get(f, f), n) for f, n in h["flags"].items()],
        "is_first_page": not filters.after,
        "chips": chips(filters, h, job=job),
        "clear_url": queue_url(job, otype),
        "base_url": queue_url(job, otype),
    }


def failed_codes(batch) -> list[tuple[str, int]]:
    """Failed rows by error code (the part before ``:``), for "Retry failed" by code."""
    counts: dict[str, int] = {}
    for error in StagedRow.objects.filter(batch=batch, mapping_state=MS.FAILED).values_list("error", flat=True):
        code = (error or "").split(":", 1)[0].strip() or "unknown"
        counts[code] = counts.get(code, 0) + 1
    return sorted(counts.items())


def chips(filters: review.Filters, h: dict, *, job=None) -> list[dict]:
    """The filter chips with their counts (4.6, 4.9: "R6: 41 left"). An
    active chip's link clears that filter."""
    def chip(label, n, key, value):
        active = (filters.mine if key == "mine" else getattr(filters, key) == value)
        return {"label": label, "count": n, "active": active,
                "query": filters.query(**{key: "" if active else value, "after": ""})}

    groups = [
        ("Decision", [chip(review.DECISION_LABELS[d], h["decisions"][d], "decision", d)
                      for d in review.DECISIONS if h["decisions"][d]]),
        ("Confidence", [chip(review.BAND_LABELS[b], h["bands"][b], "band", b) for b in review.BANDS if h["bands"][b]]),
        ("Flag", [chip(review.FLAG_TEXT.get(f, f), n, "flag", f) for f, n in h["flags"].items()]),
        ("Claims", [chip("Assigned to me", h["mine"], "mine", "1")]),
    ]
    return [{"title": title, "items": items} for title, items in groups if items]



class QueueView(ERPViewMixin, TemplateView):
    """``/erp/imports/crm/<job>/<type>/``: the review queue (4.6)."""

    template_name = "erp/crm_import/queue.html"
    current_section = SECTION

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        job = scoped_job(self.request, self.kwargs["pk"])
        otype = self.kwargs["object_type"]
        batch = job_batch(job, otype)
        filters = review.Filters.from_query(self.request.GET)
        context.update(queue_context(self.request, job, otype, batch, filters))
        context["page_title"] = f"Review {context['plural'].lower()} · {job.source}"
        return context


def wants_json(request) -> bool:
    return "application/json" in request.headers.get("Accept", "")


def _require_apply(request):
    if not can_apply(request):
        raise PermissionDenied("Deciding import rows needs “Can apply and undo CRM imports”.")


def queue_url(job, otype, query="", anchor=""):
    url = reverse("erp-crm-import-queue", args=[job.pk, otype])
    return url + (f"?{query}" if query else "") + anchor


class DecideView(ERPViewMixin, View):
    """POST one row's decision (4.6). JSON for the keyboard's ``fetch``
    (the new header counts and the re-rendered row), else a redirect."""

    def post(self, request, pk, object_type, row):
        job = scoped_job(request, pk)
        batch = job_batch(job, object_type)
        get_object_or_404(StagedRow.objects.only("pk"), batch=batch, pk=row)
        _require_apply(request)
        if job.status not in REVIEWABLE:
            if wants_json(request):
                return JsonResponse({"ok": False, "message": f"This job is {job.get_status_display().lower()}: "
                                                             "its rows cannot be decided now."}, status=409)
            messages.error(request, f"This job is {job.get_status_display().lower()}: its rows cannot be decided now.")
            return redirect(queue_url(job, object_type))
        filters = review.Filters.from_query(_query_dict(request.POST.get("query", "")))
        action = request.POST.get("action", "")
        candidate = request.POST.get("candidate") or None
        if request.POST.get("pick"):  # "Pick candidate…": the button carries the candidate id
            action, candidate = "pick", request.POST["pick"]
        keep, take = [], []
        for key, value in request.POST.items():
            if key.startswith("field:"):
                (keep if value == "keep" else take if value == "take" else []).append(key.removeprefix("field:"))
        stage_outcomes = {k.removeprefix("stage_outcome:"): v for k, v in request.POST.items()
                          if k.startswith("stage_outcome:")}
        actor = request.user.get_username()
        error = ""
        try:
            review.decide(batch, row, otype=object_type, actor=actor, action=action,
                          candidate=candidate, keep=keep, take=take,
                          outcome=request.POST.get("outcome") or None, stage_outcomes=stage_outcomes,
                          reason=request.POST.get("reason") or "rejected", seen=request.POST.get("seen") or None)
        except review.ReviewError as exc:
            error = str(exc)
        if wants_json(request):
            return self._json(request, job, object_type, batch, row, filters, error)
        if error:
            messages.error(request, error)
            return redirect(queue_url(job, object_type, filters.query(after=filters.after), f"#row-{row}"))
        messages.success(request, "Saved.")
        return redirect(queue_url(job, object_type, filters.query(after=filters.after),
                                  f"#row-{request.POST.get('next') or row}"))

    def _json(self, request, job, otype, batch, row_pk, filters, error):
        now = timezone.now()
        username = request.user.get_username()
        h = review.header(batch, username=username, now=now, today_start=today_start(request, now),
                          session_start=request.user.last_login)
        row = StagedRow.objects.get(pk=row_pk)
        row.batch = batch
        singular, plural = type_labels(otype, job.company_id)
        item = review.present([row], job=job, otype=otype, type_label=singular,
                              can_pii=request.user.has_perm(PII_PERM), username=username, now=now)[0]
        ctx = {"job": job, "object_type": otype, "item": item, "can_apply": can_decide(request, job),
               "can_pii": request.user.has_perm(PII_PERM), "skip_reasons": review.SKIP_REASONS,
               "view_query": filters.query(after=filters.after), "ai_on": bool((job.options or {}).get("ai_match")),
               "singular": singular, "plural": plural, "header": h, "batch": batch}
        return JsonResponse({
            "ok": not error, "message": error or f"Row {row.row_number}: {STATE_WORD.get(row.mapping_state, row.mapping_state)}.",
            "state": row.mapping_state, "header": review.header_json(h),
            "row_html": render_to_string("erp/crm_import/_queue_row.html", ctx, request=request),
            "header_html": render_to_string("erp/crm_import/_queue_header.html", ctx, request=request),
        }, status=200 if not error else 409 if "claimed by" in error else 400)


def _query_dict(text: str):
    from django.http import QueryDict

    return QueryDict(text or "")


class BulkView(ERPViewMixin, View):
    """POST the queue's bulk actions (4.8) and claims (4.9)."""

    ACTIONS = ("approve_eligible", "approve_selected", "create_selected", "skip_selected", "rematch_selected",
               "rematch_batch", "claim", "release", "ai_suggest")

    def post(self, request, pk, object_type):
        job = scoped_job(request, pk)
        batch = job_batch(job, object_type)
        _require_apply(request)
        action = request.POST.get("action", "")
        if action not in self.ACTIONS:
            return HttpResponseBadRequest("Unknown action.")
        filters = review.Filters.from_query(_query_dict(request.POST.get("query", "")))
        back = queue_url(job, object_type, filters.query(after=filters.after))
        ids = request.POST.getlist("ids")
        actor = request.user.get_username()
        # AuditLog.user is a UserProfile; a superuser acting in another tenant leaves it empty.
        profile = getattr(request.user, "userprofile", None)
        user = profile if profile is not None and profile.company_id == job.company_id else None
        singular, plural = type_labels(object_type, job.company_id)
        try:
            _require_reviewable(job)
            if action == "approve_eligible":
                out = review.approve_eligible(batch, job, filters, otype=object_type, actor=actor, user=user)
                messages.success(request, out.message("Approved"))
            elif action in ("approve_selected", "create_selected", "skip_selected"):
                if not ids:
                    messages.warning(request, "Nothing selected: tick the rows first.")
                    return redirect(back)
                if action == "approve_selected" and request.POST.get("confirm") != "1":
                    flagged = review.flagged_among(batch, ids)
                    if flagged:
                        return render(request, "erp/crm_import/bulk_confirm.html", {
                            "job": job, "object_type": object_type, "plural": plural, "ids": ids,
                            "flagged": [(n, [review.FLAG_TEXT.get(f, f) for f in fl]) for n, fl in flagged],
                            "query": request.POST.get("query", ""), "current_section": SECTION,
                            "page_title": "Confirm approval", "back": back,
                        })
                verb = {"approve_selected": "approve", "create_selected": "create", "skip_selected": "skip"}[action]
                out = review.bulk_decide(batch, job, ids, otype=object_type, actor=actor, action=verb,
                                         reason=request.POST.get("reason", ""), user=user)
                messages.success(request, out.message({"approve": "Approved", "create": "Set to create new for",
                                                       "skip": "Skipped"}[verb]))
            elif action in ("rematch_selected", "rematch_batch"):
                if action == "rematch_selected" and not ids:
                    messages.warning(request, "Nothing selected: tick the rows first.")
                    return redirect(back)
                out = review.rematch(batch, job, ids if action == "rematch_selected" else None,
                                     otype=object_type, actor=actor, user=user, limit=WEB_RUN_LIMIT)
                messages.success(request, f"Re-ran matching on {out.done} undecided row{'s' if out.done != 1 else ''}.")
            elif action == "ai_suggest":
                out, result = review.run_ai(batch, job, filters, otype=object_type, actor=actor, user=user)
                if not result.eligible:
                    messages.info(request, "No undecided row in this view needs an AI suggestion.")
                else:
                    messages.success(request, f"AI suggestions for {out.done} of {result.eligible} row"
                                              f"{'s' if result.eligible != 1 else ''} ({result.disagrees} disagree"
                                              f"{'s' if result.disagrees == 1 else ''} with the rule; "
                                              f"{result.calls} call{'s' if result.calls != 1 else ''}, "
                                              f"{max(0, result.budget - result.calls_used)} left). "
                                              "Suggestions never decide a row.")
                if out.refused:
                    messages.warning(request, f"{out.refused} answer{'s' if out.refused != 1 else ''} could not be "
                                              "used (malformed, unknown candidate or gateway error): those rows "
                                              "are as they were.")
                if result.stopped:
                    messages.warning(request, "The AI pass stopped early ("
                                              + ("the gateway kept failing" if result.stopped == "gateway_failing"
                                                 else "time limit for one click") + "): try again later, or run "
                                              f"`manage.py crm_import ai --job {job.pk}` on the server.")
                if result.budget_exhausted:
                    messages.warning(request, "The AI call budget of this batch is spent: the rows past it have "
                                              "no suggestion (GOLDEN_CRM_IMPORT['ai_budget_per_batch']).")
            elif action == "claim":
                out = review.claim_next(batch, job, filters, actor=actor, user=user)
                messages.success(request, f"Claimed {out.done} row{'s' if out.done != 1 else ''} for "
                                          f"{int(review.CLAIM_TTL.total_seconds() // 60)} minutes.")
                back = queue_url(job, object_type, review.Filters(**{**filters.__dict__, "mine": True,
                                                                     "after": ""}).query())
            else:
                out = review.release_claims(batch, job, actor=actor, user=user)
                messages.success(request, f"Released {out.done} claim{'s' if out.done != 1 else ''}.")
        except review.ReviewError as exc:
            messages.error(request, str(exc))
        return redirect(back)
