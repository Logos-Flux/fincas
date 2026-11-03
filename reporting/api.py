"""The CRM reports as read-only JSON (docs/crm/phase6-adapters.md 3.6, step 7).

``/api/v1/crm/reports/`` answers what the report pages under
``/erp/reports/crm/`` show, for the assistant and other machines. No report
logic lives here: the cohorts, the computations and the typed tables are
``reporting.crm_reports``; this module parses parameters, pins one tenant and
adds a deterministic one-paragraph ``summary``.

Rules:

- **Same gate as the pages.** ``PrincipalPermission`` answers a human exactly
  as ``StaffOnlyERPMiddleware`` answers ``/erp/`` (``is_staff_principal``). A
  service account reads by its contract: ``crm.view_engagement`` for the
  catalog, the funnel and stage duration; follow-ups need ``crm.view_task``
  and ``crm.view_engagement`` (they count open engagements too).
- **One tenant, so "the page's numbers" means a tenant user's page.** A
  superuser's page mixes every tenant; the API answers for the one named.
- **One tenant per answer.** A tenant user (or a pinned service account)
  reads its own company; naming another is 404. A principal that reads every
  tenant (a superuser, the shared assistant key) must name one with
  ``company=<uuid>`` unless only one company exists.
- **Canonical, never labels.** Column names are the CSV's, values typed
  (``crm_reports`` "machine-facing tables"); the summary uses canonical nouns
  (engagement, stage, task) and carries no name, email or phone: reports are
  aggregates. ``core/tests/test_labels_never_leak.py`` scans this module.
- **Periods are the page's**: ``period`` (``today`` / ``week`` / ``month`` /
  ``quarter`` / ``year``, default ``month``) or an explicit
  ``date_from``/``date_to`` pair, as local days in the site time zone (the
  ``timezone`` field says which). At most 366 days. An unknown parameter or
  value is 400 with the allowed list.
"""

from __future__ import annotations

import datetime
import uuid

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import APIException, NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from core.managers import scope_to_company
from core.models import Company
from core.permissions import PrincipalPermission
from core.templatetags.erp_tags import money
from core.views import request_company
from crm.models import Engagement, Pipeline, Task, Touchpoint

from . import crm_reports as reports

TAG = "crm"
MAX_SPAN_DAYS = 366
NO_COMPANY = "This account has no company, so the report is empty."
COMMON_PARAMS = ("company", "period", "date_from", "date_to", "format")


class ReportParamError(APIException):
    """400 with a machine-readable ``code`` and, where it helps, ``allowed``."""

    status_code = 400
    default_code = "invalid_parameter"

    def __init__(self, param, message, *, allowed=None, code="invalid_parameter"):
        super().__init__()
        self.detail = {"detail": message, "code": code, "param": param}
        if allowed is not None:
            self.detail["allowed"] = list(allowed)


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def report_company(request):
    """The one tenant this answer covers, or None for an account with no
    company (its querysets are empty, as on the page).

    Raises 400 for a malformed id, 404 for a company outside the caller's
    scope, 400 when a cross-tenant principal names none and several exist.
    """
    raw = request.query_params.get("company") or ""
    if raw and not _is_uuid(raw):
        raise ReportParamError("company", "company must be a tenant id (uuid).")
    if request.user.is_superuser:
        if raw:
            company = Company.objects.filter(pk=raw).first()
            if company is None:
                raise NotFound("No such company.")
            return company
        only = list(Company.objects.all()[:2])
        if len(only) == 1:
            return only[0]
        raise ReportParamError(
            "company", "This account reads every tenant: name one with company=<uuid>.",
            code="company_required",
        )
    own = request_company(request)
    if raw and (own is None or str(own.pk) != str(uuid.UUID(raw))):
        raise NotFound("No such company.")
    return own


def _parse_date(request, name):
    raw = request.query_params.get(name) or ""
    try:  # the page's format exactly (fromisoformat also takes 20260101, 2026-W01-1)
        return datetime.datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise ReportParamError(name, f"{name} must be a date (YYYY-MM-DD).") from None


