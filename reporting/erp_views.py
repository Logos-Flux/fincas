import csv

from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.utils import timezone
from django.views.generic import TemplateView

from core.csv_safety import csv_safe, csv_safe_row
from core.html_safety import dumps_script_safe
from core.managers import scope_to_company
from core.views.mixins import ERPViewMixin

from .crm_reports import (
    ATTRIBUTION_DEFINITIONS,
    DEFAULT_PERIOD,
    FOLLOW_UP_DEFINITIONS,
    FUNNEL_DEFINITIONS,
    NO_FX_NOTE,
    STAGE_DEFINITIONS,
    TOTAL_CSV,
    attribution_report,
    default_pipeline,
    fmt_days,
    follow_up_report,
    funnel_report,
    pct,
    period_range,
    stage_duration,
)

REPORT_CHOICES = [
    ("sales_summary", "Sales Summary"),
    ("order_status", "Order Status Pipeline"),
    ("inventory_levels", "Inventory Levels"),
    ("quote_conversion", "Quote Conversion"),
    ("accounts_receivable", "Accounts Receivable"),
]


def _ar_constants():
    from orders.models import Order

    return (
        [Order.PaymentStatus.UNPAID, Order.PaymentStatus.PARTIAL],
        [
            Order.Status.AWAITING_FULFILLMENT,
            Order.Status.AWAITING_SHIPMENT,
            Order.Status.PARTIALLY_SHIPPED,
            Order.Status.SHIPPED,
            Order.Status.COMPLETED,
        ],
    )


def latest_quote_revisions(quotes):
    """Narrow ``quotes`` to the latest revision of each revision chain.

    A chain is keyed by ``original_quote`` (or the quote's own id when it is
    the original). A row is the latest revision when it is not marked
    superseded, has no ``superseded_by``, and no row in the same chain has a
    higher ``revision_number``. Both the status and the links are checked
    because nothing in this codebase writes revisions yet: imported or
    hand-made chains may set one and not the other.

    Convention: ``original_quote`` points at the chain's ROOT (v1), not the
    immediate predecessor. A revision writer that links to the predecessor
    instead would split chains here and double-count them.
    """
    from orders.models import Quote

    chain_key = Coalesce("original_quote_id", "id")
    newer = Quote.objects.annotate(chain=Coalesce("original_quote_id", "id")).filter(
        chain=OuterRef("chain"), revision_number__gt=OuterRef("revision_number")
    )
    return (
        quotes.annotate(chain=chain_key)
        .exclude(status=Quote.Status.SUPERSEDED)
        .filter(superseded_by__isnull=True)
        .exclude(Exists(newer))
    )


def quote_won_q():
    """Won: the quote's engagement is won (CRM Phase 3), or it converted."""
    from crm.models import Engagement

    return Q(engagement__status=Engagement.Status.WON) | Q(converted_to_order__isnull=False)


def quote_lost_q():
    """Lost: the quote's status is Lost or Expired, or its engagement is lost.

    A cancelled quote stays "cancelled" here (excluded from every rate, the
    step-0 rule) even though the step-5 backfill closes its engagement as
    lost with reason ``cancelled``.
    """
    from crm.models import Engagement
    from orders.models import Quote

    return (
        Q(engagement__status=Engagement.Status.LOST) & ~Q(status=Quote.Status.CANCELLED)
    ) | Q(status__in=[Quote.Status.LOST, Quote.Status.EXPIRED])


