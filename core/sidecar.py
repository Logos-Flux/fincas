"""The ``metadata`` sidecar contract (docs/crm/phase4-fields.md section 3).

These models carry ``metadata = JSONField(default=dict)``: Party, PartyRole,
PartyRelationship, Lead, Pipeline, Engagement, Touchpoint, Task and
Communication (``SIDECAR_MODELS``). A key's namespace says who may write it
(3.1):

- **system** (no dot, or the reserved ``_*`` / ``provenance*`` names):
  golden code only, each key registered per object in ``SYSTEM_KEYS``.
  Golden code writes these directly; ``set_fields`` refuses them.
- **custom** (``custom.<name>``): canonical deployment fields. A key needs a
  ``FieldDefinition`` (``core.field_registry``, ``GOLDEN_FIELD_DEFINITIONS``)
  for the object's company; its value is validated and coerced to the
  canonical encoding (3.2).
- **provider** (``<provider>.<name>``): provenance written only by imports
  and plugin ingest (``source`` starting ``import`` or ``plugin:``).

Values (3.2) are a JSON string, integer or boolean, or a list of up to 100 of
them. No nested objects, no floats (a decimal is a string), never ``null``
(writing ``None`` removes the key). A string is at most 2,000 characters, an
object holds at most 200 keys and serialises to at most 32 KB.

The service:

- ``set_fields(obj, values, *, source, by=None)`` validates, merges under a
  row lock, saves and writes one ``AuditLog`` row.
- ``validate_fields(obj, values, *, source)`` is the same check without the
  write (forms call it from ``clean()``).
- ``read(obj, key)`` / ``fields_for(obj, group=None)`` read typed values.
- ``filter_q(model, key, value)`` is an equality filter (3.4).
- ``strip(obj, keys)`` removes keys of any namespace (erasure, promotion).

Importing this module performs no database query.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import warnings
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import connections, router, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

logger = logging.getLogger(__name__)

# --- keys and namespaces (3.1) --------------------------------------------------

SYSTEM_KEY_RE = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
NAMESPACE_RE = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")
DOTTED_KEY_RE = re.compile(r"\A[a-z][a-z0-9_]{0,31}\.[a-z0-9][a-z0-9_]{0,63}\Z")
# Personal data in a key name (7.1): an address or a phone-like digit run.
PERSONAL_IN_KEY_RE = re.compile(r"@|\d{7,}")

SYSTEM = "system"
CUSTOM = "custom"
PROVIDER = "provider"
CUSTOM_NAMESPACE = "custom"
# Reserved for golden code since #64: ``_private`` keys and ``provenance`` /
# ``provenance.*`` (a dotted key that is *not* a provider's).
RESERVED_PREFIXES = ("_", "provenance")
RESERVED_NAMESPACES = frozenset({"provenance"})

# Writers allowed to write provider keys: imports and plugin ingest.
PROVIDER_SOURCE_PREFIXES = ("import", "plugin:")

# Object name -> model label. FieldDefinitions apply to DEFINABLE_OBJECTS.
SIDECAR_MODELS = MappingProxyType({
    "party": "parties.Party",
    "party_role": "parties.PartyRole",
    "party_relationship": "parties.PartyRelationship",
    "lead": "crm.Lead",
    "pipeline": "crm.Pipeline",
    "engagement": "crm.Engagement",
    "touchpoint": "crm.Touchpoint",
    "task": "crm.Task",
    "communication": "communications.Communication",
})
DEFINABLE_OBJECTS = ("party", "engagement", "task")

# Keys golden code writes into each sidecar and reads back: the syncs find
# their own rows by them (``{"lead": pk}``, ``{"customer": pk}``), backfills
# and seeds mark what they made, erasure marks what it scrubbed. Register a
# key here when code starts writing it; ``core/tests/test_sidecar_registry``
# greps the source and fails on an unregistered one, and ``check_sidecar``
# reports unregistered un-dotted keys found in the data.
SYSTEM_KEYS = MappingProxyType({
    # parties.sync (name_inferred), parties.backfill, seed_parties.
    "party": frozenset({"backfill", "name_inferred", "seed", "seed_index"}),
    # The Customer / contact / lead syncs find their own rows by these.
    "party_role": frozenset({"backfill", "customer", "lead"}),
    "party_relationship": frozenset({"backfill", "contact", "customer"}),
    # The lead API's create-only attribution fields (crm.intake reads them;
    # CRM Phase 6 3.5). They identify the visitor: erasure clears them.
    "lead": frozenset({"click_id", "click_id_kind", "landing_url", "utm_content", "utm_term",
                       "visitor_id"}),
    "pipeline": frozenset(),
    # crm.backfill (quote chains and leads), seed_crm.
    "engagement": frozenset({
        "backfill", "lead_legacy", "quote_legacy", "seed", "seed_index", "unmapped_lead_status",
        "unmapped_stage",
    }),
    # crm.intake and crm.backfill (lead touches), seed_crm; crm.touchpoints
    # (visitor_id links a later identification, event_id dedupes a retried post).
    "touchpoint": frozenset({"event_id", "lead", "seed", "visitor_id"}),
    # crm.intake / crm.backfill (lead follow-ups), the ActivityLog mirror;
    # erasure marks the tasks it scrubbed (CRM Phase 6 step 8).
    "task": frozenset({"activitylog", "erased_at", "from_activitylog", "lead"}),
    # crm.intake / crm.backfill (lead notes), communications.backfill and
    # email_link (sent documents), the ActivityLog mirror, the back office's
    # log form, seed_crm.
    "communication": frozenset({
        "activitylog", "backfill", "backfill_step", "document_number", "document_type",
        "from_activitylog", "lead", "lead_direction", "lead_number", "lead_source", "logged_from",
        "request_type", "seed", "template_name",
    }),
})

# Keys moved from a sidecar to a column (7.2): ``{"engagement": {"custom.x":
# "x"}}``. For one minor release ``set_fields`` and ``custom_fields`` route a
# write of the old key to the column with a DeprecationWarning, through the
# model's ``full_clean`` and ``save``; then the key moves to ``RETIRED_KEYS``
# and a write is refused (``metadata_key_promoted``). Step 4 (2.7) promoted
# lifecycle stage, contact type and owner team: those columns were never
# sidecar keys in golden, but a deployment may have used the ``custom.*``
# names before the columns existed, so they route (and cannot be defined).
PROMOTED_KEYS: dict[str, dict[str, str]] = {
    "party": {
        "custom.lifecycle_stage": "lifecycle_stage",
        "custom.contact_type": "contact_type",
        "custom.owner_team": "owner_team",
    },
    "engagement": {"custom.owner_team": "owner_team"},
    "task": {},
}
RETIRED_KEYS: dict[str, frozenset[str]] = {name: frozenset() for name in DEFINABLE_OBJECTS}


def all_system_keys() -> frozenset[str]:
    """Every registered system key, on any sidecar."""
    return frozenset().union(*SYSTEM_KEYS.values())


def namespace(key: Any) -> str | None:
    """``"system"``, ``"custom"``, ``"provider"``, or None for a key that
    breaks the 3.1 key rule."""
    if not isinstance(key, str) or not key:
        return None
    if key.startswith("_") or key == "provenance" or key.startswith("provenance."):
        return SYSTEM
    if SYSTEM_KEY_RE.fullmatch(key):
        return SYSTEM
    if DOTTED_KEY_RE.fullmatch(key):
        ns = key.split(".", 1)[0]
        return CUSTOM if ns == CUSTOM_NAMESPACE else PROVIDER
    return None


def is_system_key(key: Any, object_name: str | None = None) -> bool:
    """A *registered* system key (or a reserved ``_*`` / ``provenance*`` one).

    With ``object_name``, registered for that sidecar; without, for any.
    An un-dotted key nobody registered is in the system namespace
    (``namespace``) but is not a system key: erasure does not keep it.
    """
    key = str(key)
    if key.startswith(RESERVED_PREFIXES):
        return True
    if object_name is None:
        return key in all_system_keys()
    return key in SYSTEM_KEYS.get(object_name, ())


def object_name_for(model_or_obj) -> str:
    """The sidecar object name (``"party"``…) of a model class or instance."""
    label = model_or_obj._meta.label
    for name, model_label in SIDECAR_MODELS.items():
        if model_label == label:
            return name
    raise TypeError(f"{label} is not a sidecar model ({', '.join(SIDECAR_MODELS.values())}).")


def is_provider_source(source: str | None) -> bool:
    return bool(source) and str(source).startswith(PROVIDER_SOURCE_PREFIXES)


# --- values (3.2) -----------------------------------------------------------------

MAX_STRING = 2000
MAX_LIST = 100
MAX_KEYS = 200
MAX_BYTES = 32 * 1024
MAX_INT = 2 ** 53

# Refusal codes. The API returns them as DRF error codes.
KEY_INVALID = "metadata_key_invalid"
KEY_PROTECTED = "metadata_key_protected"
KEY_PROVIDER = "metadata_key_provider"
KEY_UNDEFINED = "metadata_key_undefined"
KEY_PROMOTED = "metadata_key_promoted"
VALUE_INVALID = "metadata_value_invalid"
TOO_LARGE = "metadata_too_large"


class SidecarError(ValidationError):
    """A refused sidecar write. ``errors`` maps each key to ``(code, message)``."""

    def __init__(self, errors: dict[str, tuple[str, str]]):
        self.errors = dict(errors)
        super().__init__({
            key: [ValidationError(message, code=code)] for key, (code, message) in self.errors.items()
        })

    @property
    def codes(self) -> set[str]:
        return {code for code, _ in self.errors.values()}


def check_value(value) -> tuple[str, str] | None:
    """``(code, reason)`` when ``value`` is not a storable sidecar value (3.2),
    else None. The code is ``metadata_too_large`` for a limit, else
    ``metadata_value_invalid``."""
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_LIST:
            return TOO_LARGE, f"a list holds at most {MAX_LIST} values"
        for item in value:
            problem = _scalar_problem(item)
            if problem:
                return problem[0], f"list item: {problem[1]}"
        return None
    return _scalar_problem(value)


def _scalar_problem(value) -> tuple[str, str] | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if not -MAX_INT <= value <= MAX_INT:
            return TOO_LARGE, "an integer must be within +/- 2^53"
        return None
    if isinstance(value, str):
        if len(value) > MAX_STRING:
            return TOO_LARGE, f"a string holds at most {MAX_STRING} characters"
        return None
    if isinstance(value, dict):
        return VALUE_INVALID, "nested objects are not allowed in a sidecar"
    if isinstance(value, (list, tuple)):
        return VALUE_INVALID, "lists cannot nest"
    if isinstance(value, float):
        return VALUE_INVALID, "floats are not allowed (send a decimal as a string)"
    return VALUE_INVALID, f"{type(value).__name__} is not a string, integer, boolean or list of them"


def size_of(metadata: dict) -> int:
    return len(json.dumps(metadata, separators=(",", ":"), ensure_ascii=False, default=str).encode())


# --- typed coercion (3.2, 4.1) ----------------------------------------------------

_url = URLValidator(schemes=["http", "https"])


def _utc_z(value: datetime.datetime) -> str:
    value = value.astimezone(datetime.timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def coerce(definition, value):
    """``value`` in the definition's canonical encoding, or ``ValueError``.

    Returns None for a blank value (an empty string or list): absent means
    unknown, so a blank form field removes the key.
    """
    kind = definition.type
    if value is None or value == "" or value == []:
        return None
    if kind in ("string", "text", "url"):
        if not isinstance(value, str):
            raise ValueError("must be a string")
        if len(value) > MAX_STRING:
            raise ValueError(f"at most {MAX_STRING} characters")
        if kind == "string" and ("\n" in value or "\r" in value):
            raise ValueError("a single line (use a text field for several)")
        if kind == "url":
            try:
                _url(value)
            except ValidationError:
                raise ValueError("must be an http(s) URL") from None
        return value
    if kind == "integer":
        if isinstance(value, bool):
            raise ValueError("must be an integer")
        if isinstance(value, str) and re.fullmatch(r"-?\d{1,16}", value.strip()):
            value = int(value.strip())
        if not isinstance(value, int):
            raise ValueError("must be an integer")
        if not -MAX_INT <= value <= MAX_INT:
            raise ValueError("must be within +/- 2^53")
        return value
    if kind == "decimal":
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError("must be a decimal number")
        try:
            number = Decimal(str(value).strip())
        except InvalidOperation:
            raise ValueError("must be a decimal number") from None
        if not number.is_finite():
            raise ValueError("must be a finite number")
        text = format(number, "f")
        if len(text) > 64:
            raise ValueError("too many digits")
        return text
    if kind == "date":
        if isinstance(value, datetime.datetime):
            raise ValueError("must be a date (YYYY-MM-DD), not a date-time")
        if isinstance(value, datetime.date):
            return value.isoformat()
        parsed = parse_date(value) if isinstance(value, str) else None
        if parsed is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("must be a date (YYYY-MM-DD)")
        return parsed.isoformat()
    if kind == "datetime":
        if isinstance(value, str):
            try:
                parsed = parse_datetime(value)
            except ValueError:
                parsed = None
            if parsed is None:
                raise ValueError("must be an ISO 8601 date-time with a time zone")
            value = parsed
        if not isinstance(value, datetime.datetime):
            raise ValueError("must be an ISO 8601 date-time with a time zone")
        if timezone.is_naive(value):
            raise ValueError("must carry a time zone (UTC 'Z' or an offset)")
        return _utc_z(value)
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError("must be true or false")
        return value
    if kind == "choice":
        if not isinstance(value, str) or value not in definition.choice_values:
            raise ValueError(f"must be one of {', '.join(definition.choice_values)}")
        return value
    if kind == "multi_choice":
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("must be a list of choices")
        unknown = [v for v in value if not isinstance(v, str) or v not in definition.choice_values]
        if unknown:
            raise ValueError(f"unknown choice(s) {unknown!r}; one of {', '.join(definition.choice_values)}")
        chosen = set(value)
        # Canonical: definition order, no repeats (so equality filters agree).
        return [v for v in definition.choice_values if v in chosen]
    raise ValueError(f"unknown type {kind!r}")  # pragma: no cover - the registry refuses it


def decode(definition, value):
    """A stored canonical value as its Python type (``ValueError`` if invalid)."""
    kind = definition.type
    if kind == "decimal":
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("not a decimal")
        try:
            return Decimal(str(value))
        except InvalidOperation:
            raise ValueError("not a decimal") from None
    if kind == "date":
        parsed = parse_date(value) if isinstance(value, str) else None
        if parsed is None:
            raise ValueError("not a date")
        return parsed
    if kind == "datetime":
        parsed = parse_datetime(value) if isinstance(value, str) else None
        if parsed is None or timezone.is_naive(parsed):
            raise ValueError("not an aware date-time")
        return parsed
    # The rest are stored as they read; check they still validate.
    if coerce(definition, value) != value:
        raise ValueError("not in canonical form")
    return value


# --- definitions lookup -----------------------------------------------------------


def _company_id(obj):
    company_id = getattr(obj, "company_id", None)
    return str(company_id) if company_id is not None else None


def definitions(object_name: str, company_id=None):
    """The FieldDefinitions for ``object_name`` visible to ``company_id``."""
    from .field_registry import definitions_for

    return definitions_for(object_name, company_id)


def definition_for(object_name: str, key: str, company_id=None, *, any_company: bool = False):
    from .field_registry import get_definition

    return get_definition(object_name, key, company_id, any_company=any_company)


# --- writes (3.3) -----------------------------------------------------------------


@dataclass
class Plan:
    """What a validated write will do."""

    set: dict[str, Any] = field(default_factory=dict)      # key -> canonical value
    remove: list[str] = field(default_factory=list)
    routed: dict[str, Any] = field(default_factory=dict)   # promoted column -> value
    routed_from: dict[str, str] = field(default_factory=dict)  # promoted column -> its old key
    deprecated: list[str] = field(default_factory=list)    # promoted keys written

    @property
    def empty(self) -> bool:
        return not (self.set or self.remove or self.routed)


def plan_fields(obj, values: dict, *, source: str, company_id=None) -> Plan:
    """Validate ``values`` for ``obj`` and return the write plan, or raise
    ``SidecarError`` naming every refused key. No database access beyond
    what ``obj`` already holds."""
    if not isinstance(values, dict):
        raise SidecarError({"__all__": (VALUE_INVALID, "Sidecar values must be an object.")})
    object_name = object_name_for(obj)
    if company_id is None:
        company_id = _company_id(obj)
    plan, errors = Plan(), {}
    for key, value in values.items():
        ns = namespace(key)
        if ns is None:
            errors[str(key)] = (KEY_INVALID, (
                f"{key!r} is not a sidecar key: 'custom.<name>' or '<provider>.<name>' "
                "([a-z][a-z0-9_]{0,31} . [a-z0-9][a-z0-9_]{0,63})."))
            continue
        if PERSONAL_IN_KEY_RE.search(key):
            errors[key] = (KEY_INVALID, f"{key!r} looks like personal data in a key name.")
            continue
        if ns == SYSTEM:
            errors[key] = (KEY_PROTECTED, f"{key!r} is a system key, written by golden code only.")
            continue
        definition = None
        if ns == CUSTOM:
            if key in RETIRED_KEYS.get(object_name, ()):
                errors[key] = (KEY_PROMOTED, f"{key!r} was promoted to a column; write the column.")
                continue
            column = PROMOTED_KEYS.get(object_name, {}).get(key)
            if column is not None:
                warnings.warn(
                    f"{key!r} was promoted to {object_name}.{column}; write the column instead.",
                    DeprecationWarning, stacklevel=3,
                )
                plan.routed[column] = value
                plan.routed_from[column] = key
                plan.deprecated.append(key)
                continue
            definition = definition_for(object_name, key, company_id) \
                if object_name in DEFINABLE_OBJECTS else None
            if definition is None and value is None:
                plan.remove.append(key)  # a removed definition's leftover may be cleared
                continue
            if definition is None:
                errors[key] = (KEY_UNDEFINED, (
                    f"{key!r} has no field definition for this {object_name} "
                    "(GOLDEN_FIELD_DEFINITIONS)."))
                continue
        else:  # provider
            if not is_provider_source(source):
                errors[key] = (KEY_PROVIDER, (
                    f"{key!r} is provider provenance, written by imports and plugins only."))
                continue
            if object_name in DEFINABLE_OBJECTS:
                definition = definition_for(object_name, key, company_id)
        if value is None:
            plan.remove.append(key)
            continue
        if definition is not None:
            try:
                value = coerce(definition, value)
            except ValueError as exc:
                errors[key] = (VALUE_INVALID, f"{definition.label}: {exc}.")
                continue
            if value is None:
                plan.remove.append(key)
                continue
        problem = check_value(value)
        if problem:
            errors[key] = (problem[0], f"{key!r}: {problem[1]}.")
            continue
        plan.set[key] = list(value) if isinstance(value, tuple) else value
    if errors:
        raise SidecarError(errors)
    return plan


def _merged(current, plan: Plan) -> dict:
    result = dict(current) if isinstance(current, dict) else {}
    for key in plan.remove:
        result.pop(key, None)
    result.update(plan.set)
    return result


def _check_size(before: dict, after: dict) -> None:
    """3.2 limits. An object already over a limit may shrink, never grow."""
    grew_keys = len(after) > len(before)
    if grew_keys and len(after) > MAX_KEYS:
        raise SidecarError({"__all__": (TOO_LARGE, f"A sidecar holds at most {MAX_KEYS} keys.")})
    size = size_of(after)
    if size > MAX_BYTES and size > size_of(before):
        raise SidecarError({"__all__": (TOO_LARGE, f"The sidecar would be {size} bytes; the limit is {MAX_BYTES}.")})


def validate_fields(obj, values: dict, *, source: str, company_id=None) -> Plan:
    """``plan_fields`` plus the size limits against ``obj.metadata``."""
    plan = plan_fields(obj, values, source=source, company_id=company_id)
    current = obj.metadata if isinstance(obj.metadata, dict) else {}
    _check_size(current, _merged(current, plan))
    return plan


def _profile(by):
    if by is None:
        return None
    from core.models import UserProfile

    if isinstance(by, UserProfile):
        return by
    return getattr(by, "userprofile", None)


def _audit(obj, object_name, plan: Plan, source, by, company_id) -> None:
    from core.models import AuditLog

    shown, hidden = {}, []
    for key, value in plan.set.items():
        definition = definition_for(object_name, key, company_id) if object_name in DEFINABLE_OBJECTS else None
        # Values shown only under a definition that says they are not
        # personal; a provider key without one may hold anything.
        if definition is None or definition.pii:
            hidden.append(key)
        else:
            shown[key] = value
    changes = {"source": str(source)[:64], "set": shown}
    if hidden:
        changes["set_pii"] = sorted(hidden)  # values omitted (3.3): pii or undefined
    if plan.remove:
        changes["removed"] = sorted(plan.remove)
    if plan.routed:
        changes["routed"] = sorted(plan.routed)  # column names only
    AuditLog.objects.create(
        user=_profile(by), action="update_fields", model_name=obj._meta.label,
        object_id=obj.pk, changes=changes,
    )


def set_fields(obj, values: dict, *, source: str, by=None) -> Plan:
    """Merge ``values`` into ``obj.metadata`` and save (3.3).

    ``custom.*`` keys are validated and coerced against their definition for
    the object's company; provider keys need an import or plugin ``source``;
    system keys are refused (golden code writes them directly); ``None``
    removes a key. The merge happens on the locked row, so a concurrent
    write of other keys (a sync's system key, an import) is never lost.
    Raises ``SidecarError``; on success writes one ``AuditLog`` row (pii
    definition values omitted) and returns the plan. ``obj`` must be saved.
    """
    if obj.pk is None or obj._state.adding:
        raise ValueError("set_fields needs a saved object.")
    object_name = object_name_for(obj)
    company_id = _company_id(obj)
    plan = plan_fields(obj, values, source=source, company_id=company_id)
    if plan.empty:
        return plan
    model = type(obj)
    with transaction.atomic(using=router.db_for_write(model)):
        locked = model._base_manager.select_for_update().only("pk", "metadata").get(pk=obj.pk)
        current = locked.metadata if isinstance(locked.metadata, dict) else {}
        merged = _merged(current, plan)
        _check_size(current, merged)
        changed = Plan(
            set={k: v for k, v in plan.set.items() if current.get(k, _MISSING) != v},
            remove=[k for k in plan.remove if k in current],
            routed=dict(plan.routed), routed_from=dict(plan.routed_from),
            deprecated=list(plan.deprecated),
        )
        if not (changed.set or changed.remove) and not changed.routed:
            obj.metadata = current
            return changed
        if changed.routed:
            # A promoted column goes through the model (its clean, tenancy
            # and save guards), never a raw update. Validated before anything
            # is written; a refusal leaves the row unchanged.
            _apply_routed(obj, changed)
        stamp = ["updated_at"] if any(f.name == "updated_at" for f in model._meta.concrete_fields) else []
        if changed.set or changed.remove:
            update = {"metadata": merged}
            if stamp:
                update["updated_at"] = timezone.now()
            model._base_manager.filter(pk=obj.pk).update(**update)
            for name, value in update.items():
                setattr(obj, name, value)
        obj.metadata = merged
        if changed.routed:
            obj.save(update_fields=[*changed.routed, *stamp])
        _audit(obj, object_name, changed, source, by, company_id)
    return changed


_MISSING = object()


def _apply_routed(obj, plan: Plan) -> None:
    """Set each promoted column on ``obj`` and run the model's validation;
    ``SidecarError`` (``metadata_value_invalid``, under the old key) on a
    refusal. A foreign key takes the target's id; ``None`` clears a column
    (a blank string for a non-null text column)."""
    from django.core.exceptions import FieldDoesNotExist

    model = type(obj)
    errors = {}
    for column, value in plan.routed.items():
        key = plan.routed_from.get(column, column)
        try:
            model_field = model._meta.get_field(column)
        except FieldDoesNotExist:
            errors[key] = (VALUE_INVALID, f"{key!r} routes to {column!r}, which does not exist.")
            continue
        if value is None and not model_field.null:
            value = model_field.get_default()
        if isinstance(value, (dict, list, float)):
            errors[key] = (VALUE_INVALID, f"{key!r}: one scalar value for {column}.")
            continue
        try:
            setattr(obj, model_field.attname, model_field.to_python(value))
        except ValidationError as exc:
            errors[key] = (VALUE_INVALID, f"{key!r}: {' '.join(exc.messages)}")
    if not errors:
        names = {model._meta.get_field(c).name for c in plan.routed}
        try:
            obj.full_clean(exclude=[f.name for f in model._meta.concrete_fields if f.name not in names],
                           validate_unique=False)
        except ValidationError as exc:
            detail = getattr(exc, "message_dict", {"__all__": exc.messages})
            for column in plan.routed:
                messages = detail.get(column) or detail.get(model._meta.get_field(column).name)
                if messages:
                    key = plan.routed_from.get(column, column)
                    errors[key] = (VALUE_INVALID, f"{key!r}: {' '.join(messages)}")
            if not errors:
                errors["__all__"] = (VALUE_INVALID, " ".join(exc.messages))
    if errors:
        raise SidecarError(errors)


def strip(obj, keys, *, save: bool = True) -> list[str]:
    """Remove ``keys`` (any namespace) from ``obj.metadata``; returns the keys
    removed. Used by erasure and promotion, which write their own audit."""
    keys = list(keys)
    current = obj.metadata if isinstance(obj.metadata, dict) else {}
    if not save or obj.pk is None or obj._state.adding:
        removed = [k for k in keys if k in current]
        obj.metadata = {k: v for k, v in current.items() if k not in keys}
        return removed
    model = type(obj)
    with transaction.atomic(using=router.db_for_write(model)):
        locked = model._base_manager.select_for_update().only("pk", "metadata").get(pk=obj.pk)
        current = locked.metadata if isinstance(locked.metadata, dict) else {}
        removed = [k for k in keys if k in current]
        new = {k: v for k, v in current.items() if k not in keys}
        if removed:
            model._base_manager.filter(pk=obj.pk).update(metadata=new)
        obj.metadata = new
    return removed


# --- reads ------------------------------------------------------------------------

_LOGGED: set[tuple] = set()


def read(obj, key: str):
    """The typed value of ``key`` (``Decimal``, ``date``, ``datetime``, list…)
    per its definition, else the raw JSON value; None when absent. A stored
    value that no longer validates is returned raw and logged once (7.1)."""
    metadata = obj.metadata if isinstance(obj.metadata, dict) else {}
    if key not in metadata:
        return None
    value = metadata[key]
    try:
        object_name = object_name_for(obj)
    except TypeError:
        return value
    if object_name not in DEFINABLE_OBJECTS or namespace(key) not in (CUSTOM, PROVIDER):
        return value
    definition = definition_for(object_name, key, _company_id(obj))
    if definition is None:
        return value
    try:
        return decode(definition, value)
    except ValueError:
        marker = (object_name, key, str(obj.pk))
        if marker not in _LOGGED:
            _LOGGED.add(marker)
            logger.warning("sidecar: %s %s %s holds a value its definition refuses; read raw",
                           object_name, obj.pk, key)
        return value


def fields_for(obj, *, group: str | None = None) -> list[tuple]:
    """Ordered ``(definition, value)`` pairs for rendering: every definition
    for the object's company (value None when absent), in definition order."""
    object_name = object_name_for(obj)
    if object_name not in DEFINABLE_OBJECTS:
        return []
    return [
        (definition, read(obj, definition.key))
        for definition in definitions(object_name, _company_id(obj))
        if group is None or definition.group == group
    ]


def custom_values(obj) -> dict:
    """The defined ``custom.*`` keys present on ``obj``, canonical JSON values
    (the API's ``custom_fields``)."""
    object_name = object_name_for(obj)
    metadata = obj.metadata if isinstance(obj.metadata, dict) else {}
    if object_name not in DEFINABLE_OBJECTS:
        return {}
    return {
        d.key: metadata[d.key]
        for d in definitions(object_name, _company_id(obj))
        if namespace(d.key) == CUSTOM and d.key in metadata
    }


# --- filtering (3.4) --------------------------------------------------------------


def filter_q(model, key: str, value) -> Q:
    """An equality ``Q`` on a sidecar key (3.4). ``multi_choice`` keys match
    "contains this option". Ranges, sorting and aggregation are not offered:
    needing one is the promotion trigger.

    Postgres compiles to ``metadata @> {key: value}`` (the GIN index); other
    vendors to a JSON key lookup. Both compare the canonical encoding, so a
    typed value (a ``Decimal``, a ``date``) is coerced through the definition
    first.
    """
    ns = namespace(key)
    if ns not in (CUSTOM, PROVIDER):
        raise ValueError(f"{key!r} is not a filterable sidecar key (custom.* or a provider key).")
    if value is None or isinstance(value, (dict, list, tuple, float)):
        raise ValueError("A sidecar filter compares one scalar value.")
    object_name = object_name_for(model)
    definition = definition_for(object_name, key, any_company=True) \
        if object_name in DEFINABLE_OBJECTS else None
    if definition is not None and not definition.filterable:
        raise ValueError(f"{key} is not filterable (its definition says filterable: false).")
    multi = definition is not None and definition.type == "multi_choice"
    if definition is not None and not multi:
        try:
            value = coerce(definition, value)
        except ValueError:
            # Generic: the definition may be another tenant's.
            raise ValueError(f"Not a valid value for {key}.") from None
        if value is None:
            raise ValueError("A sidecar filter compares one non-blank value.")
    elif multi and (not isinstance(value, str) or value not in definition.choice_values):
        raise ValueError(f"Not a valid value for {key}.")
    vendor = connections[router.db_for_read(model)].vendor
    if vendor == "postgresql":
        return Q(metadata__contains={key: [value] if multi else value})
    if multi:
        # The stored JSON text of the list; choice values are [a-z0-9_], so the
        # quoted value cannot match inside another one.
        # (SQLite has no JSON containment; ``icontains`` compares the text,
        # and choice values are lower-case.)
        return Q(**{f"metadata__{key}__icontains": json.dumps(value)})
    # Django's SQLite key lookup keeps JSON types apart (true is not 1, 1 is not
    # "1"), like Postgres's @>: core/tests/test_sidecar_vendors.py pins both
    # vendors to one table of expected matches, undefined provider keys included.
    return Q(**{f"metadata__{key}": value})


__all__ = [
    "CUSTOM", "DEFINABLE_OBJECTS", "DOTTED_KEY_RE", "MAX_BYTES", "MAX_KEYS", "MAX_LIST",
    "MAX_STRING", "PROMOTED_KEYS", "PROVIDER", "RETIRED_KEYS", "SIDECAR_MODELS", "SYSTEM",
    "SYSTEM_KEYS", "Plan", "SidecarError", "all_system_keys", "check_value", "coerce",
    "custom_values", "decode", "fields_for", "filter_q", "is_system_key", "namespace",
    "object_name_for", "plan_fields", "read", "set_fields", "strip", "validate_fields",
]
