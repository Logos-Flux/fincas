"""CRM settings registries (docs/crm/phase3-engagement.md 1.1, 1.2).

Same loader shape as ``parties/role_registry.py``: a frozen dataclass per
entry, ``ImproperlyConfigured`` at import on any bad entry (``CrmConfig.ready``
imports this module), and a rebuild on ``setting_changed`` so tests can
override a setting. No database access.

- ``GOLDEN_LOSS_REASONS``         ``Engagement.loss_reason``
- ``GOLDEN_ENGAGEMENT_SOURCES``   ``Engagement.source``, ``Touchpoint.channel``
- ``GOLDEN_TASK_KINDS``           ``Task.kind``
- ``GOLDEN_DEFAULT_PIPELINES``    the pipelines seeded for a new Company

The first three hold built-ins a deployment cannot remove or redefine; a
deployment appends entries::

    GOLDEN_LOSS_REASONS = [{"name": "credit", "label": "Failed credit check"}]
    GOLDEN_TASK_KINDS = [{"name": "site_visit", "label": "Site visit",
                          "icon": "ti ti-map-pin"}]

``GOLDEN_DEFAULT_PIPELINES`` *replaces* golden's seed when set (a list of
``{key, name, stages: [{key, name, default_probability}]}``; the first
pipeline is the default). After seeding, pipelines are tenant data.

Keys are ``[a-z][a-z0-9_]{0,31}`` (the ``parties/_registry.py`` rule) and are
persisted: never rename one in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

from parties._registry import check_key, check_label, configured_list

UNKNOWN_LOSS_REASON = "unknown"


@dataclass(frozen=True)
class LossReason:
    name: str
    label: str
    icon: str = ""
    # False: closes the thread but is left out of win-rate denominators (4.2).
    counts_as_loss: bool = True


@dataclass(frozen=True)
class EngagementSource:
    name: str
    label: str
    icon: str = ""


@dataclass(frozen=True)
class TaskKind:
    name: str
    label: str
    icon: str = ""


@dataclass(frozen=True)
class StageSeed:
    key: str
    name: str
    default_probability: int | None = None
    outcome_hint: str = ""


@dataclass(frozen=True)
class PipelineSeed:
    key: str
    name: str
    stages: tuple[StageSeed, ...]


BUILTIN_LOSS_REASONS: tuple[LossReason, ...] = (
    LossReason("price", "Price"),
    LossReason("competitor", "Lost to a competitor"),
    LossReason("timing", "Timing"),
    LossReason("budget", "No budget"),
    LossReason("no_response", "No response"),
    LossReason("scope", "Scope mismatch"),
    LossReason("expired", "Expired"),
    LossReason("cancelled", "Canceled"),
    LossReason("disqualified", "Disqualified", counts_as_loss=False),
    LossReason("duplicate", "Duplicate", counts_as_loss=False),
    LossReason("other", "Other"),
    LossReason(UNKNOWN_LOSS_REASON, "Unknown"),
)

# Every Lead.Source value is here (website_form .. ai_chat, other), so the
# lead backfill maps one to one.
BUILTIN_ENGAGEMENT_SOURCES: tuple[EngagementSource, ...] = (
    EngagementSource("website_form", "Website form"),
    EngagementSource("phone", "Phone"),
    EngagementSource("email", "Email"),
    EngagementSource("trade_show", "Trade show"),
    EngagementSource("referral", "Referral"),
    EngagementSource("marketplace", "Marketplace"),
    EngagementSource("social", "Social media"),
    EngagementSource("paid_search", "Paid search"),
    EngagementSource("paid_social", "Paid social"),
    EngagementSource("partner", "Partner"),
    EngagementSource("outbound", "Outbound"),
    EngagementSource("ai_chat", "AI chat (Claude, Gemini, Muse)"),
    EngagementSource("other", "Other"),
)

BUILTIN_TASK_KINDS: tuple[TaskKind, ...] = (
    TaskKind("todo", "To-do", "ti ti-checkbox"),
    TaskKind("call", "Call", "ti ti-phone"),
    TaskKind("email", "Email", "ti ti-mail"),
    TaskKind("meeting", "Meeting", "ti ti-calendar-event"),
    TaskKind("follow_up", "Follow up", "ti ti-arrow-forward-up"),
)

# Golden's default pipeline (1.2): the union of Lead.Status and
# Quote.SalesStage open values, so both backfills are lossless, then
# Closed/Signed, which proposes closing as won when a deal enters it
# (added 2026-10-15; crm.0019 appends it to existing ``sales`` pipelines).
CLOSED_SIGNED_STAGE = StageSeed("closed_signed", "Closed/Signed", 100, outcome_hint="won")
GOLDEN_SALES_PIPELINE = PipelineSeed(
    "sales", "Sales",
    (
        StageSeed("new", "New", 5),
        StageSeed("prospecting", "Prospecting", 10),
        StageSeed("qualification", "Qualification", 20),
        StageSeed("needs_analysis", "Needs analysis", 30),
        StageSeed("proposal_sent", "Proposal sent", 50),
        StageSeed("negotiation", "Negotiation", 70),
        StageSeed("verbal_commit", "Verbal commit", 90),
        CLOSED_SIGNED_STAGE,
    ),
)

OUTCOME_HINTS = ("", "won", "lost")


def _check_icon(where: str, icon: Any) -> None:
    if not isinstance(icon, str) or (icon and not icon.startswith("ti ti-")):
        raise ImproperlyConfigured(f"{where} has icon={icon!r}; use a 'ti ti-*' class or omit it.")


def _entry(setting: str, cls, entry: Any, index: int):
    where = f"{setting}[{index}]"
    if isinstance(entry, cls):
        item = entry
    elif isinstance(entry, dict):
        try:
            item = cls(**entry)
        except TypeError as exc:
            raise ImproperlyConfigured(f"{where} is not a valid {cls.__name__}: {exc}") from exc
    else:
        raise ImproperlyConfigured(
            f"{where} must be a dict of {cls.__name__} kwargs, got {type(entry).__name__}."
        )
    check_key(where, item.name)
    where = f"{where} ({item.name})"
    check_label(where, "label", item.label)
    _check_icon(where, item.icon)
    if hasattr(item, "counts_as_loss") and not isinstance(item.counts_as_loss, bool):
        raise ImproperlyConfigured(f"{where} counts_as_loss must be True or False.")
    return item


def _build(setting: str, cls, builtins: tuple) -> dict:
    configured = configured_list(setting, getattr(settings, setting, []))
    builtin_names = frozenset(b.name for b in builtins)
    entries = {b.name: b for b in builtins}
    for i, raw in enumerate(configured):
        item = _entry(setting, cls, raw, i)
        if item.name in builtin_names:
            raise ImproperlyConfigured(
                f"{setting}[{i}] redefines the built-in {item.name!r}; built-ins "
                f"({', '.join(sorted(builtin_names))}) are fixed. Declare a new key instead."
            )
        if item.name in entries:
            raise ImproperlyConfigured(f"{setting} reuses the name {item.name!r}.")
        entries[item.name] = item
    return entries


def build_loss_reasons() -> dict[str, LossReason]:
    return _build("GOLDEN_LOSS_REASONS", LossReason, BUILTIN_LOSS_REASONS)


def build_engagement_sources() -> dict[str, EngagementSource]:
    return _build("GOLDEN_ENGAGEMENT_SOURCES", EngagementSource, BUILTIN_ENGAGEMENT_SOURCES)


def build_task_kinds() -> dict[str, TaskKind]:
    return _build("GOLDEN_TASK_KINDS", TaskKind, BUILTIN_TASK_KINDS)


def _check_name(where: str, name: Any) -> None:
    check_label(where, "name", name)
    if len(name) > 100:  # Pipeline.name / Stage.name are CharField(100)
        raise ImproperlyConfigured(f"{where} name is longer than 100 characters.")


def _probability(where: str, value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ImproperlyConfigured(f"{where} default_probability={value!r}; use 0-100 or None.")
    return value


def _stage_seed(where: str, raw: Any) -> StageSeed:
    if not isinstance(raw, dict):
        raise ImproperlyConfigured(f"{where} must be a dict, got {type(raw).__name__}.")
    unknown = set(raw) - {"key", "name", "default_probability", "outcome_hint"}
    if unknown:
        raise ImproperlyConfigured(f"{where} has unknown field(s) {sorted(unknown)}.")
    key = raw.get("key")
    check_key(where, key)
    where = f"{where} ({key})"
    _check_name(where, raw.get("name"))
    hint = raw.get("outcome_hint", "")
    if hint not in OUTCOME_HINTS:
        raise ImproperlyConfigured(f"{where} outcome_hint={hint!r}; use '', 'won' or 'lost'.")
    return StageSeed(key, raw["name"], _probability(where, raw.get("default_probability")), hint)


def _pipeline_seed(where: str, raw: Any) -> PipelineSeed:
    if isinstance(raw, PipelineSeed):
        raw = {"key": raw.key, "name": raw.name,
               "stages": [s.__dict__ for s in raw.stages]}
    if not isinstance(raw, dict):
        raise ImproperlyConfigured(f"{where} must be a dict, got {type(raw).__name__}.")
    unknown = set(raw) - {"key", "name", "stages"}
    if unknown:
        raise ImproperlyConfigured(f"{where} has unknown field(s) {sorted(unknown)}.")
    key = raw.get("key")
    check_key(where, key)
    where = f"{where} ({key})"
    _check_name(where, raw.get("name"))
    stages = raw.get("stages")
    if isinstance(stages, str) or not isinstance(stages, (list, tuple)) or not stages:
        raise ImproperlyConfigured(f"{where} stages must be a non-empty list.")
    seeds = tuple(_stage_seed(f"{where} stages[{i}]", s) for i, s in enumerate(stages))
    keys = [s.key for s in seeds]
    if len(set(keys)) != len(keys):
        raise ImproperlyConfigured(f"{where} lists a stage key twice.")
    return PipelineSeed(key, raw["name"], seeds)


def build_default_pipelines() -> tuple[PipelineSeed, ...]:
    configured = getattr(settings, "GOLDEN_DEFAULT_PIPELINES", None)
    if configured is None:
        return (GOLDEN_SALES_PIPELINE,)
    configured = configured_list("GOLDEN_DEFAULT_PIPELINES", configured)
    if not configured:
        raise ImproperlyConfigured(
            "GOLDEN_DEFAULT_PIPELINES is empty; every company needs a default pipeline. "
            "Unset it to use golden's."
        )
    seeds = tuple(
        _pipeline_seed(f"GOLDEN_DEFAULT_PIPELINES[{i}]", raw) for i, raw in enumerate(configured)
    )
    keys = [s.key for s in seeds]
    if len(set(keys)) != len(keys):
        raise ImproperlyConfigured("GOLDEN_DEFAULT_PIPELINES lists a pipeline key twice.")
    return seeds


_LOSS_REASONS: dict[str, LossReason] = build_loss_reasons()
_SOURCES: dict[str, EngagementSource] = build_engagement_sources()
_TASK_KINDS: dict[str, TaskKind] = build_task_kinds()
_DEFAULT_PIPELINES: list[PipelineSeed] = list(build_default_pipelines())

# Read-only views; the backing containers are refilled in place on reload,
# so `from crm.registries import LOSS_REASONS` stays current.
LOSS_REASONS = MappingProxyType(_LOSS_REASONS)
ENGAGEMENT_SOURCES = MappingProxyType(_SOURCES)
TASK_KINDS = MappingProxyType(_TASK_KINDS)

_BUILDERS = {
    "GOLDEN_LOSS_REASONS": (_LOSS_REASONS, build_loss_reasons),
    "GOLDEN_ENGAGEMENT_SOURCES": (_SOURCES, build_engagement_sources),
    "GOLDEN_TASK_KINDS": (_TASK_KINDS, build_task_kinds),
}


@receiver(setting_changed)
def _reload_registry(sender, setting, **kwargs) -> None:
    if setting == "GOLDEN_DEFAULT_PIPELINES":
        rebuilt = build_default_pipelines()
        _DEFAULT_PIPELINES[:] = rebuilt
        return
    if setting not in _BUILDERS:
        return
    target, build = _BUILDERS[setting]
    rebuilt = build()
    target.clear()
    target.update(rebuilt)


def default_pipelines() -> tuple[PipelineSeed, ...]:
    """The pipelines seeded for a new Company; the first is the default."""
    return tuple(_DEFAULT_PIPELINES)


def counted_loss_reasons() -> tuple[str, ...]:
    """Loss reasons that count in win-rate denominators (4.2)."""
    return tuple(name for name, r in LOSS_REASONS.items() if r.counts_as_loss)


__all__ = [
    "BUILTIN_ENGAGEMENT_SOURCES",
    "BUILTIN_LOSS_REASONS",
    "BUILTIN_TASK_KINDS",
    "ENGAGEMENT_SOURCES",
    "EngagementSource",
    "GOLDEN_SALES_PIPELINE",
    "LOSS_REASONS",
    "LossReason",
    "PipelineSeed",
    "StageSeed",
    "TASK_KINDS",
    "TaskKind",
    "UNKNOWN_LOSS_REASON",
    "build_default_pipelines",
    "build_engagement_sources",
    "build_loss_reasons",
    "build_task_kinds",
    "counted_loss_reasons",
    "default_pipelines",
]
