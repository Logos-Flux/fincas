"""``manage.py check_sidecar``: what the sidecar contract cannot refuse at write
time (phase4-fields.md 3.3).

Reports, per sidecar model (``core.sidecar.SIDECAR_MODELS``):

- ``invalid_value``: a value its FieldDefinition refuses (a definition
  changed after the data was written);
- ``undefined_custom_key``: a ``custom.*`` key with no definition for the
  row's company;
- ``provider_key_not_in_inventory``: a provider key no inventory row maps to
  (only when inventories are given: ``--inventory`` or
  ``settings.GOLDEN_FIELD_INVENTORIES``);
- ``pii_shaped_value``: an email address or phone number under a key with no
  ``pii`` definition (system keys are golden's and are not inspected);
- ``unregistered_system_key``: an un-dotted key not in
  ``core.sidecar.SYSTEM_KEYS`` for that model;
- ``invalid_key``, ``bad_value``, ``too_large``: a key that breaks the key
  rule, a value outside 3.2 (nested object, float, over a limit), a sidecar
  over 200 keys or 32 KB.

``--stats`` adds per-key counts and bytes. Read-only. Exit 0 when clean, 1 on
any finding (``--stats`` alone never fails). CI runs it on the seeded
database.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core import sidecar
from core.field_registry import get_definition

_SAMPLE = 10
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\A\+?[\d\s().-]{7,}\Z")


def looks_personal(value) -> bool:
    """An email address or a phone number, in a string or a list of them."""
    if isinstance(value, list):
        return any(looks_personal(v) for v in value)
    if not isinstance(value, str):
        return False
    if EMAIL_RE.search(value):
        return True
    return bool(PHONE_RE.fullmatch(value.strip())) and sum(c.isdigit() for c in value) >= 7


def inventory_keys(paths) -> tuple[set[str], list[str]]:
    """Sidecar keys the inventories map to (rows with decision ``sidecar``),
    plus the errors of any inventory that does not validate."""
    from crm.fields.inventory import validate_file

    keys, errors = set(), []
    for path in paths:
        inventory = validate_file(path, check_models=False)
        errors += [f"{path}: {e}" for e in inventory.errors]
        keys |= {row["target"] for row in inventory.rows if row.get("decision") == "sidecar"}
    return keys, errors


class Command(BaseCommand):
    help = "Report sidecar (metadata) values the contract refuses (exit 1 if any)."

    def add_arguments(self, parser):
        parser.add_argument("--model", action="append", choices=sorted(sidecar.SIDECAR_MODELS),
                            help="Check only this sidecar object (repeatable).")
        parser.add_argument("--stats", action="store_true", help="Also print per-key counts and bytes.")
        parser.add_argument("--inventory", action="append", default=None, metavar="CSV",
                            help="A field inventory CSV (repeatable); default "
                                 "settings.GOLDEN_FIELD_INVENTORIES.")

    def handle(self, *args, **options):
        objects = options["model"] or list(sidecar.SIDECAR_MODELS)
        paths = options["inventory"]
        if paths is None:
            paths = list(getattr(settings, "GOLDEN_FIELD_INVENTORIES", None) or [])
        known_provider_keys = None
        problems = 0
        if paths:
            known_provider_keys, errors = inventory_keys(paths)
            for error in errors:
                self.stdout.write(f"inventory: {error}")
            problems += len(errors)
        findings: dict[tuple[str, str], list[str]] = defaultdict(list)
        stats: dict[str, dict[str, Counter]] = {}
        rows = 0
        for object_name in objects:
            model = apps.get_model(sidecar.SIDECAR_MODELS[object_name])
            per_key = stats.setdefault(object_name, defaultdict(Counter))
            qs = model._base_manager.order_by("pk").values_list("pk", "company_id", "metadata")
            for pk, company_id, metadata in qs.iterator(chunk_size=2000):
                rows += 1
                for code, detail in self._row(object_name, company_id, metadata, known_provider_keys, per_key):
                    findings[(object_name, code)].append(f"{pk} {detail}")
        for (object_name, code), items in sorted(findings.items()):
            problems += len(items)
            shown = "; ".join(items[:_SAMPLE]) + ("; …" if len(items) > _SAMPLE else "")
            self.stdout.write(f"{object_name}: {code} x{len(items)}: {shown}")
        if known_provider_keys is None:
            self.stdout.write("provider keys not checked against an inventory "
                              "(no --inventory, GOLDEN_FIELD_INVENTORIES empty).")
        if options["stats"]:
            for object_name, per_key in stats.items():
                for key, counter in sorted(per_key.items()):
                    self.stdout.write(f"stats {object_name} {key}: {counter['rows']} row(s), "
                                      f"{counter['bytes']} byte(s)")
        self.stdout.write(f"{rows} sidecar(s) checked, {problems} finding(s).")
        if problems:
            raise CommandError(f"{problems} sidecar finding(s).", returncode=1)

    def _row(self, object_name, company_id, metadata, known_provider_keys, per_key):
        if not isinstance(metadata, dict):
            yield "bad_value", f"metadata is a {type(metadata).__name__}, not an object"
            return
        definable = object_name in sidecar.DEFINABLE_OBJECTS
        company = str(company_id) if company_id is not None else None
        for key, value in metadata.items():
            counter = per_key[key]
            counter["rows"] += 1
            counter["bytes"] += len(json.dumps(value, default=str).encode())
            ns = sidecar.namespace(key)
            if ns is None:
                yield "invalid_key", repr(key)
                continue
            if ns == sidecar.SYSTEM:
                if not sidecar.is_system_key(key, object_name):
                    yield "unregistered_system_key", key
                continue
            problem = sidecar.check_value(value)
            if problem:
                yield "bad_value", f"{key}: {problem[1]}"
            definition = get_definition(object_name, key, company) if definable else None
            if ns == sidecar.CUSTOM and definition is None:
                yield "undefined_custom_key", key
            if definition is not None and not problem:
                try:
                    sidecar.decode(definition, value)
                except ValueError as exc:
                    yield "invalid_value", f"{key}: {exc}"
            if ns == sidecar.PROVIDER and known_provider_keys is not None \
                    and key not in known_provider_keys:
                yield "provider_key_not_in_inventory", key
            if (definition is None or not definition.pii) and looks_personal(value):
                yield "pii_shaped_value", key
        if len(metadata) > sidecar.MAX_KEYS:
            yield "too_large", f"{len(metadata)} keys"
        size = sidecar.size_of(metadata)
        if size > sidecar.MAX_BYTES:
            yield "too_large", f"{size} bytes"