def report_period(request) -> dict:
    """``{period, date_from, date_to}``: an explicit pair, else the period."""
    has_from = "date_from" in request.query_params
    has_to = "date_to" in request.query_params
    period = request.query_params.get("period") or reports.DEFAULT_PERIOD
    if period not in reports.PERIODS:
        raise ReportParamError("period", "Unknown period.", allowed=reports.PERIODS)
    if has_from or has_to:
        if not (has_from and has_to):
            raise ReportParamError("date_from" if not has_from else "date_to",
                                   "Pass date_from and date_to together, or a period.")
        date_from, date_to = _parse_date(request, "date_from"), _parse_date(request, "date_to")
        period = None
    else:
        date_from, date_to = reports.period_range(period, timezone.localdate())
    if date_from > date_to:
        raise ReportParamError("date_from", "date_from is after date_to.")
    if (date_to - date_from).days + 1 > MAX_SPAN_DAYS:
        raise ReportParamError("date_to", f"A report covers at most {MAX_SPAN_DAYS} days.")
    return {"period": period, "date_from": date_from, "date_to": date_to}


def _scoped(queryset, request, company):
    """Fail-closed scoping (``scope_to_company``), pinned to ``company``."""
    queryset = scope_to_company(queryset, request)
    return queryset.filter(company=company) if company is not None else queryset.none()


# --- summaries: deterministic English over the typed data -------------------------------


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _pct(value) -> str:
    return "n/a" if value is None else f"{value}%"


def _money(parts) -> str:
    out = []
    for part in parts:
        amount = money(part["amount"], part["currency"])
        if part["lower_bound"]:
            missing = part["won"] - part["won_amount_known"]
            amount += (f" (lower bound: {missing} of {_plural(part['won'], 'won engagement')} "
                       f"{'has' if missing == 1 else 'have'} no amount)")
        out.append(amount)
    return "; ".join(out)


def _span(params) -> str:
    return f"from {params['date_from']} to {params['date_to']}"


def _source_word(key) -> str:
    """A registered source key, else a neutral word: ``source`` is free text
    when written by an import or ``update()``, so it is never echoed raw."""
    from crm.registries import ENGAGEMENT_SOURCES

    if not key:
        return "unknown"
    return key if ENGAGEMENT_SOURCES.get(key) is not None else "an unregistered source"


def summarize_funnel(params, table) -> str:
    t = table["totals"]
    by = "first-touch channel" if params["attribution"] == "first_touch" else "engagement source"
    text = (f"{_plural(t['created'], 'engagement')} created {_span(params)}: {t['won']} won, "
            f"{t['lost']} lost, {t['not_counted']} closed without counting as a loss, "
            f"{t['open']} open; {t['reached_qualification']} reached qualification. "
            f"Win rate {_pct(t['win_rate'])}.")
    if t["won_value"]:
        text += f" Won value {_money(t['won_value'])}; no FX conversion."
    if table["rows"]:
        top = table["rows"][0]
        text += (f" Largest {by}: {_source_word(top['source'])} "
                 f"({_plural(top['created'], 'engagement')}).")
    return text


def summarize_stages(params, table) -> str:
    pipeline = params.get("pipeline") or {}
    if not pipeline:
        return f"No pipeline in this tenant, so no stage report {_span(params)}."
    rows, cov = table["rows"], table["coverage"]
    text = (f"Pipeline {pipeline['key']}, {_span(params)}: "
            f"{_plural(table['totals']['entered'], 'stage entry', 'stage entries')} "
            f"across {_plural(len(rows), 'stage')}.")
    timed = [r for r in rows if r["median_days"] is not None]
    if timed:
        slowest = max(timed, key=lambda r: (float(r["median_days"]), -rows.index(r)))
        text += f" Longest median time in stage: {slowest['stage']} ({slowest['median_days']} days)."
    waiting = sum(r["still_in_stage"] for r in rows)
    if waiting:
        text += f" {_plural(waiting, 'visit')} still in stage."
    if cov["no_history_excluded"]:
        n = cov["no_history_excluded"]
        text += (f" {_plural(n, 'engagement')} excluded: no observed stage history, "
                 "so time in stage is unknown.")
    if cov["reconstructed_visits"]:
        n = cov["reconstructed_visits"]
        text += f" {_plural(n, 'visit')} began or ended at reconstructed history and {'was' if n == 1 else 'were'} not timed."
    return text


def summarize_follow_ups(params, table) -> str:
    t = table["totals"]
    return (f"{_plural(t['due'], 'task')} due {_span(params)}: "
            f"{t['completed_on_time']} completed on time, {t['completed_late']} late, "
            f"{t['open_overdue']} open and overdue, {t['open_not_yet_due']} not yet due, "
            f"{t['cancelled']} canceled; on-time rate {_pct(t['on_time_rate'])}; "
            f"{_plural(t['times_rescheduled'], 'reschedule')}. "
            f"Now: {_plural(t['open_engagements'], 'open engagement')}, "
            f"{t['without_next_action']} without a next action, "
            f"{t['overdue_next_actions']} with an overdue next action.")


