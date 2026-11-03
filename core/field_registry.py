"""Field definitions: ``settings.GOLDEN_FIELD_DEFINITIONS`` (phase4-fields.md 4.1).

A deployment describes the sidecar keys it uses on Party, Engagement and
Task, so they validate, render, filter and export like columns without a
golden migration::

    GOLDEN_FIELD_DEFINITIONS = {
        "engagement": [
            {"key": "custom.package", "label": "Package", "type": "choice",
             "choices": [["accelerator", "Accelerator"], ["other", "Other"]],
             "group": "Qualification", "list": True, "filterable": True},
        ],
        "party": [
            {"key": "custom.job_title", "label": "Job title", "type": "string",
             "group": "Profile", "pii": True, "companies": ["<company uuid>"]},
        ],
    }

Same loader shape as ``core/label_registry.py``: a frozen dataclass per entry,
``ImproperlyConfigured`` at import on any bad entry, a rebuild on
``setting_changed``. Importing this module performs no database query, so an
unknown company id is a system check (``core.W113``, ``core/checks.py``), not
an import error.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

from .sidecar import (
    CUSTOM,
    DEFINABLE_OBJECTS,
    PERSONAL_IN_KEY_RE,
    PROMOTED_KEYS,
    RESERVED_NAMESPACES,
    RETIRED_KEYS,
    namespace,
)

SETTING = "GOLDEN_FIELD_DEFINITIONS"
TYPES = ("string", "text", "integer", "decimal", "date", "datetime", "boolean", "choice",
         "multi_choice", "url")
CHOICE_TYPES = ("choice", "multi_choice")
# Ranges are not equality (3.4): these types cannot be filterable.
UNFILTERABLE_TYPES = ("text", "decimal", "date", "datetime")
MAX_LABEL = 60
MAX_HELP = 500
# Choice values follow the registry key rule (parties/_registry.py).
CHOICE_VALUE_RE = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")


@dataclass(frozen=True)
class FieldDefinition:
    object: str                         # "party" | "engagement" | "task"
    key: str                            # custom.* or a provider key (display-only)
    label: str
    type: str
    choices: tuple[tuple[str, str], ...] = ()
    group: str = "Details"
    list: bool = False                  # offered in the column chooser
    list_default: bool = False          # shown by default
    filterable: bool = False            # gets an equality list filter
    export: bool = True
    pii: bool = False                   # erasure scrubs it; audit omits its values
    required: bool = False              # staff forms only, never imports
    companies: frozenset[str] | None = None  # None = every tenant
    help: str = ""

    @property
    def choice_values(self) -> tuple[str, ...]:
        return tuple(value for value, _ in self.choices)

    def choice_label(self, value) -> str:
        return dict(self.choices).get(value, str(value))

    @property
    def is_custom(self) -> bool:
        return namespace(self.key) == CUSTOM

    @property
    def read_only(self) -> bool:
        """A provider key's definition is display-only: forms never write it."""
        return not self.is_custom

    def applies_to(self, company_id) -> bool:
        return self.companies is None or (company_id is not None and str(company_id) in self.companies)


ATTRIBUTES = tuple(f for f in FieldDefinition.__dataclass_fields__ if f != "object")
_BOOLS = ("list", "list_default", "filterable", "export", "pii", "required")


def _fail(where: str, message: str):
    raise ImproperlyConfigured(f"{SETTING}{where} {message}")


def _text(where, name, value, *, max_length, required=True) -> str:
    if not isinstance(value, str):
        _fail(where, f"{name} must be a string, got {type(value).__name__}.")
    value = value.strip()
    if required and not value:
        _fail(where, f"has an empty {name}.")
    if len(value) > max_length:
        _fail(where, f"{name} is longer than {max_length} characters.")
    return value


def _key(where, object_name, key) -> str:
    if not isinstance(key, str):
        _fail(where, f"key must be a string, got {type(key).__name__}.")
    ns = namespace(key)
    if ns is None or ns == "system" or key.split(".", 1)[0] in RESERVED_NAMESPACES:
        _fail(where, (
            f"has key={key!r}; a definition names a 'custom.<name>' or '<provider>.<name>' key "
            "([a-z][a-z0-9_]{0,31} . [a-z0-9][a-z0-9_]{0,63}); un-dotted keys are golden's own."
        ))
    if PERSONAL_IN_KEY_RE.search(key):
        _fail(where, f"has key={key!r}, which looks like personal data ('@' or 7+ digits).")
    if key in PROMOTED_KEYS.get(object_name, {}) or key in RETIRED_KEYS.get(object_name, ()):
        _fail(where, f"has key={key!r}, which was promoted to a column (core.sidecar.PROMOTED_KEYS).")
    return key


def _choices(where, kind, value) -> tuple[tuple[str, str], ...]:
    if kind not in CHOICE_TYPES:
        if value:
            _fail(where, f"has choices, but type={kind!r} takes none (choice or multi_choice).")
        return ()
    if not isinstance(value, (list, tuple)) or not value:
        _fail(where, f"type={kind!r} needs a non-empty choices list of [value, label] pairs.")
    out, seen = [], set()
    for i, pair in enumerate(value):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            _fail(where, f"choices[{i}] must be a [value, label] pair.")
        choice, label = pair
        if not isinstance(choice, str) or not CHOICE_VALUE_RE.fullmatch(choice):
            _fail(where, (f"choices[{i}] has value={choice!r}; use a lower-case key "
                          "([a-z][a-z0-9_]*, at most 32 characters)."))
        if choice in seen:
            _fail(where, f"lists the choice {choice!r} twice.")
        seen.add(choice)
        out.append((choice, _text(f"{where} choices[{i}]", "label", label, max_length=MAX_LABEL)))
    return tuple(out)


