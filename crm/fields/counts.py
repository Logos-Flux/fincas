"""Fill an inventory's ``populated``, ``total`` and ``options`` from local
export files (phase4-fields.md 1.2, ``manage.py field_inventory``).

Inputs, both produced outside golden (it never calls a provider API):

- a **property definitions** dump: a JSON list, a JSON object with
  ``results`` (the shape of HubSpot's properties endpoint), or JSONL, one
  definition per line, either flat or in the Phase 5 canonical envelope
  (``property_defs.jsonl``: the definition under ``data``). Each definition
  has ``name``, ``label``, ``type`` and optionally ``options``
  (``[{value, label}]``) and ``object_type``;
- a **records export** CSV whose headers are property names or labels.

Only counts and enumeration option values leave the records file: free-text
values are never written to the inventory.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from .inventory import COLUMNS

ENUM_TYPES = frozenset({"enumeration", "enum", "choice", "select", "multi_select",
                        "multiselect", "bool", "boolean", "booleancheckbox"})
MULTI_SEPARATOR = ";"  # HubSpot's multi-checkbox values


def load_definitions(path: str | Path, *, object_type: str) -> list[dict]:
    """Normalised definitions (``name``, ``label``, ``type``, ``options``) for
    ``object_type``. A definition that names another object is skipped; one
    that names none is taken to belong to ``object_type``."""
    text = Path(path).read_text(encoding="utf-8-sig").strip()
    if not text:
        return []
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:  # JSONL: one definition per line
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        if isinstance(loaded, dict):
            records = loaded["results"] if isinstance(loaded.get("results"), list) else [loaded]
        else:
            records = loaded

    out = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"a definition is not an object: {record!r}")
        data = record.get("data") if isinstance(record.get("data"), dict) else record
        owner = data.get("object_type")
        if owner and owner != object_type:
            continue
        name = data.get("name")
        if not name:
            raise ValueError(f"a definition has no name: {record!r}")
        options = [str(o.get("value", "")) if isinstance(o, dict) else str(o)
                   for o in data.get("options") or ()]
        out.append({
            "name": str(name),
            "label": str(data.get("label") or ""),
            "type": str(data.get("type") or ""),
            "options": options,
        })
    return out


def count_records(path: str | Path, definitions: list[dict]) -> tuple[int, dict[str, dict]]:
    """``(total, {name: {"populated": n, "options": Counter}})``. A column is
    matched to a definition by name first, then by label."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        by_name = {d["name"]: d for d in definitions}
        by_label = {d["label"]: d for d in definitions if d["label"]}
        columns: dict[int, dict] = {}
        for index, title in enumerate(h.strip() for h in header):
            definition = by_name.get(title) or by_label.get(title)
            if definition and definition["name"] not in {d["name"] for d in columns.values()}:
                columns[index] = definition
        stats = {d["name"]: {"populated": 0, "options": Counter()} for d in definitions}
        total = 0
        for cells in reader:
            if not any(c.strip() for c in cells):
                continue
            total += 1
            for index, definition in columns.items():
                value = cells[index].strip() if index < len(cells) else ""
                if not value:
                    continue
                entry = stats[definition["name"]]
                entry["populated"] += 1
                if _is_enum(definition):
                    for part in value.split(MULTI_SEPARATOR):
                        if part.strip():
                            entry["options"][part.strip()] += 1
    matched = {d["name"] for d in columns.values()}
    for name in list(stats):
        if name not in matched:
            stats[name]["populated"] = None  # the export has no such column
    return total, stats


def _is_enum(definition: dict) -> bool:
    return definition["type"].lower() in ENUM_TYPES or bool(definition["options"])


def format_options(definition: dict, counted: Counter) -> str:
    """``value=count|…``: defined options in definition order (zero counts
    kept, so an unused option is visible), then values the data has that the
    definition does not, by count. Empty when the definition lists no
    options: the data's values are then never copied out."""
    if not definition["options"]:
        # No option list (owner pickers, externally sourced options): the
        # values could be people's names, so none are written.
        return ""
    parts = [f"{value}={counted.get(value, 0)}" for value in definition["options"]]
    extra = sorted((v for v in counted if v not in definition["options"]),
                   key=lambda v: (-counted[v], v))
    parts += [f"{value}={counted[value]}" for value in extra]
    return "|".join(p.replace("|", "/") for p in parts)


def fill(rows: list[dict], *, provider: str, object_type: str,
         definitions: list[dict], total: int, stats: dict[str, dict]) -> list[dict]:
    """Return ``rows`` (an existing inventory, possibly empty) with the counts
    of ``provider``/``object_type`` rows refreshed and a new row appended for
    each defined property the inventory lacks. Person-filled columns (usage,
    evidence, pii, decision, target, notes) are never touched; a new row has
    usage ``unknown`` and no decision, so the validator flags it until a
    person decides."""
    out = [dict(r) for r in rows]
    index = {(r["provider"], r["object"], r["source_id"]): r for r in out}
    for definition in definitions:
        key = (provider, object_type, definition["name"])
        row = index.get(key)
        if row is None:
            row = dict.fromkeys(COLUMNS, "")
            row.update(provider=provider, object=object_type, source_id=definition["name"],
                       filtered="unknown", sorted="unknown", reported="unknown")
            out.append(row)
            index[key] = row
        if definition["label"]:
            row["source_label"] = definition["label"]
        if definition["type"]:
            row["source_type"] = definition["type"]
        entry = stats.get(definition["name"])
        if entry is None or entry["populated"] is None:
            continue  # not in this export: leave the counts as they were
        row["populated"] = str(entry["populated"])
        row["total"] = str(total)
        if _is_enum(definition):
            row["options"] = format_options(definition, entry["options"])
    return out


def write(rows: list[dict], handle) -> None:
    extra = [c for r in rows for c in r if c not in COLUMNS]
    fieldnames = list(COLUMNS) + list(dict.fromkeys(extra))
    writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in fieldnames})