def summarize_attribution(params, table) -> str:
    t, cov = table["totals"], table["coverage"]
    text = (f"{_plural(cov['won'], 'engagement')} won {_span(params)}, "
            f"{cov['no_touch']} without a touchpoint.")
    if table["rows"]:
        top = table["rows"][0]
        text += (f" Most first touches: {_source_word(top['channel'])} ({top['first_touch']} of "
                 f"{t['first_touch']}); linear credit {top['linear']}.")
    if t["first_touch_won_value"]:
        text += f" Won value {_money(t['first_touch_won_value'])}; no FX conversion."
    return text


# --- the views -----------------------------------------------------------------------------


class _CrmReportView(APIView):
    permission_classes = [PrincipalPermission]
    #: Names the model for the service contract (``view_engagement``).
    queryset = Engagement.objects.none()
    report = ""
    params: tuple[str, ...] = ()
    #: Further model permissions a service account needs on top of the contract.
    service_perms: tuple[str, ...] = ()

    def check_permissions(self, request):
        super().check_permissions(request)
        from core.service_accounts import is_service_account

        if is_service_account(request.user) and not all(
            request.user.has_perm(p) for p in self.service_perms
        ):
            self.permission_denied(request, message="This report needs " + ", ".join(self.service_perms))

    def check_params(self, request):
        allowed = COMMON_PARAMS + self.params
        for name in request.query_params:
            if name not in allowed:
                raise ReportParamError(name, f"Unknown parameter {name!r}.", allowed=allowed,
                                       code="unknown_parameter")

    def build(self, request, company, period) -> tuple[dict, dict]:
        raise NotImplementedError

    def get(self, request):
        self.check_params(request)
        period = report_period(request)
        company = report_company(request)
        extra, table = self.build(request, company, period)
        params = {"period": period["period"], "date_from": period["date_from"].isoformat(),
                  "date_to": period["date_to"].isoformat(), **extra}
        summary = SUMMARIES[self.report](params, table)
        if company is None:
            # Fail closed, as on the page, but never let an empty answer read as "nothing happened".
            table["notes"] = [{"code": "no_company", "text": NO_COMPANY}, *table["notes"]]
            summary = f"{NO_COMPANY} {summary}"
        return Response({
            "report": self.report,
            "company": str(company.pk) if company is not None else None,
            "timezone": timezone.get_current_timezone_name(),
            "params": params,
            **table,
            "summary": summary,
        })


FUNNEL_PARAMS = {"attribution": reports.ATTRIBUTIONS}


@extend_schema(tags=[TAG], parameters=[
    OpenApiParameter("company", str), OpenApiParameter("period", str, enum=reports.PERIODS),
    OpenApiParameter("date_from", str), OpenApiParameter("date_to", str),
    OpenApiParameter("attribution", str, enum=reports.ATTRIBUTIONS),
], responses={200: dict})
class FunnelReportView(_CrmReportView):
    """Engagements created in the period, by source (the funnel page)."""

    report = "funnel"
    params = ("attribution",)

    def build(self, request, company, period):
        attribution = request.query_params.get("attribution") or "engagement"
        if attribution not in reports.ATTRIBUTIONS:
            raise ReportParamError("attribution", "Unknown attribution.", allowed=reports.ATTRIBUTIONS)
        engagements = _scoped(Engagement.objects.all(), request, company)
        data = reports.funnel_report(engagements, period["date_from"], period["date_to"],
                                     attribution=attribution)
        return {"attribution": attribution}, reports.funnel_table(data)


@extend_schema(tags=[TAG], parameters=[
    OpenApiParameter("company", str), OpenApiParameter("period", str, enum=reports.PERIODS),
    OpenApiParameter("date_from", str), OpenApiParameter("date_to", str),
    OpenApiParameter("pipeline", str, description="Pipeline key or id; default the default pipeline."),
], responses={200: dict})
class StageDurationReportView(_CrmReportView):
    """Per stage of one pipeline: entries, time in stage, what came next."""

    report = "stage-duration"
    params = ("pipeline",)

    def build(self, request, company, period):
        pipelines = list(_scoped(Pipeline.objects.all(), request, company)
                         .order_by("-is_default", "sort_order", "name"))
        wanted = request.query_params.get("pipeline") or ""
        if wanted:
            wanted_id = str(uuid.UUID(wanted)) if _is_uuid(wanted) else None
            hits = [p for p in pipelines if p.key == wanted or str(p.pk) == wanted_id]
            if not hits:
                raise ReportParamError("pipeline", "No such pipeline in this tenant.",
                                       allowed=[p.key for p in pipelines])
            pipeline = hits[0]
        else:
            pipeline = reports.default_pipeline(pipelines)
        data = None
        if pipeline is not None:
            data = reports.stage_duration(pipeline, _scoped(Engagement.objects.all(), request, company),
                                          period["date_from"], period["date_to"])
        extra = {"pipeline": {"id": str(pipeline.pk), "key": pipeline.key} if pipeline else None}
        return extra, reports.stage_table(data)