def _companies(where, value) -> frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)) or not value:
        _fail(where, "companies must be None or a non-empty list of Company ids (UUIDs).")
    out = set()
    for company_id in value:
        try:
            out.add(str(uuid.UUID(str(company_id))))
        except ValueError:
            _fail(where, f"companies lists {company_id!r}, which is not a Company id (UUID).")
    return frozenset(out)


def definition_from_config(object_name: str, index: int, entry: Any) -> FieldDefinition:
    where = f"[{object_name!r}][{index}]"
    if isinstance(entry, FieldDefinition):
        entry = {name: getattr(entry, name) for name in ATTRIBUTES}
    if not isinstance(entry, dict):
        _fail(where, f"must be a dict of field definition attributes, got {type(entry).__name__}.")
    unknown = sorted(str(k) for k in set(entry) - set(ATTRIBUTES))
    if unknown:
        _fail(where, f"has unknown attribute(s) {', '.join(map(repr, unknown))}; "
                     f"allowed: {', '.join(ATTRIBUTES)}.")
    for required in ("key", "label", "type"):
        if required not in entry:
            _fail(where, f"is missing {required}.")
    key = _key(where, object_name, entry["key"])
    where = f"{where} ({key})"
    kind = entry["type"]
    if kind not in TYPES:
        _fail(where, f"has type={kind!r}; must be one of {', '.join(TYPES)}.")
    flags = {}
    for name in _BOOLS:
        value = entry.get(name, FieldDefinition.__dataclass_fields__[name].default)
        if not isinstance(value, bool):
            _fail(where, f"{name} must be true or false, got {value!r}.")
        flags[name] = value
    if flags["filterable"] and kind in UNFILTERABLE_TYPES:
        _fail(where, (f"is filterable with type={kind!r}; sidecar filters are equality only "
                      f"(3.4), so {', '.join(UNFILTERABLE_TYPES)} cannot filter. Promote the field."))
    if flags["list_default"] and not flags["list"]:
        _fail(where, "has list_default without list.")
    if flags["list_default"] and flags["pii"]:
        _fail(where, "is pii and list_default; no list has a permission gate for pii columns yet.")
    return FieldDefinition(
        object=object_name, key=key,
        label=_text(where, "label", entry["label"], max_length=MAX_LABEL),
        type=kind,
        choices=_choices(where, kind, entry.get("choices", ())),
        group=_text(where, "group", entry.get("group", "Details"), max_length=MAX_LABEL),
        companies=_companies(where, entry.get("companies")),
        help=_text(where, "help", entry.get("help", ""), max_length=MAX_HELP, required=False),
        **flags,
    )


def build_definitions() -> dict[str, tuple[FieldDefinition, ...]]:
    """``settings.GOLDEN_FIELD_DEFINITIONS``, validated, keyed by object."""
    configured = getattr(settings, SETTING, None) or {}
    if not isinstance(configured, dict):
        raise ImproperlyConfigured(
            f"{SETTING} must be a dict of object -> list of definitions, "
            f"got {type(configured).__name__}."
        )
    out: dict[str, tuple[FieldDefinition, ...]] = {name: () for name in DEFINABLE_OBJECTS}
    for object_name, entries in configured.items():
        if object_name not in DEFINABLE_OBJECTS:
            raise ImproperlyConfigured(
                f"{SETTING} names an unknown object {object_name!r}; "
                f"field definitions apply to {', '.join(DEFINABLE_OBJECTS)}."
            )
        if not isinstance(entries, (list, tuple)):
            raise ImproperlyConfigured(f"{SETTING}[{object_name!r}] must be a list of dicts.")
        seen: set[str] = set()
        built = []
        for i, entry in enumerate(entries):
            definition = definition_from_config(object_name, i, entry)
            if definition.key in seen:
                raise ImproperlyConfigured(
                    f"{SETTING}[{object_name!r}] defines {definition.key!r} twice; one key, "
                    "one definition (scope it with companies)."
                )
            seen.add(definition.key)
            built.append(definition)
        out[object_name] = tuple(built)
    return out


_DEFINITIONS: dict[str, tuple[FieldDefinition, ...]] = build_definitions()
DEFINITIONS = MappingProxyType(_DEFINITIONS)


@receiver(setting_changed)
def _reload_registry(sender, setting, **kwargs) -> None:
    if setting != SETTING:
        return
    rebuilt = build_definitions()
    _DEFINITIONS.clear()
    _DEFINITIONS.update(rebuilt)


def definitions_for(object_name: str, company_id=None) -> tuple[FieldDefinition, ...]:
    """Definitions for ``object_name`` that apply to ``company_id``, in order.

    ``company_id=None`` (no tenant known) sees only unscoped definitions.
    """
    return tuple(d for d in DEFINITIONS.get(object_name, ()) if d.applies_to(company_id))


def get_definition(object_name: str, key: str, company_id=None, *, any_company: bool = False):
    """The definition of ``key`` on ``object_name`` for ``company_id``, or None.

    ``any_company=True`` ignores ``companies`` (a key has one definition per
    object, so its type is the same for every tenant).
    """
    for definition in DEFINITIONS.get(object_name, ()):
        if definition.key == key and (any_company or definition.applies_to(company_id)):
            return definition
    return None


def all_company_ids() -> frozenset[str]:
    """Every Company id named by a definition's ``companies``."""
    return frozenset(
        company_id
        for defs in DEFINITIONS.values() for d in defs
        for company_id in (d.companies or ())
    )


__all__ = [
    "DEFINITIONS", "FieldDefinition", "TYPES", "all_company_ids", "build_definitions",
    "definition_from_config", "definitions_for", "get_definition",
]