class ReportListView(ERPViewMixin, TemplateView):
    template_name = "erp/report.html"
    current_section = "reports"
    page_title = "Reports"

    def _get_date_range(self):
        period = self.request.GET.get("period", DEFAULT_PERIOD)
        date_from = self.request.GET.get("date_from")
        date_to = self.request.GET.get("date_to")

        if date_from and date_to:
            from datetime import datetime
            try:
                return datetime.strptime(date_from, "%Y-%m-%d").date(), datetime.strptime(date_to, "%Y-%m-%d").date()
            except ValueError:
                pass

        # The site's calendar day, like the ``__date`` filters the reports
        # use (``timezone.now().date()`` is the UTC day: "today" went blank
        # every evening in America/New_York).
        return period_range(period, timezone.localdate())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_report = self.request.GET.get("report", "sales_summary")
        date_from, date_to = self._get_date_range()

        context["report_choices"] = REPORT_CHOICES
        context["active_report"] = active_report
        context["period"] = self.request.GET.get("period", "month")
        context["date_from"] = date_from
        context["date_to"] = date_to

        report_label = dict(REPORT_CHOICES).get(active_report, "Report")
        context["report_title"] = report_label
        context["page_title"] = report_label

        # Generate report data
        method = getattr(self, f"_report_{active_report}", self._report_sales_summary)
        report = method(date_from, date_to)
        context.update(report)
        context["scope_label"] = self._scope_label()
        return context

    def _scope_label(self):
        """Say whose data the page shows, so every number is attributable.

        Mirrors ``scope_to_company``: a superuser is unscoped, a staff user
        sees their profile's company, and anyone else sees nothing.
        """
        user = self.request.user
        if user.is_superuser:
            return "All companies (superuser view)"
        company = getattr(getattr(user, "userprofile", None), "company", None)
        if company is None:
            return "No company linked to this login, so every report is empty"
        return company.name

    def _orders(self):
        from orders.models import Order

        return Order.objects.for_request(self.request)

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") == "csv":
            return self._export_csv()
        return super().get(request, *args, **kwargs)

    def _export_csv(self):
        context = self.get_context_data()
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="report.csv"'
        writer = csv.writer(response)
        writer.writerow([c["label"] for c in context.get("table_columns", [])])
        for row in context.get("table_data", []):
            writer.writerow(csv_safe_row(row))
        return response

    def _report_sales_summary(self, date_from, date_to):
        from django.db.models import Sum

        orders = self._orders().filter(created_at__date__gte=date_from, created_at__date__lte=date_to)
        total_revenue = orders.aggregate(total=Sum("grand_total"))["total"] or 0
        total_orders = orders.count()

        # Top customers
        from django.db.models import Count, Sum
        top_customers = orders.values("customer__first_name", "customer__last_name").annotate(
            total=Sum("grand_total"), count=Count("id")
        ).order_by("-total")[:10]

        table_data = [
            [f"{c['customer__first_name']} {c['customer__last_name']}", c["count"], f"${c['total']:.2f}"]
            for c in top_customers
        ]

        return {
            "summary_cards": [
                {"label": "Total Revenue", "value": f"${total_revenue:,.2f}", "color": "success", "icon": "ti ti-coins"},
                {"label": "Total Orders", "value": total_orders, "color": "info", "icon": "ti ti-shopping-cart"},
                {"label": "Avg Order Value", "value": f"${(total_revenue / total_orders):,.2f}" if total_orders else "$0", "color": "primary", "icon": "ti ti-chart-bar"},
            ],
            "chart_title": "Revenue by Day",
            "chart_data": dumps_script_safe({"type": "bar", "labels": [], "datasets": []}),
            "table_columns": [
                {"label": "Customer"}, {"label": "Orders"}, {"label": "Revenue", "align": "end"},
            ],
            "table_data": table_data,
            "definitions": [
                ("Total Revenue", "Sum of grand total over every order created in the period, in any status (canceled and declined included), in its own currency with no FX conversion."),
                ("Total Orders", "Orders created in the period, any status."),
                ("Avg Order Value", "Total Revenue / Total Orders."),
                ("Table", "Top 10 customers by revenue in the period."),
            ],
        }

    def _report_order_status(self, date_from, date_to):
        from django.db.models import Count

        from orders.models import Order
        status_counts = self._orders().filter(
            created_at__date__gte=date_from, created_at__date__lte=date_to
        ).values("status").annotate(count=Count("id")).order_by("status")

        status_labels = dict(Order.Status.choices)
        labels = [status_labels.get(s["status"], s["status"]) for s in status_counts]
        values = [s["count"] for s in status_counts]
        table_data = [[label, value] for label, value in zip(labels, values)]

        return {
            "summary_cards": [
                {"label": "Total", "value": sum(values), "color": "primary", "icon": "ti ti-shopping-cart"},
            ],
            "chart_title": "Order Status Distribution",
            "chart_data": dumps_script_safe({
                "type": "bar",
                "labels": labels,
                "datasets": [{"label": "Orders", "data": values, "backgroundColor": "rgba(94,114,228,0.5)"}],
            }),
            "table_columns": [{"label": "Status"}, {"label": "Count"}],
            "table_data": table_data,
            "definitions": [
                ("Total", "Orders created in the period, counted once each by their current status."),
            ],
        }

    def _report_inventory_levels(self, date_from, date_to):
        from django.db.models import F

        from inventory.models import WarehouseStock
        # WarehouseStock has no company of its own; it inherits one through
        # its warehouse (inventory/models.py), so scope through that FK.
        stocks = scope_to_company(
            WarehouseStock.objects.select_related("warehouse", "variant"),
            self.request,
            field="warehouse__company",
        ).order_by("warehouse__name")

        low_stock_count = stocks.filter(reorder_level__gt=0, quantity__lte=F("reorder_level")).count()
        table_data = [
            [s.warehouse.name, s.variant.sku, s.quantity, s.reserved_qty, s.available_qty, s.reorder_level or "—"]
            for s in stocks[:50]
        ]

        return {
            "summary_cards": [
                {"label": "Total SKUs Tracked", "value": stocks.count(), "color": "info", "icon": "ti ti-package"},
                {"label": "Low Stock Alerts", "value": low_stock_count, "color": "danger", "icon": "fas fa-exclamation-triangle"},
            ],
            "chart_title": "Stock by Warehouse",
            "chart_data": dumps_script_safe({"type": "bar", "labels": [], "datasets": []}),
            "table_columns": [
                {"label": "Warehouse"}, {"label": "SKU"}, {"label": "On Hand"},
                {"label": "Reserved"}, {"label": "Available"}, {"label": "Reorder Level"},
            ],
            "table_data": table_data,
            "definitions": [
                ("Total SKUs Tracked", "Warehouse stock rows (one per SKU per warehouse), current snapshot; the period does not apply."),
                ("Low Stock Alerts", "Rows with a reorder level above 0 and on-hand quantity at or below it."),
                ("Table", "First 50 rows by warehouse name."),
            ],
        }

    def _report_quote_conversion(self, date_from, date_to):
        from django.db.models import Count

        from orders.models import Quote

        # Period = when the chain started (its original quote's creation),
        # so a deal revised after the period still counts in the period it
        # was opened, judged by its latest revision.
        heads = (
            latest_quote_revisions(Quote.objects.for_request(self.request))
            .annotate(chain_started=Coalesce("original_quote__created_at", "created_at"))
            .filter(chain_started__date__gte=date_from, chain_started__date__lte=date_to)
        )
        won_q = quote_won_q()
        lost_q = quote_lost_q() & ~won_q
        cancelled_q = Q(status=Quote.Status.CANCELLED) & ~won_q & ~lost_q

        total = heads.count()
        won = heads.filter(won_q).count()
        lost = heads.filter(lost_q).count()
        cancelled = heads.filter(cancelled_q).count()
        open_count = total - won - lost - cancelled
        decided = won + lost
        win_rate = f"{won / decided * 100:.0f}%" if decided else "—"

        # The stage lives on the engagement (CRM Phase 3, spec 1.6). Step 9
        # replaces this report with the engagement reports.
        stage_counts = (
            heads.values("engagement__stage__sort_order", "engagement__stage__name")
            .annotate(count=Count("id"))
            .order_by("engagement__stage__sort_order", "engagement__stage__name")
        )
        table_data = [
            [s["engagement__stage__name"] or "No stage", s["count"]] for s in stage_counts
        ]

        return {
            "summary_cards": [
                {"label": "Quotes", "value": total, "color": "info", "icon": "ti ti-file-text"},
                {"label": "Won", "value": won, "color": "success", "icon": "fas fa-trophy"},
                {"label": "Lost", "value": lost, "color": "danger", "icon": "ti ti-x"},
                {"label": "Open", "value": open_count, "color": "warning", "icon": "ti ti-clock"},
                {"label": "Win Rate", "value": win_rate, "color": "primary", "icon": "ti ti-chart-bar"},
            ],
            "chart_title": "Quotes by Stage",
            "chart_data": dumps_script_safe({
                "type": "bar",
                "labels": [row[0] for row in table_data],
                "datasets": [{"label": "Quotes", "data": [row[1] for row in table_data], "backgroundColor": "rgba(94,114,228,0.5)"}],
            }),
            "table_columns": [{"label": "Stage"}, {"label": "Count"}],
            "table_data": table_data,
            "definitions": [
                ("Quotes", "One per revision chain whose original quote was created in the period, judged by the chain's latest revision (see Latest revision)."),
                ("Latest revision", "A chain is a quote plus every revision whose original quote is it. The latest revision is the one with the highest revision number that is not marked superseded and has no superseding quote. A chain whose last row is marked superseded with no successor (bad data) is not counted."),
                ("Won", "The quote's engagement is won, or the quote converted to an order."),
                ("Lost", "Not won, and the engagement is lost or the quote's status is Lost or Expired."),
                ("Open", f"Neither won, lost nor canceled ({cancelled} canceled in the period, excluded from every rate)."),
                ("Win Rate", "Won / (Won + Lost). Open quotes are not in the denominator; shows — when nothing has been decided."),
                ("Table", "Latest revisions by their engagement's stage. Closing an engagement keeps its stage, so a lost deal shows the stage it was lost at."),
            ],
        }

    def _report_accounts_receivable(self, date_from, date_to):
        from django.db.models import Sum

        payment_statuses, order_statuses = _ar_constants()
        unpaid = self._orders().filter(
            payment_status__in=payment_statuses,
            status__in=order_statuses,
        ).select_related("customer").order_by("-created_at")

        total_outstanding = unpaid.aggregate(total=Sum("balance_due"))["total"] or 0
        unpaid_count = unpaid.count()
        unpaid = unpaid[:50]
        table_data = [
            [o.order_number, str(o.customer), f"${o.grand_total or 0:.2f}", f"${o.balance_due or 0:.2f}", o.get_payment_status_display()]
            for o in unpaid
        ]

        return {
            "summary_cards": [
                {"label": "Outstanding Balance", "value": f"${total_outstanding:,.2f}", "color": "danger", "icon": "ti ti-coins"},
                {"label": "Unpaid Orders", "value": unpaid_count, "color": "warning", "icon": "ti ti-credit-card"},
            ],
            "chart_title": "Aging",
            "chart_data": dumps_script_safe({"type": "bar", "labels": [], "datasets": []}),
            "table_columns": [
                {"label": "Order #"}, {"label": "Customer"}, {"label": "Total"},
                {"label": "Balance Due"}, {"label": "Status"},
            ],
            "table_data": table_data,
            "definitions": [
                ("Unpaid Orders", "Orders, any creation date, with payment Unpaid or Partial and status Awaiting Fulfillment, Awaiting Shipment, Partially Shipped, Shipped or Completed. The period does not apply."),
                ("Outstanding Balance", "Sum of balance due over those orders, in their own currency with no FX conversion."),
                ("Table", "The 50 most recent of those orders."),
            ],
        }


