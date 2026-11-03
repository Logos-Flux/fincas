"""The field inventory format and its validator (phase4-fields.md 1.2-1.4).

An inventory is one CSV per provider: one row per provider property, with
usage counts, how it is used, and golden's decision for it. Golden ships the
format, this validator and each provider's *standard* properties
(``docs/crm/fields/<provider>-standard.csv``); a deployment's real inventory,
with its custom properties and counts, lives in the deployment's own repo
(F1). ``manage.py check_field_inventory`` runs :func:`validate` on either.

The module is pure apart from :func:`resolve_model`, which looks a target's
model up in the app registry; pass ``check_models=False`` to skip that.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

COLUMNS = (
    "provider", "object", "source_id", "source_label", "source_type", "options",
    "populated", "total", "filtered", "sorted", "reported", "evidence", "pii",
    "decision", "target", "notes",
)
DECISIONS = ("map", "promote", "sidecar", "drop", "defer")
USAGE_COLUMNS = ("filtered", "sorted", "reported")
USAGE_VALUES = ("yes", "no", "unknown")
YES_NO = ("yes", "no")

# Sidecar keys (3.1). A dotted key is ``<namespace>.<name>``; ``custom`` is
# the canonical namespace (needs a FieldDefinition), any other namespace is a
# provider's (provenance, written by imports only).
NAMESPACE_RE = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")
DOTTED_KEY_RE = re.compile(r"\A[a-z][a-z0-9_]{0,31}\.[a-z0-9][a-z0-9_]{0,63}\Z")
CUSTOM_NAMESPACE = "custom"
OBJECT_RE = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
# Personal data in a key name: an address or a phone-like run of digits.
PERSONAL_IN_KEY_RE = re.compile(r"@|\d{7,}")

# ``map`` / ``promote`` targets name golden's own fields:
#   Party.primary_email          a model field
#   PartyPhone(line_type=mobile) a model, qualified
#   ConsentRecord(withdrawn) + Suppression   several, joined by " + "
_REF_RE = re.compile(
    r"\A(?P<model>[A-Z][A-Za-z0-9]*)"
    r"(?:\.(?P<field>[a-z_][a-z0-9_]*))?"
    r"(?:\((?P<qualifier>[a-z0-9_]+(?:=[a-z0-9_]+)?(?:,\s*[a-z0-9_]+(?:=[a-z0-9_]+)?)*)\))?\Z"
)
_PROMOTE_RE = re.compile(r"\A(?P<model>[A-Z][A-Za-z0-9]*)\.(?P<field>[a-z][a-z0-9_]*)\Z")
# ``defer`` names the phase that owns the property: "Phase 5", "Phase 4b: …".
_DEFER_RE = re.compile(r"\APhase \d+[a-z]?(?:\Z|[\s:,;(-])")


@dataclass(frozen=True)
class InventoryError:
    line: int  # the CSV line (1 = the header)
    code: str
    message: str
    column: str = ""

    def __str__(self) -> str:
        where = f"line {self.line}" + (f", {self.column}" if self.column else "")
        return f"{where}: [{self.code}] {self.message}"


@dataclass
class Inventory:
    rows: list[dict] = field(default_factory=list)
    errors: list[InventoryError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def provider_key(provider: str, source_id: str) -> str:
    """The default provenance key for a provider property (3.1): the
    ``source_id`` lower-cased, every character outside ``[a-z0-9_]`` mapped
    to ``_``. Lossy for some ids; the inventory's ``target`` records the key
    actually used."""
    name = re.sub(r"[^a-z0-9_]", "_", source_id.lower())
    return f"{provider}.{name}"


def resolve_model(name: str):
    """The golden model whose class name is ``name``, or None (also None when
    the name is ambiguous across apps)."""
    from django.apps import apps

    found = [m for m in apps.get_models() if m.__name__ == name]
    return found[0] if len(found) == 1 else None


def _has_field(model, name: str) -> bool:
    from django.core.exceptions import FieldDoesNotExist

    try:
        model._meta.get_field(name)
    except FieldDoesNotExist:
        return False
    return True


def _int(value: str):
    """``None`` for blank (not counted yet), an int, or ``ValueError``."""
    if value == "":
        return None
    if not value.isdigit():
        raise ValueError(value)
    return int(value)


def validate_text(text: str, *, check_models: bool = True) -> Inventory:
    """Validate an inventory given as CSV text."""
    return validate_rows(csv.reader(io.StringIO(text.lstrip("\ufeff"))),
                         check_models=check_models)


def validate_file(path: str | Path, *, check_models: bool = True) -> Inventory:
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            return validate_rows(csv.reader(handle), check_models=check_models)
    except (UnicodeDecodeError, csv.Error) as exc:
        return Inventory(errors=[InventoryError(
            1, "bad_encoding", f"not a UTF-8 CSV ({exc}); re-save it as CSV UTF-8",
        )])


def validate_rows(reader: Iterable[list[str]], *, check_models: bool = True) -> Inventory:
    """Parse and validate a CSV inventory; every refusal is collected, so one
    run reports them all. Rows come back as dicts with whitespace trimmed."""
    result = Inventory()
    lines = iter(reader)
    header = next(lines, None)
    if not header:
        result.errors.append(InventoryError(1, "missing_column", "the file has no header row"))
        return result
    header = [h.strip() for h in header]
    missing = [c for c in COLUMNS if c not in header]
    if missing:
        result.errors.append(InventoryError(
            1, "missing_column", f"missing column(s): {', '.join(missing)}",
        ))
        return result
    dupes = sorted({h for h in header if header.count(h) > 1})
    if dupes:
        result.errors.append(InventoryError(
            1, "duplicate_column", f"column(s) appear twice: {', '.join(dupes)}",
        ))
        return result

    seen: dict[tuple[str, str, str], int] = {}
    claimed: dict[tuple[str, str, str], int] = {}
    for line_no, cells in enumerate(lines, start=2):
        if not any(c.strip() for c in cells):
            continue  # a blank line
        if len(cells) > len(header):
            result.errors.append(InventoryError(
                line_no, "bad_row",
                f"{len(cells)} cells for {len(header)} columns (an unquoted comma?)",
            ))
            continue
        row = {name: (cells[i].strip() if i < len(cells) else "") for i, name in enumerate(header)}
        errors = _check_row(row, line_no, check_models=check_models)
        identity = (row["provider"], row["object"], row["source_id"])
        duplicate = False
        if all(identity):
            if identity in seen:
                duplicate = True
                errors.append(InventoryError(
                    line_no, "duplicate_row",
                    f"{'/'.join(identity)} is already on line {seen[identity]}",
                ))
            else:
                seen[identity] = line_no
        if row["decision"] == "sidecar" and row["target"] and not duplicate:
            claim = (row["provider"], row["object"], row["target"])
            if claim in claimed:
                errors.append(InventoryError(
                    line_no, "duplicate_target",
                    f"{row['target']} is already the target of line {claimed[claim]} "
                    "(two properties would overwrite each other)", "target",
                ))
            else:
                claimed[claim] = line_no
        result.errors.extend(errors)
        result.rows.append(row)
    return result


def _check_row(row: dict, line: int, *, check_models: bool) -> list[InventoryError]:
    errors: list[InventoryError] = []

    def refuse(code, column, message):
        errors.append(InventoryError(line, code, message, column))

    provider = row["provider"]
    if not NAMESPACE_RE.match(provider) or provider == CUSTOM_NAMESPACE:
        refuse("bad_provider", "provider",
               f"{provider!r} is not a provider namespace ([a-z][a-z0-9_]{{0,31}}, not 'custom')")
    if not OBJECT_RE.match(row["object"]):
        refuse("bad_object", "object", f"{row['object']!r} is not an object name ([a-z][a-z0-9_]*)")
    if not row["source_id"]:
        refuse("missing_source_id", "source_id", "source_id is required (the stable property id)")

    counts = {}
    for column in ("populated", "total"):
        try:
            counts[column] = _int(row[column])
        except ValueError:
            refuse("bad_count", column, f"{row[column]!r} is not a non-negative integer")
            counts[column] = None
    if counts["populated"] is not None and counts["total"] is not None \
            and counts["populated"] > counts["total"]:
        refuse("bad_count", "populated",
               f"populated {counts['populated']} exceeds total {counts['total']}")

    for column in USAGE_COLUMNS:
        if row[column] not in USAGE_VALUES:
            refuse("bad_usage", column, f"{row[column]!r} is not one of yes / no / unknown")
    if row["pii"] not in YES_NO:
        refuse("bad_pii", "pii", f"{row['pii']!r} is not yes / no")
    if row["options"] and any(not part.strip() for part in row["options"].split("|")):
        refuse("bad_options", "options", "an empty option between '|' separators")

    decision = row["decision"]
    if decision not in DECISIONS:
        refuse("bad_decision", "decision",
               f"{decision!r} is not one of {' / '.join(DECISIONS)}")
        return errors
    target = row["target"]
    if decision == "map":
        _check_map_target(target, refuse, check_models)
    elif decision == "promote":
        _check_promote_target(target, refuse, check_models)
    elif decision == "sidecar":
        _check_sidecar_target(row, refuse)
    elif decision == "defer":
        if not _DEFER_RE.match(target):
            refuse("bad_target", "target",
                   f"a defer target names the owning phase ('Phase 5 …'), got {target!r}")
    elif decision == "drop":
        if target:
            refuse("bad_target", "target", f"a drop has no target, got {target!r}")
        if counts["populated"] and not row["notes"]:
            refuse("drop_needs_reason", "notes",
                   f"dropping a populated property ({counts['populated']} records) needs a reason in notes")
    return errors


def _check_map_target(target, refuse, check_models):
    if not target:
        refuse("bad_target", "target", "a map target names the golden field ('Party.primary_email')")
        return
    for part in (p.strip() for p in target.split(" + ")):
        match = _REF_RE.match(part)
        if not match:
            refuse("bad_target", "target",
                   f"{part!r} is not Model, Model.field or Model(qualifier)")
            continue
        if not check_models:
            continue
        model = resolve_model(match["model"])
        if model is None:
            refuse("unknown_target", "target", f"{match['model']!r} is not a golden model")
        elif match["field"] and not _has_field(model, match["field"]):
            refuse("unknown_target", "target",
                   f"{match['model']}.{match['field']} does not exist (a new column is 'promote')")


def _check_promote_target(target, refuse, check_models):
    match = _PROMOTE_RE.match(target)
    if not match:
        refuse("bad_target", "target", f"a promote target is the new column 'Model.field', got {target!r}")
        return
    if PERSONAL_IN_KEY_RE.search(target):
        refuse("personal_key", "target", "a column name must not contain personal data")
    if check_models and resolve_model(match["model"]) is None:
        refuse("unknown_target", "target", f"{match['model']!r} is not a golden model")


def _check_sidecar_target(row, refuse):
    key = row["target"]
    if PERSONAL_IN_KEY_RE.search(key):
        refuse("personal_key", "target",
               f"{key!r} looks like personal data in a key name ('@' or 7+ digits)")
        return
    if not DOTTED_KEY_RE.match(key):
        refuse("bad_sidecar_key", "target",
               f"{key!r} is not a sidecar key '<namespace>.<name>' "
               "([a-z][a-z0-9_]{0,31} . [a-z0-9][a-z0-9_]{0,63}); undotted keys are golden's own")
        return
    namespace = key.split(".", 1)[0]
    if namespace not in (CUSTOM_NAMESPACE, row["provider"]):
        refuse("bad_sidecar_key", "target",
               f"{key!r}: an import writes 'custom.*' or its own provider's keys "
               f"('{row['provider']}.*'), not '{namespace}.*'")
        return
    if namespace != CUSTOM_NAMESPACE and row["pii"] == "yes":
        refuse("pii_provider_key", "target",
               "personal data needs a 'custom.*' key with a pii FieldDefinition (so erasure "
               "finds it), a typed contact point, or drop; never a provider key")