@extend_schema(tags=[TAG], parameters=[
    OpenApiParameter("company", str), OpenApiParameter("period", str, enum=reports.PERIODS),
    OpenApiParameter("date_from", str), OpenApiParameter("date_to", str),
], responses={200: dict})
class FollowUpReportView(_CrmReportView):
    """Tasks due in the period: on time, late, overdue; plus next actions now."""

    queryset = Task.objects.none()  # the service contract: view_task...
    service_perms = ("crm.view_engagement",)  # ...and the open-engagement counts
    report = "follow-ups"

    def build(self, request, company, period):
        data = reports.follow_up_report(
            _scoped(Task.objects.all(), request, company),
            _scoped(Engagement.objects.all(), request, company),
            period["date_from"], period["date_to"],
        )
        return {}, reports.follow_up_table(data)


@extend_schema(tags=[TAG], parameters=[
    OpenApiParameter("company", str), OpenApiParameter("period", str, enum=reports.PERIODS),
    OpenApiParameter("date_from", str), OpenApiParameter("date_to", str),
], responses={200: dict})
class AttributionReportView(_CrmReportView):
    """Engagements won in the period, credited to channels: first, last, linear."""

    queryset = Touchpoint.objects.none()  # the service contract: view_touchpoint...
    service_perms = ("crm.view_engagement",)  # ...and the engagements
    report = "attribution"

    def build(self, request, company, period):
        data = reports.attribution_report(_scoped(Engagement.objects.all(), request, company),
                                          period["date_from"], period["date_to"])
        return {}, reports.attribution_table(data)


SUMMARIES = {
    "funnel": summarize_funnel,
    "stage-duration": summarize_stages,
    "follow-ups": summarize_follow_ups,
    "attribution": summarize_attribution,
}

CATALOG = [
    {"report": "funnel", "path": "funnel/", "permission": "crm.view_engagement",
     "params": {"attribution": list(reports.ATTRIBUTIONS)},
     "columns": reports.FUNNEL_COLUMNS, "definitions": reports.FUNNEL_DEFINITIONS},
    {"report": "stage-duration", "path": "stage-duration/", "permission": "crm.view_engagement",
     "params": {"pipeline": "a pipeline key or id of the tenant; default its default pipeline"},
     "columns": reports.STAGE_COLUMNS, "definitions": reports.STAGE_DEFINITIONS},
    {"report": "follow-ups", "path": "follow-ups/", "permission": "crm.view_task, crm.view_engagement",
     "params": {}, "columns": reports.FOLLOW_UP_COLUMNS,
     "definitions": reports.FOLLOW_UP_DEFINITIONS},
    {"report": "attribution", "path": "attribution/", "permission": "crm.view_touchpoint, crm.view_engagement",
     "params": {}, "columns": reports.ATTRIBUTION_COLUMNS,
     "definitions": reports.ATTRIBUTION_DEFINITIONS},
]


@extend_schema(tags=[TAG], responses={200: dict})
class CrmReportCatalogView(APIView):
    """The reports, their parameters and allowed values, columns and the
    definitions each page shows."""

    permission_classes = [PrincipalPermission]
    queryset = Engagement.objects.none()

    def get(self, request):
        return Response({
            "common_params": {
                "company": "tenant id (uuid); required for a principal that reads every tenant",
                "period": list(reports.PERIODS), "default_period": reports.DEFAULT_PERIOD,
                "date_from": "YYYY-MM-DD, with date_to", "date_to": "YYYY-MM-DD, with date_from",
                "max_days": MAX_SPAN_DAYS,
            },
            "reports": [
                {**entry, "definitions": [{"term": t, "text": d} for t, d in entry["definitions"]]}
                for entry in CATALOG
            ],
            "notes": [reports.NO_FX_NOTE,
                      "Dates are local days in the site time zone, as on the report pages."],
        })