# --- CRM Phase 3 reports (spec 4.2, step 9) ----------------------------------------


def heading(text="", *, key="", plural=True, before="", after="", case="as_is"):
    """A display heading: plain ``text``, or a label-layer noun (``key``) with
    text around it. The template renders it with ``{% label %}``; CSV never
    sees it (canonical ``csv`` names only, spec 3.4)."""
    return {"text": text, "label_key": key, "plural": plural, "before": before,
            "after": after, "case": case}


def blank(display):
    """CSV form of a display value: the "—" placeholder becomes empty."""
    return "" if display == "—" else display


def pct_cell(part, whole):
    text = pct(part, whole)
    return cell(text, blank(text))


def column(csv_name, title, *, align="", csv_only=False):
    if isinstance(title, str):
        title = heading(title)
    return {"csv": csv_name, "heading": title, "align": align, "csv_only": csv_only}


def cell(display, csv_value=None, *, csv_only=False):
    """``display`` on the page, ``csv_value`` (default: the same) in the CSV."""
    return {"display": display, "csv": display if csv_value is None else csv_value,
            "csv_only": csv_only}


#: (report key, URL name, sidebar section, title). Titles name nouns through
#: the label layer.
CRM_REPORTS = [
    ("crm_funnel", "erp-report-crm-funnel", "reports_crm_funnel",
     heading(key="crm.engagement", plural=False, after=" funnel by source")),
    ("crm_stages", "erp-report-crm-stages", "reports_crm_stages",
     heading(key="crm.stage", plural=False, after=" duration and conversion")),
    ("crm_follow_ups", "erp-report-crm-follow-ups", "reports_crm_follow_ups",
     heading("Follow-up completion")),
    ("crm_attribution", "erp-report-crm-attribution", "reports_crm_attribution",
     heading("Multi-touch attribution")),
]


