"""CRM Phase 3 reports (docs/crm/phase3-engagement.md 4.2, step 9).

Three pure computations over querysets the caller has already scoped with
``.for_request()``: the funnel by source, stage duration and conversion,
and follow-up completion. Each returns plain data; ``reporting.erp_views``
turns it into the page and the CSV, and ``reporting.api`` into JSON (CRM
Phase 6 step 7). The cohorts (which rows a period selects), the pipeline
choice and the definitions live here too, so the page, the CSV and the API
can never disagree about what a report counts.

Rules shared by all three:

- **Money is grouped by currency, never converted** (no FX), and a null
  ``expected_value`` is never summed as zero: a value whose cell holds any
  engagement without an amount renders as a lower bound, ``"≥ $X"``.
- **Durations come from ``EngagementStageChange`` only** (E12), and only
  from rows people wrote: ``backfill`` and ``import`` rows carry a
  reconstructed time and stage, not an observed one. An engagement with no
  other history is excluded and counted in a footnote.
- Dates are local calendar days in the site time zone, like the other
  reports on ``/erp/reports/``.

The ``*_table`` functions at the bottom are the machine-facing form of each
report: the CSV's canonical column names with typed values (integers,
decimal strings, ``None`` for unknown), never a display string such as
``"—"`` or ``"≥ $X"``, and never a tenant label (the label layer is staff-UI only).
"""

from __future__ import annotations

import datetime
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from django.db.models import Q
from django.utils import timezone

from crm.models import (
    OPEN_TASK_STATUSES,
    Engagement,
    EngagementStageChange,
    Stage,
    Task,
    TaskEvent,
)
from crm.registries import ENGAGEMENT_SOURCES, LOSS_REASONS

#: The funnel's "qualified" bar: this stage key, or any later stage.
QUALIFICATION_KEY = "qualification"
#: History written by a backfill or an import: its time and stage are
#: reconstructed, so no duration is measured from it.
RECONSTRUCTED_SOURCES = EngagementStageChange.RECONSTRUCTED_SOURCES
ATTRIBUTIONS = ("engagement", "first_touch")
UNKNOWN_SOURCE_CSV = ""
TOTAL_CSV = "(total)"

_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}


def money(amount: Decimal, currency: str) -> str:
    """``$1,200.00`` for USD/EUR/GBP, else ``1,200.00 CAD`` (no FX, ever)."""
    text = f"{amount:,.2f}"
    symbol = _SYMBOLS.get(currency)
    return f"{symbol}{text}" if symbol else f"{text} {currency}"


def pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.0f}%" if whole else "—"


def counts_as_loss(reason: str) -> bool:
    """A loss reason's ``counts_as_loss`` flag; an unregistered key counts."""
    entry = LOSS_REASONS.get(reason)
    return entry.counts_as_loss if entry is not None else True


def source_label(key: str) -> str:
    if not key:
        return "Unknown"
    entry = ENGAGEMENT_SOURCES.get(key)
    return entry.label if entry is not None else key


def local_date(instant):
    return timezone.localtime(instant).date()


# --- funnel by source ------------------------------------------------------------


@dataclass
class ValueCell:
    """Won value of one funnel cell, per currency: sum of known amounts and
    how many of the cell's won engagements had one."""

    by_currency: dict = field(default_factory=lambda: defaultdict(lambda: [Decimal("0"), 0, 0]))

    def add(self, currency: str, amount):
        entry = self.by_currency[currency]
        entry[2] += 1
        if amount is not None:
            entry[0] += amount
            entry[1] += 1

    @property
    def known(self) -> int:
        return sum(e[1] for e in self.by_currency.values())

    @property
    def total(self) -> int:
        return sum(e[2] for e in self.by_currency.values())

    @property
    def is_lower_bound(self) -> bool:
        return self.known < self.total

    def parts(self) -> list[tuple[str, Decimal, bool]]:
        """``(currency, sum of known amounts, lower bound?)``, by currency."""
        return [
            (currency, entry[0], entry[1] < entry[2])
            for currency, entry in sorted(self.by_currency.items())
        ]

    def display(self) -> str:
        if not self.total:
            return "—"
        return "; ".join(
            f"{'≥ ' if lower else ''}{money(amount, currency)}"
            for currency, amount, lower in self.parts()
        )

    def csv(self) -> str:
        if not self.total:
            return ""
        return "; ".join(
            f"{'≥' if lower else ''}{amount:.2f} {currency}"
            for currency, amount, lower in self.parts()
        )


