"""Label registry: what the staff UI calls each noun (CRM Phase 3, §3).

Same loader shape as ``core/theme_registry.py``: a frozen dataclass per
entry, ``ImproperlyConfigured`` at import on any bad entry, and a rebuild on
``setting_changed`` so tests can override the settings. Importing this module
performs no database query.

Three layers, resolved per lookup in this order (``core.labels`` reads them):

1. **Tenant** — ``settings.GOLDEN_LABEL_TENANT_OVERRIDES``, keyed by Company
   id. The spec's ``core.LabelOverride`` model (edited at
   ``/erp/settings/labels/``) needs a migration and has not landed yet; until
   it does, per-tenant wording is deployment configuration::

       GOLDEN_LABEL_TENANT_OVERRIDES = {
           "6f1c…-company-uuid": {"orders.quote": {"singular": "Proposal"}},
       }

2. **Deployment** — ``settings.GOLDEN_LABEL_OVERRIDES``::

       GOLDEN_LABEL_OVERRIDES = {
           "crm.engagement": {"singular": "Idea", "plural": "Ideas", "article": "an"},
       }

3. **Golden default** — ``GOLDEN_LABELS`` below plus one key per registry
   entry (``party_role.<name>`` from ``parties.role_registry``;
   ``lifecycle_stage.<name>`` and ``contact_type.<name>`` from
   ``parties.classification_registry``). Built into
   core, not a setting: a deployment can reword a key but never add, remove
   or redefine one.

An unknown key, a missing ``singular``, a non-string value or an unknown
field raises ``ImproperlyConfigured`` at import (a typo must not silently do
nothing). ``plural`` defaults to ``singular + "s"`` and ``article`` to
``"an"``/``"a"`` by the first letter, so ``{"singular": "Idea"}`` alone is
enough for regular nouns.

Labels are for nouns in the **staff UI only**. API payloads, CSV exports,
PDFs, emails, signals, webhooks, URLs, ``erp_assist`` output, ``AuditLog``
and log lines always use canonical names (§3.4);
``core/tests/test_labels_never_leak.py`` enforces it.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

from . import cache_guard

ARTICLES = ("a", "an", "")
FIELDS = ("singular", "plural", "article")
MAX_TEXT = 60  # the spec's LabelOverride.singular/plural are CharField(60)
KEY_RE = re.compile(r"\A[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\Z")


@dataclass(frozen=True)
class Label:
    singular: str
    plural: str
    article: str = "a"  # indefinite article: "a" | "an" | "" ("an Engagement", "a Idea")

    def __str__(self) -> str:
        return self.singular


def default_article(singular: str) -> str:
    return "an" if singular[:1].lower() in "aeiou" else "a"


# Built-in nouns. Defaults match the wording the back office already shows,
# so an instance with no overrides renders exactly as before.
GOLDEN_LABELS = MappingProxyType({
    "crm.engagement": Label("Engagement", "Engagements", "an"),
    "crm.pipeline": Label("Pipeline", "Pipelines", "a"),
    "crm.stage": Label("Stage", "Stages", "a"),
    "crm.task": Label("Task", "Tasks", "a"),
    "crm.lead": Label("Lead", "Leads", "a"),
    "crm.touchpoint": Label("Touchpoint", "Touchpoints", "a"),
    "communications.communication": Label("Communication", "Communications", "a"),
    "parties.party": Label("Party", "Parties", "a"),
    "parties.organisation": Label("Organization", "Organizations", "a"),
    "parties.person": Label("Person", "People", "a"),
    "customers.customer": Label("Customer", "Customers", "a"),
    "orders.quote": Label("Quote", "Quotes", "a"),
    "orders.order": Label("Sales Order", "Sales Orders", "a"),
    "orders.sales_invoice": Label("Invoice", "Invoices", "an"),
    "purchasing.supplier": Label("Supplier", "Suppliers", "a"),
})


def _registry_defaults() -> dict[str, Label]:
    """One key per registry entry, its ``label`` as the default.

    Built from the registry's own builder (pure settings parsing) rather than
    its module-level dict, so a ``setting_changed`` rebuild here never sees a
    stale registry that has not reloaded yet. ``loss_reason.*``,
    ``engagement_source.*``, ``task_kind.*`` and ``communication_kind.*`` join
    when their registries land (Phase 3 steps 3 and 4).
    """
    out: dict[str, Label] = {}
    if apps.is_installed("parties"):
        from parties.classification_registry import build_contact_types, build_lifecycle_stages
        from parties.role_registry import build_party_roles

        for prefix, entries in (
            ("party_role", build_party_roles()),
            ("lifecycle_stage", build_lifecycle_stages()),  # phase4-fields.md 2.2
            ("contact_type", build_contact_types()),
        ):
            for name, entry in entries.items():
                out[f"{prefix}.{name}"] = Label(
                    entry.label, f"{entry.label}s", default_article(entry.label)
                )
    return out


def build_defaults() -> dict[str, Label]:
    defaults = dict(GOLDEN_LABELS)
    for key, value in _registry_defaults().items():
        if key in defaults:  # pragma: no cover - a registry key shadowing a built-in
            raise ImproperlyConfigured(f"label key {key!r} is defined twice.")
        defaults[key] = value
    return defaults


def _text(where: str, field: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ImproperlyConfigured(
            f"{where} {field} must be a string, got {type(value).__name__}."
        )
    value = value.strip()
    if field != "article" and not value:
        raise ImproperlyConfigured(f"{where} has an empty {field}.")
    if len(value) > MAX_TEXT:
        raise ImproperlyConfigured(f"{where} {field} is longer than {MAX_TEXT} characters.")
    return value


def label_from_config(where: str, entry: Any) -> Label:
    """One override dict (``singular`` required) as a validated Label."""
    if isinstance(entry, Label):
        entry = {"singular": entry.singular, "plural": entry.plural, "article": entry.article}
    if not isinstance(entry, dict):
        raise ImproperlyConfigured(
            f"{where} must be a dict with singular/plural/article, got {type(entry).__name__}."
        )
    unknown = sorted(set(entry) - set(FIELDS))
    if unknown:
        raise ImproperlyConfigured(
            f"{where} has unknown field(s) {', '.join(map(repr, unknown))}; "
            f"allowed: {', '.join(FIELDS)}."
        )
    if "singular" not in entry:
        raise ImproperlyConfigured(f"{where} is missing singular.")
    singular = _text(where, "singular", entry["singular"])
    plural = _text(where, "plural", entry["plural"]) if "plural" in entry else f"{singular}s"
    if "article" in entry:
        article = _text(where, "article", entry["article"])
        if article not in ARTICLES:
            raise ImproperlyConfigured(
                f"{where} has article={article!r}; must be one of {ARTICLES!r}."
            )
    else:
        article = default_article(singular)
    return Label(singular, plural, article)


def _overrides(where: str, configured: Any, defaults: dict[str, Label]) -> dict[str, Label]:
    if not isinstance(configured, dict):
        raise ImproperlyConfigured(
            f"{where} must be a dict of label key -> override, got {type(configured).__name__}."
        )
    out: dict[str, Label] = {}
    for key, entry in configured.items():
        if key not in defaults:
            hint = "" if isinstance(key, str) and KEY_RE.fullmatch(key) else " (keys look like 'crm.engagement')"
            raise ImproperlyConfigured(f"{where} names an unknown label key {key!r}{hint}.")
        out[key] = label_from_config(f"{where}[{key!r}]", entry)
    return out


def build_deployment(defaults: dict[str, Label]) -> dict[str, Label]:
    """settings.GOLDEN_LABEL_OVERRIDES, validated against the known keys."""
    return _overrides(
        "GOLDEN_LABEL_OVERRIDES", getattr(settings, "GOLDEN_LABEL_OVERRIDES", None) or {}, defaults
    )


def build_tenants(defaults: dict[str, Label]) -> dict[str, dict[str, Label]]:
    """settings.GOLDEN_LABEL_TENANT_OVERRIDES, keyed by normalised Company id."""
    configured = getattr(settings, "GOLDEN_LABEL_TENANT_OVERRIDES", None) or {}
    if not isinstance(configured, dict):
        raise ImproperlyConfigured(
            "GOLDEN_LABEL_TENANT_OVERRIDES must be a dict of Company id -> overrides, "
            f"got {type(configured).__name__}."
        )
    out: dict[str, dict[str, Label]] = {}
    for company_id, overrides in configured.items():
        try:
            normalised = str(uuid.UUID(str(company_id)))
        except ValueError:
            raise ImproperlyConfigured(
                f"GOLDEN_LABEL_TENANT_OVERRIDES key {company_id!r} is not a Company id (UUID)."
            ) from None
        if normalised in out:
            raise ImproperlyConfigured(
                f"GOLDEN_LABEL_TENANT_OVERRIDES lists Company {normalised} twice."
            )
        where = f"GOLDEN_LABEL_TENANT_OVERRIDES[{company_id!r}]"
        out[normalised] = _overrides(where, overrides, defaults)
    return out


# Mutated in place on reload (below) so that `from … import DEFAULTS` holds.
DEFAULTS: dict[str, Label] = build_defaults()
DEPLOYMENT: dict[str, Label] = build_deployment(DEFAULTS)
TENANTS: dict[str, dict[str, Label]] = build_tenants(DEFAULTS)

_SETTINGS = (
    "GOLDEN_LABEL_OVERRIDES", "GOLDEN_LABEL_TENANT_OVERRIDES", "GOLDEN_PARTY_ROLES",
    "GOLDEN_LIFECYCLE_STAGES", "GOLDEN_CONTACT_TYPES",
)


@receiver(setting_changed)
def _reload_registry(sender, setting, **kwargs) -> None:
    if setting not in _SETTINGS:
        return
    defaults = build_defaults()
    deployment = build_deployment(defaults)
    tenants = build_tenants(defaults)
    DEFAULTS.clear()
    DEFAULTS.update(defaults)
    DEPLOYMENT.clear()
    DEPLOYMENT.update(deployment)
    TENANTS.clear()
    TENANTS.update(tenants)


def is_label_key(key: Any) -> bool:
    return isinstance(key, str) and key in DEFAULTS


# --- the stored tenant layer (core.LabelOverride) --------------------------------
#
# Rows are read through the cache: one entry per company (its overrides as
# Label objects) and one flag saying whether any row exists at all, so a
# deployment with no stored overrides pays one cached lookup per process,
# never a query per request. The model's receivers call invalidate_tenant.

CACHE_TIMEOUT = 600
# How long a process serves labels from memory while the cache is down
# (F-086): overrides are near-static, so the database is asked once a minute
# per process, not once per 5 s per page.
OUTAGE_FALLBACK_S = 60.0
_ANY_KEY = "golden.labels.any.v1"


def _tenant_key(company_id: str) -> str:
    return f"golden.labels.tenant.v1.{company_id}"


def stored_any() -> bool:
    """Whether any ``LabelOverride`` row exists (cached).

    Reads go through ``core.cache_guard``: with the cache unreachable
    (F-086) the flag is read from the database once per
    ``OUTAGE_FALLBACK_S`` per process, never per label call.
    """
    flag = cache_guard.cache_get(_ANY_KEY, what="label cache")
    if flag is None:
        from .models import LabelOverride

        flag = LabelOverride.objects.exists()
        cache_guard.cache_set(_ANY_KEY, flag, CACHE_TIMEOUT, what="label cache", fallback_ttl=OUTAGE_FALLBACK_S)
    return bool(flag)


def stored_tenant(company_id: str) -> dict[str, Label]:
    """``key -> Label`` from the rows of one company (cached). Unknown keys
    (a row left by a removed registry entry) are skipped, never raised: a
    stored row must not take the boot down the way a setting does."""
    if not stored_any():
        return {}
    key = _tenant_key(company_id)
    rows = cache_guard.cache_get(key, what="label cache")
    if rows is None:
        from .models import LabelOverride

        rows = list(LabelOverride.objects.filter(company_id=company_id)
                    .values_list("key", "singular", "plural", "article"))
        cache_guard.cache_set(key, rows, CACHE_TIMEOUT, what="label cache", fallback_ttl=OUTAGE_FALLBACK_S)
    out: dict[str, Label] = {}
    for label_key, singular, plural, article in rows:
        if label_key not in DEFAULTS:
            continue
        singular = (singular or "").strip()
        if not singular:
            continue
        plural = (plural or "").strip() or singular
        # An empty article means "derive it" (the screen's default), as the
        # setting layer does when the key is left out.
        out[label_key] = Label(singular, plural, article if article in ("a", "an") else default_article(singular))
    return out


def invalidate_tenant(company_id) -> None:
    # With the cache unreachable the old rows stay cached until CACHE_TIMEOUT
    # (the guard logs it); the fallback copy in this process is dropped.
    cache_guard.cache_delete_many([_ANY_KEY, _tenant_key(str(company_id))], what="label cache")


def tenant_layer(company_id: str) -> dict[str, Label]:
    """The tenant layer for one company: the setting's entry, then the stored
    rows on top (a row edited on the screen wins over the settings file)."""
    configured = TENANTS.get(company_id, {})
    stored = stored_tenant(company_id)
    if not stored:
        return configured
    return {**configured, **stored}


def has_tenant_overrides() -> bool:
    """Whether resolving a request's company can change any label."""
    return bool(TENANTS) or stored_any()