class CrmReportView(ReportListView):
    """One CRM report per URL (sidebar entries link to each), sharing the
    period controls, scoping and definitions footnote of ``ReportListView``.

    Every queryset is ``.for_request()``-scoped. The CSV writes the
    columns' canonical ``csv`` names and values, never labels.
    """

    template_name = "erp/crm_report.html"
    report_key = ""

    def get_context_data(self, **kwargs):
        # Skip ReportListView's ?report= dispatch; keep the mixin's context.
        context = super(ReportListView, self).get_context_data(**kwargs)
        date_from, date_to = self._get_date_range()
        title = next(t for key, _, _, t in CRM_REPORTS if key == self.report_key)
        from core.labels import labels_for_request, render

        def text(h):  # the breadcrumb / <title> text of a heading
            if not h["label_key"]:
                return h["text"]
            resolved = labels_for_request(self.request)[h["label_key"]]
            return h["before"] + render(resolved, plural=h["plural"], case=h["case"]) + h["after"]

        context.update(
            page_title=text(title),
            active_report=self.report_key,
            crm_reports=[
                {"key": key, "url_name": url_name, "title": t}
                for key, url_name, _, t in CRM_REPORTS
            ],
            report_heading=title,
            period=self.request.GET.get("period", "month"),
            date_from=date_from,
            date_to=date_to,
            scope_label=self._scope_label(),
            report_notes=[NO_FX_NOTE],
            total_row=None,
        )
        context.update(getattr(self, f"_build_{self.report_key}")(date_from, date_to))
        context["visible_column_count"] = sum(
            1 for c in context["table_columns"] if not c["csv_only"]
        )
        return context

    def _export_csv(self):
        context = self.get_context_data()
        name = f"{self.report_key}_{context['date_from']:%Y-%m-%d}_{context['date_to']:%Y-%m-%d}"
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{name}.csv"'
        writer = csv.writer(response)
        writer.writerow([c["csv"] for c in context["table_columns"]])
        rows = list(context["table_rows"])
        if context.get("total_row"):
            rows.append(context["total_row"])
        for row in rows:
            writer.writerow([csv_safe(c["csv"]) for c in row])
        return response

    # ── funnel by source ────────────────────────────────────

    def _build_crm_funnel(self, date_from, date_to):
        from crm.models import Engagement

        data = funnel_report(Engagement.objects.for_request(self.request), date_from, date_to,
                             attribution=self.request.GET.get("by", "engagement"))

        def cells(row, *, total=False):
            value = row.value
            return [
                cell(row.label, TOTAL_CSV if total else row.key),
                cell(row.created), cell(row.reached), cell(row.won), cell(row.lost),
                cell(row.not_counted), cell(row.open), cell(row.win_rate, blank(row.win_rate)),
                cell(value.display(), value.csv()),
                cell(f"{value.known} of {value.total}" if value.total else "—", value.known),
            ]

        total = data["total"]
        known, won_total = total.value.known, total.value.total
        return {
            "attribution": data["attribution"],
            "attribution_choices": [("engagement", heading(key="crm.engagement", plural=False,
                                                           after=" source")),
                                    ("first_touch", heading("First touch"))],
            "summary_cards": [
                {"heading": heading(key="crm.engagement", after=" created"),
                 "value": total.created, "icon": "ti ti-chart-bar", "color": "info"},
                {"heading": heading("Won"), "value": total.won, "icon": "ti ti-thumb-up",
                 "color": "success"},
                {"heading": heading("Win rate"), "value": total.win_rate,
                 "icon": "ti ti-chart-bar", "color": "primary"},
                {"heading": heading("Won value"), "value": total.value.display(),
                 "icon": "ti ti-coins", "color": "success"},
            ],
            "completeness": {
                "pct": pct(known, won_total), "known": known, "total": won_total,
            } if won_total else None,
            "table_columns": [
                column("source", "Source"),
                column("created", "Created", align="end"),
                column("reached_qualification", "Reached qualification", align="end"),
                column("won", "Won", align="end"),
                column("lost", "Lost", align="end"),
                column("not_counted", "Disqualified / duplicate", align="end"),
                column("open", "Open", align="end"),
                column("win_rate", "Win rate", align="end"),
                column("won_value", "Won value", align="end"),
                column("won_amount_known", "Amount known", align="end"),
            ],
            "table_rows": [cells(row) for row in data["rows"]],
            "total_row": cells(total, total=True) if data["rows"] else None,
            "definitions": FUNNEL_DEFINITIONS,
        }

    # ── stage duration and conversion ───────────────────────

    def _pipelines(self):
        from crm.models import Pipeline

        return Pipeline.objects.for_request(self.request).select_related("company").order_by(
            "company__name", "-is_default", "sort_order", "name"
        )

    def _selected_pipeline(self, pipelines):
        return default_pipeline(pipelines, self.request.GET.get("pipeline", ""))

    def _build_crm_stages(self, date_from, date_to):
        from crm.models import Engagement

        pipelines = list(self._pipelines())
        pipeline = self._selected_pipeline(pipelines)
        multi_company = len({p.company_id for p in pipelines}) > 1
        choices = [
            (str(p.pk), f"{p.name} ({p.company.name})" if multi_company else p.name)
            for p in pipelines
        ]
        rows, excluded, reconstructed = [], 0, 0
        if pipeline is not None:
            data = stage_duration(pipeline, Engagement.objects.for_request(self.request),
                                  date_from, date_to)
            excluded, reconstructed = data["excluded"], data["reconstructed_visits"]
            for row in data["rows"]:
                median, p75 = row.median_p75
                entered = len(row.entered)
                still_median = row.still_in_median
                rows.append([
                    cell(row.stage.name, row.stage.key),
                    cell(row.stage.name, csv_only=True),
                    cell(entered),
                    cell(fmt_days(median), "" if median is None else round(median, 1)),
                    cell(fmt_days(p75), "" if p75 is None else round(p75, 1)),
                    cell(len(row.still_in)),
                    cell(fmt_days(still_median),
                         "" if still_median is None else round(still_median, 1)),
                    cell(len(row.advanced)),
                    pct_cell(len(row.advanced), entered),
                    cell(len(row.lost_here)),
                    pct_cell(len(row.lost_here), entered),
                ])
        total_entered = sum(r[2]["csv"] for r in rows)
        return {
            "pipeline_choices": choices,
            "selected_pipeline": str(pipeline.pk) if pipeline else "",
            "summary_cards": [
                {"heading": heading(key="crm.stage", plural=False, after=" entries"), "value": total_entered,
                 "icon": "ti ti-chart-bar", "color": "info"},
                {"heading": heading("Excluded, no observed history"), "value": excluded,
                 "icon": "ti ti-clock", "color": "secondary"},
            ],
            "no_history_excluded": excluded,
            "reconstructed_visits": reconstructed,
            "report_notes": [],
            "table_columns": [
                column("stage", heading(key="crm.stage", plural=False)),
                column("stage_name", "Stage name", csv_only=True),
                column("entered", "Entered", align="end"),
                column("median_days", "Median days", align="end"),
                column("p75_days", "P75 days", align="end"),
                column("still_in_stage", "Still in stage", align="end"),
                column("still_in_stage_median_days", "Still in stage, median days so far", align="end"),
                column("advanced", "Advanced or won", align="end"),
                column("advanced_pct", "Advanced %", align="end"),
                column("lost_here", "Lost here", align="end"),
                column("lost_here_pct", "Lost here %", align="end"),
            ],
            "table_rows": rows,
            "definitions": STAGE_DEFINITIONS,
        }

    # ── follow-up completion ────────────────────────────────

    def _build_crm_follow_ups(self, date_from, date_to):
        from crm.models import Engagement, Task

        data = follow_up_report(Task.objects.for_request(self.request),
                                Engagement.objects.for_request(self.request), date_from, date_to)

        def cells(row, *, total=False):
            return [
                cell(row.label, TOTAL_CSV if total else (row.label if row.key else "")),
                cell(row.key, TOTAL_CSV if total else row.key, csv_only=True),
                cell(row.due), cell(row.on_time), cell(row.late), cell(row.overdue),
                cell(row.not_yet_due), cell(row.cancelled), cell(row.rescheduled),
                cell(row.on_time_rate, blank(row.on_time_rate)),
            ]

        total = data["total"]
        open_count, without = data["open_engagements"], data["without_next_action"]
        return {
            "summary_cards": [
                {"heading": heading(key="crm.task", after=" due"), "value": total.due,
                 "icon": "ti ti-check", "color": "info"},
                {"heading": heading("Completed on time"), "value": total.on_time_rate,
                 "icon": "ti ti-thumb-up", "color": "success"},
                {"heading": heading(key="crm.engagement", before="Open ",
                                    after=" without a next action", case="lower"),
                 "value": f"{without} ({pct(without, open_count)})",
                 "detail": f"of {open_count} open", "icon": "ti ti-clock", "color": "danger"},
                {"heading": heading("Overdue next actions"),
                 "value": data["overdue_next_actions"], "icon": "ti ti-clock",
                 "color": "warning"},
            ],
            "report_notes": [],
            "table_columns": [
                column("assignee", "Assignee"),
                column("assignee_id", "Assignee id", csv_only=True),
                column("due", heading(key="crm.task", after=" due"), align="end"),
                column("completed_on_time", "Completed on time", align="end"),
                column("completed_late", "Completed late", align="end"),
                column("open_overdue", "Open, overdue", align="end"),
                column("open_not_yet_due", "Open, not yet due", align="end"),
                column("cancelled", "Canceled", align="end"),
                column("times_rescheduled", "Times rescheduled", align="end"),
                column("on_time_rate", "On-time rate", align="end"),
            ],
            "table_rows": [cells(row) for row in data["rows"]],
            "total_row": cells(total, total=True) if data["rows"] else None,
            "definitions": FOLLOW_UP_DEFINITIONS,
        }

    # ── multi-touch attribution (Phase 6 step 6) ────────────

    def _build_crm_attribution(self, date_from, date_to):
        from crm.models import Engagement

        data = attribution_report(Engagement.objects.for_request(self.request), date_from, date_to)

        def cells(row, *, total=False):
            value = row.value
            return [
                cell(row.label, TOTAL_CSV if total else row.key),
                cell(row.first), cell(row.last), cell(row.linear_text),
                cell(value.display(), value.csv()),
                cell(f"{value.known} of {value.total}" if value.total else "—", value.known),
            ]

        total = data["total"]
        return {
            "summary_cards": [
                {"heading": heading(key="crm.engagement", before="Won ", case="lower"),
                 "value": total.first + data["no_touch"], "icon": "ti ti-trophy", "color": "success"},
                {"heading": heading("Without a touchpoint"), "value": data["no_touch"],
                 "icon": "ti ti-question-mark", "color": "warning"},
            ],
            "table_columns": [
                column("channel", "Channel"),
                column("first_touch", "First touch", align="end"),
                column("last_touch", "Last touch", align="end"),
                column("linear", "Linear", align="end"),
                column("first_touch_won_value", "Won value (first touch)", align="end"),
                column("won_amount_known", "Amount known", align="end"),
            ],
            "table_rows": [cells(row) for row in data["rows"]],
            "total_row": cells(total, total=True) if data["rows"] else None,
            "definitions": ATTRIBUTION_DEFINITIONS,
        }