@dataclass
class FunnelRow:
    key: str
    label: str
    created: int = 0
    reached: int = 0
    won: int = 0
    lost: int = 0
    not_counted: int = 0
    open: int = 0
    value: ValueCell = field(default_factory=ValueCell)

    @property
    def win_rate(self) -> str:
        return pct(self.won, self.won + self.lost)


def _qualification_orders(pipeline_ids) -> dict:
    """``{pipeline_id: sort_order}`` of each pipeline's qualification stage."""
    return dict(
        Stage.objects.filter(pipeline_id__in=pipeline_ids, key=QUALIFICATION_KEY)
        .values_list("pipeline_id", "sort_order")
    )


def funnel_by_source(engagements, *, attribution: str = "engagement") -> dict:
    """Engagements (the cohort, already scoped and dated) by source.

    ``attribution`` is ``engagement`` (``Engagement.source``) or
    ``first_touch`` (the first touchpoint's ``channel``). Blank shows as
    Unknown and is never dropped.
    """
    if attribution not in ATTRIBUTIONS:
        attribution = "engagement"
    rows = list(engagements.values(
        "pk", "pipeline_id", "stage_id", "stage__pipeline_id", "stage__sort_order", "status",
        "loss_reason", "expected_value", "currency", "source", "first_touch__channel",
    ))
    pipeline_ids = {r["pipeline_id"] for r in rows}
    history = EngagementStageChange.objects.filter(
        engagement__in=engagements.values("pk"), to_stage__isnull=False,
    ).values_list("engagement_id", "to_stage__pipeline_id", "to_stage__sort_order")
    history = list(history)
    pipeline_ids |= {pipeline_id for _, pipeline_id, _ in history}
    qualification = _qualification_orders(pipeline_ids)

    def past_bar(pipeline_id, sort_order):
        bar = qualification.get(pipeline_id)
        return bar is not None and sort_order is not None and sort_order >= bar

    reached_by_history = {
        eng_id for eng_id, pipeline_id, sort_order in history if past_bar(pipeline_id, sort_order)
    }

    by_key: dict[str, FunnelRow] = {}
    total = FunnelRow(TOTAL_CSV, "All sources")
    for r in rows:
        key = r["source"] if attribution == "engagement" else (r["first_touch__channel"] or "")
        row = by_key.get(key)
        if row is None:
            row = by_key[key] = FunnelRow(key, source_label(key))
        reached = (
            r["pk"] in reached_by_history
            or past_bar(r["stage__pipeline_id"], r["stage__sort_order"])
            or r["status"] == Engagement.Status.WON
        )
        for target in (row, total):
            target.created += 1
            target.reached += reached
            if r["status"] == Engagement.Status.WON:
                target.won += 1
                target.value.add(r["currency"], r["expected_value"])
            elif r["status"] == Engagement.Status.LOST:
                if counts_as_loss(r["loss_reason"]):
                    target.lost += 1
                else:
                    target.not_counted += 1
            else:
                target.open += 1
    ordered = sorted(by_key.values(), key=lambda row: (-row.created, row.label.lower(), row.key))
    return {"rows": ordered, "total": total, "attribution": attribution}


# --- stage duration and conversion ------------------------------------------------


def _quantiles(days: list[float]) -> tuple[float | None, float | None]:
    """Median and 75th percentile (linear interpolation, inclusive)."""
    if not days:
        return None, None
    if len(days) == 1:
        return days[0], days[0]
    return statistics.median(days), statistics.quantiles(days, n=4, method="inclusive")[2]


def fmt_days(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


@dataclass
class StageRow:
    stage: Stage
    entered: set = field(default_factory=set)
    durations: list = field(default_factory=list)
    still_in: set = field(default_factory=set)
    still_in_days: list = field(default_factory=list)
    advanced: set = field(default_factory=set)
    lost_here: set = field(default_factory=set)

    @property
    def median_p75(self):
        return _quantiles(sorted(self.durations))

    @property
    def still_in_median(self) -> float | None:
        return statistics.median(self.still_in_days) if self.still_in_days else None


def _is_entry(row, stage_ids) -> bool:
    """The row puts an open engagement into a stage of this pipeline (a new
    stint): creation, a move, a pipeline change, or a reopen."""
    return (
        row["to_stage_id"] in stage_ids
        and row["to_status"] == Engagement.Status.OPEN
        and not (row["from_stage_id"] == row["to_stage_id"]
                 and row["from_status"] == Engagement.Status.OPEN)
    )


def stage_duration(pipeline, engagements, date_from, date_to, *, now=None) -> dict:
    """Per stage of ``pipeline``: entries in the period, time spent, and what
    happened next, from ``EngagementStageChange``.

    ``engagements`` is the scoped engagement queryset (not dated: a stint is
    dated by its entry row).
    """
    now = now or timezone.now()
    stages = list(pipeline.stages.order_by("sort_order", "name"))
    order = {s.pk: s.sort_order for s in stages}
    rows_by_stage = {s.pk: StageRow(s) for s in stages}

    # Only engagements with something in or after the period: a visit is
    # dated by its entry row and only looks forward from it.
    start = datetime.datetime.combine(date_from, datetime.time.min,
                                      tzinfo=timezone.get_current_timezone())
    in_pipeline = Q(pipeline=pipeline) | Q(
        pk__in=EngagementStageChange.objects.filter(to_pipeline=pipeline).values("engagement_id")
    )
    recent = Q(created_at__gte=start) | Q(
        pk__in=EngagementStageChange.objects.filter(changed_at__gte=start).values("engagement_id")
    )
    involved_qs = engagements.filter(in_pipeline & recent)
    involved = list(involved_qs.values_list("pk", "pipeline_id", "created_at"))
    history = defaultdict(list)
    for row in (
        EngagementStageChange.objects.filter(engagement__in=involved_qs.values("pk"))
        .order_by("engagement_id", "changed_at", "created_at")
        .values("engagement_id", "to_stage_id", "to_status", "from_stage_id", "from_status",
                "changed_at", "source")
    ):
        history[row["engagement_id"]].append(row)

    def in_period(instant):
        return date_from <= local_date(instant) <= date_to

    excluded = set()
    reconstructed_visits = 0
    for pk, pipeline_id, created_at in involved:
        rows = history.get(pk, [])
        if all(r["source"] in RECONSTRUCTED_SOURCES for r in rows):
            # No observed history: its visits would be invented. Counted when
            # it would otherwise have been in this period's cohort.
            if any(_is_entry(r, order) and in_period(r["changed_at"]) for r in rows) or (
                not rows and pipeline_id == pipeline.pk and in_period(created_at)
            ):
                excluded.add(pk)
            continue
        for i, entry in enumerate(rows):
            if not _is_entry(entry, order) or not in_period(entry["changed_at"]):
                continue
            if entry["source"] in RECONSTRUCTED_SOURCES:
                # Neither the time nor the stage was observed: not a visit.
                reconstructed_visits += 1
                continue
            later = rows[i + 1:]
            stage_row = rows_by_stage[entry["to_stage_id"]]
            stage_row.entered.add(pk)
            if later:
                exit_row = later[0]
                if exit_row["source"] in RECONSTRUCTED_SOURCES:
                    # Observed entry, reconstructed exit: counted, not timed.
                    reconstructed_visits += 1
                else:
                    days = (exit_row["changed_at"] - entry["changed_at"]).total_seconds() / 86400
                    stage_row.durations.append(days)
                if (exit_row["to_status"] == Engagement.Status.LOST
                        and exit_row["from_stage_id"] == entry["to_stage_id"]):
                    stage_row.lost_here.add(pk)
            else:
                stage_row.still_in.add(pk)
                stage_row.still_in_days.append(
                    (now - entry["changed_at"]).total_seconds() / 86400
                )
            here = order[entry["to_stage_id"]]
            if any(
                r["to_status"] == Engagement.Status.WON
                or (r["to_stage_id"] in order and order[r["to_stage_id"]] > here)
                for r in later
            ):
                stage_row.advanced.add(pk)

    stage_rows = [
        rows_by_stage[s.pk] for s in stages if s.is_active or rows_by_stage[s.pk].entered
    ]
    return {"rows": stage_rows, "excluded": len(excluded),
            "reconstructed_visits": reconstructed_visits}


# --- follow-up completion ---------------------------------------------------------


@dataclass
class FollowUpRow:
    key: str
    label: str
    due: int = 0
    on_time: int = 0
    late: int = 0
    overdue: int = 0
    not_yet_due: int = 0
    cancelled: int = 0
    rescheduled: int = 0

    @property
    def on_time_rate(self) -> str:
        return pct(self.on_time, self.due)


def _assignee_label(row) -> str:
    if row["assignee_id"] is None:
        return "Unassigned"
    return (row["assignee__display_name"] or row["assignee__user__username"]
            or str(row["assignee_id"]))


def follow_up_completion(tasks, open_engagements, *, now=None) -> dict:
    """``tasks``: the cohort (scoped, due in the period). ``open_engagements``:
    the scoped open engagements, for the next-action cards (a snapshot)."""
    now = now or timezone.now()
    rows = list(tasks.values(
        "pk", "assignee_id", "assignee__display_name", "assignee__user__username",
        "status", "due_at", "completed_at",
    ))
    reschedules = defaultdict(int)
    for task_id in TaskEvent.objects.filter(
        task__in=tasks.values("pk"), event=TaskEvent.Event.RESCHEDULED,
    ).values_list("task_id", flat=True):
        reschedules[task_id] += 1

    by_assignee: dict = {}
    total = FollowUpRow(TOTAL_CSV, "All assignees")
    for r in rows:
        key = str(r["assignee_id"]) if r["assignee_id"] else ""
        row = by_assignee.get(key)
        if row is None:
            row = by_assignee[key] = FollowUpRow(key, _assignee_label(r))
        for target in (row, total):
            target.due += 1
            target.rescheduled += reschedules.get(r["pk"], 0)
            if r["status"] == Task.Status.DONE:
                if r["completed_at"] <= r["due_at"]:
                    target.on_time += 1
                else:
                    target.late += 1
            elif r["status"] == Task.Status.CANCELLED:
                target.cancelled += 1
            elif r["status"] in OPEN_TASK_STATUSES:
                if r["due_at"] < now:
                    target.overdue += 1
                else:
                    target.not_yet_due += 1
    ordered = sorted(
        by_assignee.values(), key=lambda row: (row.key == "", row.label.lower(), row.key)
    )
    open_count = open_engagements.count()
    without_next = open_engagements.filter(next_task__isnull=True).count()
    overdue_next = open_engagements.filter(next_action_due_at__lt=now).count()
    return {
        "rows": ordered,
        "total": total,
        "open_engagements": open_count,
        "without_next_action": without_next,
        "overdue_next_actions": overdue_next,
    }


# --- periods and cohorts (shared by the page, the CSV and the API) -------------------

#: The period picker's choices, as on ``/erp/reports/``.
PERIODS = ("today", "week", "month", "quarter", "year")
DEFAULT_PERIOD = "month"


def period_range(period: str, today: datetime.date) -> tuple[datetime.date, datetime.date]:
    """``(date_from, date_to)`` of a picker period ending ``today``.

    ``today`` is the site's calendar day (``timezone.localdate()``), never the
    UTC day: "today" used to go blank every evening in America/New_York. An
    unknown period is the month, as on the page.
    """
    if period == "today":
        return today, today
    if period == "week":
        return today - datetime.timedelta(days=today.weekday()), today
    if period == "quarter":
        return today.replace(month=((today.month - 1) // 3) * 3 + 1, day=1), today
    if period == "year":
        return today.replace(month=1, day=1), today
    return today.replace(day=1), today


def dated(queryset, field: str, date_from, date_to):
    """Rows whose ``field`` falls on a local day in ``[date_from, date_to]``."""
    return queryset.filter(**{f"{field}__date__gte": date_from, f"{field}__date__lte": date_to})


def funnel_report(engagements, date_from, date_to, *, attribution: str = "engagement") -> dict:
    """The funnel over the cohort: engagements created in the period."""
    return funnel_by_source(dated(engagements, "created_at", date_from, date_to),
                            attribution=attribution)


def follow_up_report(tasks, engagements, date_from, date_to, *, now=None) -> dict:
    """Follow-up completion over the cohort: dated tasks due in the period,
    plus the open-engagement snapshot."""
    cohort = dated(tasks.filter(due_at__isnull=False), "due_at", date_from, date_to)
    return follow_up_completion(cohort, engagements.filter(status=Engagement.Status.OPEN), now=now)


def default_pipeline(pipelines, wanted: str = ""):
    """The page's pipeline choice: the one whose id is ``wanted``, else the
    first default pipeline, else the first pipeline, else None."""
    if wanted:
        for pipeline in pipelines:
            if str(pipeline.pk) == wanted:
                return pipeline
    defaults = [p for p in pipelines if p.is_default]
    return (defaults or list(pipelines) or [None])[0]


# --- definitions (shown under each page, listed by the API catalog) ------------------

FUNNEL_DEFINITIONS = [
    ("Cohort", "Engagements created in the period (site time zone), in any pipeline, whatever their status today."),
    ("Source", "With the engagement-source toggle, the engagement's own source; with first touch, the channel of the touchpoint that opened it. Blank shows as Unknown and is never dropped."),
    ("Reached qualification", "The engagement's stage history (or, without history, its current stage) includes the pipeline's qualification stage or a stage after it (higher sort order), or it was won. A pipeline with no qualification stage counts only its won engagements."),
    ("Won / Open", "The engagement's outcome today. Closing keeps the stage, so the outcome never follows the stage name."),
    ("Lost", "Lost with a reason that counts as a loss. Disqualified and duplicate (and any reason a deployment marks the same way) close the thread but are counted apart, in their own column, and left out of the win rate."),
    ("Win rate", "Won / (Won + Lost). Open and disqualified engagements are not in the denominator; shows — when nothing has been decided."),
    ("Won value", "Sum of expected value over won engagements, per currency, with no FX conversion. An unknown amount is never counted as zero: a cell shows ≥ when any of its won engagements has no amount, so the value is a lower bound."),
    ("Amount known", "Won engagements with an expected value, of all won engagements in the row."),
]

STAGE_DEFINITIONS = [
    ("Source of truth", "Engagement stage history only (one row per move, win, loss, reopen or pipeline change). Nothing is read from the audit log."),
    ("Entered", "Engagements that entered the stage in the period: created there, moved or reopened into it, or moved into this pipeline at it. One engagement counts once per stage, however often it re-entered."),
    ("Median / P75 days", "Over every completed visit that began in the period: the time from entering the stage to the engagement's next history row (a move, a win, a loss). The 75th percentile interpolates between visits."),
    ("Still in stage", "Visits that began in the period with no later history row: the engagement is still there. Their time so far is shown apart and never mixed into the medians."),
    ("Advanced or won", "Entered engagements that later reached a stage with a higher sort order in this pipeline, or were won."),
    ("Lost here", "Entered engagements whose visit ended with a loss while at this stage (any loss reason)."),
    ("Reconstructed history", "Rows written by a backfill or an import carry a reconstructed time and stage. A visit never starts at one, and a visit that ends at one is counted but not timed; both are counted in the footnote. An engagement with no other history is excluded, and counted there too."),
]

FOLLOW_UP_DEFINITIONS = [
    ("Cohort", "Tasks due in the period (site time zone), by their current due time, with or without an engagement. Undated tasks are never due."),
    ("Completed on time", "Done, with the completion time at or before the due time. An all-day task is due at the end of that day in the zone it was set in."),
    ("Completed late", "Done after the due time."),
    ("Open, overdue", "Still open (open, in progress or waiting) and past the due time now."),
    ("Canceled", "Canceled tasks stay in the cohort."),
    ("Times rescheduled", "Reschedule events recorded on these tasks (task history), whenever they happened."),
    ("On-time rate", "Completed on time / tasks due in the period (canceled and still-open tasks included in the denominator)."),
    ("Without a next action", "Open engagements today with no open task: the next action is the earliest-due open task. Not limited to the period."),
    ("Overdue next actions", "Open engagements today whose next action's due time has passed."),
]

NO_FX_NOTE = "Money is grouped by currency, with no FX conversion."


# --- the machine-facing tables (CSV names, typed values) -----------------------------
#
# Each returns ``{columns, rows, totals, notes, coverage}``. ``columns`` are the
# CSV's canonical names (plus ``amount_incomplete`` on the funnel); a value is
# an integer, a decimal string, a list of per-currency money parts, or None
# when unknown. Percentages are decimal strings with one decimal ("66.7");
# the page rounds the same ratio to a whole percent.


def rate(part: int, whole: int) -> str | None:
    """``part / whole`` as a percentage string with one decimal, None for 0/0."""
    if not whole:
        return None
    return str((Decimal(part) * 100 / Decimal(whole)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def days(value: float | None) -> str | None:
    return None if value is None else f"{value:.1f}"


def money_parts(value: ValueCell) -> list[dict]:
    """Per currency (sorted): the known sum, whether it is a lower bound, and
    how many won engagements had an amount, of how many."""
    return [
        {"currency": currency, "amount": f"{entry[0]:.2f}", "lower_bound": entry[1] < entry[2],
         "won_amount_known": entry[1], "won": entry[2]}
        for currency, entry in sorted(value.by_currency.items())
    ]


FUNNEL_COLUMNS = [
    "source", "created", "reached_qualification", "won", "lost", "not_counted", "open",
    "win_rate", "won_value", "won_amount_known", "amount_incomplete",
]


def _funnel_values(row: FunnelRow) -> dict:
    return {
        "created": row.created, "reached_qualification": row.reached, "won": row.won,
        "lost": row.lost, "not_counted": row.not_counted, "open": row.open,
        "win_rate": rate(row.won, row.won + row.lost),
        "won_value": money_parts(row.value), "won_amount_known": row.value.known,
        "amount_incomplete": row.value.is_lower_bound,
    }


def funnel_table(data: dict) -> dict:
    """The funnel (``funnel_by_source``'s result) as typed rows. ``source`` is
    the source key (or touchpoint channel), None when blank (Unknown)."""
    total = data["total"]
    won, known = total.value.total, total.value.known
    notes = [{"code": "no_fx_conversion", "text": NO_FX_NOTE}]
    if known < won:
        notes.append({
            "code": "amount_lower_bound", "won_amount_known": known, "won": won,
            "text": f"Won value is a lower bound: {won - known} of {won} won engagements "
                    "have no amount, and none is counted as zero.",
        })
    return {
        "columns": FUNNEL_COLUMNS,
        "rows": [{"source": row.key or None, **_funnel_values(row)} for row in data["rows"]],
        "totals": _funnel_values(total),
        "notes": notes,
        "coverage": {"won": won, "won_amount_known": known, "amount_incomplete": known < won},
    }


STAGE_COLUMNS = [
    "stage", "stage_name", "entered", "median_days", "p75_days", "still_in_stage",
    "still_in_stage_median_days", "advanced", "advanced_pct", "lost_here", "lost_here_pct",
]


def stage_table(data: dict | None) -> dict:
    """Stage duration (``stage_duration``'s result, or None with no pipeline)
    as typed rows, with the no-history footnote as structured counts."""
    rows = []
    excluded = reconstructed = 0
    if data is not None:
        excluded, reconstructed = data["excluded"], data["reconstructed_visits"]
        for row in data["rows"]:
            median, p75 = row.median_p75
            entered = len(row.entered)
            rows.append({
                "stage": row.stage.key, "stage_name": row.stage.name, "entered": entered,
                "median_days": days(median), "p75_days": days(p75),
                "still_in_stage": len(row.still_in),
                "still_in_stage_median_days": days(row.still_in_median),
                "advanced": len(row.advanced), "advanced_pct": rate(len(row.advanced), entered),
                "lost_here": len(row.lost_here), "lost_here_pct": rate(len(row.lost_here), entered),
            })
    notes = []
    if excluded:
        notes.append({
            "code": "no_history_excluded", "count": excluded,
            "text": f"{excluded} engagement{'s' if excluded != 1 else ''} excluded: no stage "
                    "history except backfill or import rows (or none), so time in stage is unknown.",
        })
    if reconstructed:
        notes.append({
            "code": "reconstructed_visits", "count": reconstructed,
            "text": f"{reconstructed} visit{'s' if reconstructed != 1 else ''} in the period began "
                    "or ended at a backfill or import row and "
                    f"{'were' if reconstructed != 1 else 'was'} not timed.",
        })
    return {
        "columns": STAGE_COLUMNS,
        "rows": rows,
        "totals": {"entered": sum(r["entered"] for r in rows)},
        "notes": notes,
        "coverage": {"no_history_excluded": excluded, "reconstructed_visits": reconstructed},
    }


FOLLOW_UP_COLUMNS = [
    "assignee", "assignee_id", "due", "completed_on_time", "completed_late", "open_overdue",
    "open_not_yet_due", "cancelled", "times_rescheduled", "on_time_rate",
]


def _follow_up_values(row: FollowUpRow) -> dict:
    return {
        "due": row.due, "completed_on_time": row.on_time, "completed_late": row.late,
        "open_overdue": row.overdue, "open_not_yet_due": row.not_yet_due,
        "cancelled": row.cancelled, "times_rescheduled": row.rescheduled,
        "on_time_rate": rate(row.on_time, row.due),
    }


def follow_up_table(data: dict) -> dict:
    """Follow-up completion as typed rows. ``assignee`` is the staff display
    name and ``assignee_id`` the profile id, both None for unassigned tasks
    (as in the CSV); the open-engagement snapshot sits in ``totals``."""
    return {
        "columns": FOLLOW_UP_COLUMNS,
        "rows": [
            {"assignee": row.label if row.key else None, "assignee_id": row.key or None,
             **_follow_up_values(row)}
            for row in data["rows"]
        ],
        "totals": {
            **_follow_up_values(data["total"]),
            "open_engagements": data["open_engagements"],
            "without_next_action": data["without_next_action"],
            "overdue_next_actions": data["overdue_next_actions"],
        },
        "notes": [],
        "coverage": {"open_engagements": data["open_engagements"]},
    }

# --- multi-touch attribution (CRM Phase 6 step 6) ------------------------------------

MULTI_TOUCH_MODELS = ("first", "last", "linear")


@dataclass
class TouchRow:
    """Credit one channel earned over the cohort's won engagements.

    ``first`` / ``last``: engagements whose first / last touch was this
    channel. ``linear``: the sum of ``1 / n`` over every engagement with
    ``n`` touches on its path (exact; rounded only for display). ``value``:
    the won amounts credited by first touch, per currency, lower-bounded
    like the funnel (never converted).
    """

    key: str
    label: str
    first: int = 0
    last: int = 0
    linear: Fraction = Fraction(0)
    value: ValueCell = field(default_factory=ValueCell)

    @property
    def linear_text(self) -> str:
        return f"{(Decimal(self.linear.numerator) / Decimal(self.linear.denominator)):.2f}"


def multi_touch(engagements) -> dict:
    """Won engagements of the cohort (scoped, dated) credited to channels.

    First touch is the engagement's ``first_touch`` (the funnel's
    ``first_touch`` attribution and the Google Ads adapter read the same
    FK, through ``crm.touchpoints.attribution_for``); the path for last and
    linear is ``crm.touchpoints.paths_for`` (lookback before creation to the
    close). An engagement with no touch at all is counted as ``no_touch``.
    """
    from crm.touchpoints import paths_for

    won = engagements.filter(status=Engagement.Status.WON).select_related("first_touch", "last_touch")
    by_key: dict[str, TouchRow] = {}
    total = TouchRow(TOTAL_CSV, "All channels")
    no_touch = 0

    def row_for(key):
        row = by_key.get(key)
        if row is None:
            row = by_key[key] = TouchRow(key, source_label(key))
        return row

    won = list(won.order_by("closed_at", "pk"))
    paths = paths_for(won)
    for eng in won:
        path = paths[eng.pk]
        # The engagement's stored first touch (crm.touchpoints.attribution_for's
        # ``first``), as the funnel reads it: none is Unknown there and here.
        first = eng.first_touch
        if first is None and not path:
            no_touch += 1
            continue
        path = path or [first]
        first_row = row_for(first.channel if first is not None else "")
        first_row.first += 1
        first_row.value.add(eng.currency, eng.expected_value)
        row_for(path[-1].channel).last += 1
        for touch in path:
            row_for(touch.channel).linear += Fraction(1, len(path))
        total.first += 1
        total.last += 1
        total.linear += 1
        total.value.add(eng.currency, eng.expected_value)
    ordered = sorted(by_key.values(), key=lambda r: (-r.first, -r.linear, r.label.lower(), r.key))
    return {"rows": ordered, "total": total, "no_touch": no_touch, "models": MULTI_TOUCH_MODELS}


def attribution_report(engagements, date_from, date_to) -> dict:
    """Multi-touch credit over the cohort: engagements won in the period."""
    won = engagements.filter(status=Engagement.Status.WON)
    return multi_touch(dated(won, "closed_at", date_from, date_to))


ATTRIBUTION_DEFINITIONS = [
    ("Cohort", "Engagements won in the period (site time zone), in any pipeline."),
    ("Path", "The touchpoints of the engagement's party from the attribution lookback (GOLDEN_CRM attribution_lookback_days) before the engagement was created up to its close, plus any touchpoint linked to the engagement itself."),
    ("First touch", "The engagement's first touch, fixed when it was created: the same touch the funnel's first-touch view and the Google Ads conversion upload use. An engagement created with none shows as Unknown, as in the funnel."),
    ("Last touch", "The latest touchpoint on the path before the close."),
    ("Linear", "Each touchpoint on the path earns 1 / n of the engagement, so every won engagement adds up to 1 across channels."),
    ("Won value (first touch)", "Sum of expected value over the won engagements credited by first touch, per currency, with no FX conversion; ≥ when any has no amount."),
    ("No touch", "Won engagements with no touchpoint at all are counted apart, never dropped."),
]

ATTRIBUTION_COLUMNS = [
    "channel", "first_touch", "last_touch", "linear", "first_touch_won_value", "won_amount_known",
    "amount_incomplete",
]


def _attribution_values(row: TouchRow) -> dict:
    return {
        "first_touch": row.first, "last_touch": row.last, "linear": row.linear_text,
        "first_touch_won_value": money_parts(row.value), "won_amount_known": row.value.known,
        "amount_incomplete": row.value.is_lower_bound,
    }


def attribution_table(data: dict) -> dict:
    """Multi-touch credit as typed rows; ``channel`` is None when blank (Unknown)."""
    notes = [{"code": "no_fx_conversion", "text": NO_FX_NOTE}]
    if data["no_touch"]:
        notes.append({"code": "no_touch", "count": data["no_touch"],
                      "text": f"{data['no_touch']} won engagement{'s' if data['no_touch'] != 1 else ''} "
                              "had no touchpoint and earn no channel credit."})
    return {
        "columns": ATTRIBUTION_COLUMNS,
        "rows": [{"channel": row.key or None, **_attribution_values(row)} for row in data["rows"]],
        "totals": _attribution_values(data["total"]),
        "notes": notes,
        "coverage": {"won": data["total"].first + data["no_touch"], "no_touch": data["no_touch"]},
    }
