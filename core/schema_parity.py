"""Structural schema snapshots and diffs for Postgres (Pin milestone M1).

A *snapshot* is a plain dict describing every table in one Postgres schema:
table properties (row-level security, storage options), columns (type,
nullability, default, identity, generated, non-default collation),
constraints (primary key, unique, foreign key, check, exclusion; full
definition, validity, backing-index definition), indexes that do not back a
constraint (definition and validity), non-internal triggers and RLS policies.
It is read from ``pg_catalog`` / ``pg_indexes`` / ``pg_policies``, never from
``pg_dump`` text, and every collection is keyed by name so that column order,
constraint order or catalog OID order can never produce a diff.

``diff_snapshots(baseline, actual)`` compares two snapshots. Direction is
fixed: ``added`` means present in *actual* but not in *baseline*, ``removed``
the reverse. The ``check_schema_parity`` management command is a thin wrapper
around this module; everything here is unit-testable with hand-built dicts.

Diff entry keys (also the expected-diff manifest format):
    table                      whole table added/removed, or a table property changed
    table.column               column added, removed or changed
    table.index:<name>         index added, removed, changed or renamed
    table.constraint:<name>    constraint added, removed, changed or renamed
    table.trigger:<name>       trigger added, removed, changed or renamed
    table.policy:<name>        RLS policy added, removed, changed or renamed
A manifest line may pin fields after the key (``table.column nullable``).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

SNAPSHOT_FORMAT = 2

_FK_ACTIONS = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}
_CONSTRAINT_TYPES = {
    "p": "primary_key",
    "u": "unique",
    "f": "foreign_key",
    "c": "check",
    "x": "exclude",
}
_IDENTITY = {"": None, "a": "always", "d": "by default"}
_GENERATED = {"": None, "s": "stored", "v": "virtual"}


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------

_TABLES_SQL = """
SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, c.reloptions
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition
"""

# A collation is recorded only when it differs from the type's default, so the
# common case adds nothing to the snapshot.
_COLUMNS_SQL = """
SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid), a.attidentity::text, a.attgenerated::text,
       CASE WHEN a.attcollation <> t.typcollation THEN coll.collname END
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_type t ON t.oid = a.atttypid
LEFT JOIN pg_collation coll ON coll.oid = a.attcollation
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND a.attnum > 0 AND NOT a.attisdropped
"""

# contype 'n' (catalogued NOT NULL, Postgres 18+) is deliberately excluded:
# nullability is already a column property. pg_get_constraintdef carries
# MATCH type, SET NULL (cols), NOT VALID, NULLS NOT DISTINCT and DEFERRABLE;
# the backing index definition carries storage options (WITH (fillfactor=..)).
_CONSTRAINTS_SQL = """
SELECT c.relname, con.conname, con.contype::text, pg_get_constraintdef(con.oid),
       CASE WHEN rn.nspname = n.nspname THEN ref.relname ELSE rn.nspname || '.' || ref.relname END,
       con.confdeltype::text, con.confupdtype::text, con.condeferrable, con.condeferred,
       ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord)
             JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
             ORDER BY k.ord),
       ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(attnum, ord)
             JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum
             ORDER BY k.ord),
       con.convalidated,
       CASE WHEN con.contype IN ('p', 'u', 'x') THEN pg_get_indexdef(con.conindid) END
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_class ref ON ref.oid = con.confrelid
LEFT JOIN pg_namespace rn ON rn.oid = ref.relnamespace
WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND con.contype IN ('p', 'u', 'f', 'c', 'x')
"""

# Indexes that back a primary-key / unique / exclusion constraint are already
# reported as that constraint, so they are left out here to avoid double entries.
# indisvalid catches an index left invalid by a failed CREATE INDEX CONCURRENTLY,
# whose definition text is otherwise identical to a healthy one.
_INDEXES_SQL = """
SELECT i.tablename, i.indexname, i.indexdef, x.indisvalid
FROM pg_indexes i
JOIN pg_namespace ins ON ins.nspname = i.schemaname
JOIN pg_class ic ON ic.relname = i.indexname AND ic.relnamespace = ins.oid
JOIN pg_index x ON x.indexrelid = ic.oid
WHERE i.schemaname = %s
  AND NOT EXISTS (
      SELECT 1 FROM pg_constraint con
      WHERE con.conindid = ic.oid AND con.contype IN ('p', 'u', 'x')
  )
"""

# Internal triggers (the ones Postgres creates for foreign keys) are excluded;
# they are covered by the constraint itself.
_TRIGGERS_SQL = """
SELECT c.relname, tg.tgname, pg_get_triggerdef(tg.oid), tg.tgenabled::text
FROM pg_trigger tg
JOIN pg_class c ON c.oid = tg.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition AND NOT tg.tgisinternal
"""

_POLICIES_SQL = """
SELECT tablename, policyname, permissive, roles::text[], cmd, qual, with_check
FROM pg_policies
WHERE schemaname = %s
"""

_TRIGGER_ENABLED = {"O": "origin", "D": "disabled", "R": "replica", "A": "always"}


def capture_snapshot(cursor, schema: str = "public", table_apps: dict | None = None, apps=None) -> dict:
    """Read the structure of every table in ``schema`` through a DB-API cursor.

    ``table_apps`` maps table name -> Django app label (unknown tables get
    ``None``). When ``apps`` is given, only tables whose label is in it are kept.
    """
    table_apps = table_apps or {}
    wanted = set(apps) if apps else None

    cursor.execute("SHOW server_version_num")
    server_version_num = int(cursor.fetchone()[0])

    cursor.execute(_TABLES_SQL, [schema])
    tables: dict[str, dict] = {}
    for name, row_security, force_row_security, reloptions in cursor.fetchall():
        app = table_apps.get(name)
        if wanted is not None and app not in wanted:
            continue
        properties = {}
        if row_security:
            properties["row_security"] = True
        if force_row_security:
            properties["force_row_security"] = True
        if reloptions:
            properties["storage_options"] = sorted(reloptions)
        tables[name] = {
            "app": app,
            "properties": properties,
            "columns": {},
            "constraints": {},
            "indexes": {},
            "triggers": {},
            "policies": {},
        }

    cursor.execute(_COLUMNS_SQL, [schema])
    for table, column, data_type, notnull, default, identity, generated, collation in cursor.fetchall():
        if table not in tables:
            continue
        col = {"type": data_type, "nullable": not notnull, "default": default}
        if _GENERATED.get(generated or ""):
            col = {"type": data_type, "nullable": not notnull, "generated": _GENERATED[generated], "expression": default}
        if _IDENTITY.get(identity or ""):
            col["identity"] = _IDENTITY[identity]
        if collation:
            col["collation"] = collation
        tables[table]["columns"][column] = col

    cursor.execute(_CONSTRAINTS_SQL, [schema])
    for row in cursor.fetchall():
        (table, name, contype, definition, ref_table, on_delete, on_update, deferrable, deferred,
         cols, ref_cols, validated, index_definition) = row
        if table not in tables:
            continue
        kind = _CONSTRAINT_TYPES[contype]
        if kind == "foreign_key":
            con = {
                "type": kind,
                "definition": definition,
                "columns": list(cols),
                "references_table": ref_table,
                "references_columns": list(ref_cols),
                "on_delete": _FK_ACTIONS.get(on_delete, on_delete),
                "on_update": _FK_ACTIONS.get(on_update, on_update),
                "deferrable": bool(deferrable),
                "initially_deferred": bool(deferred),
            }
        elif kind == "check":
            con = {"type": kind, "expression": definition}
        else:
            con = {"type": kind, "columns": list(cols), "definition": definition, "index_definition": index_definition}
        con["validated"] = bool(validated)
        tables[table]["constraints"][name] = con

    cursor.execute(_INDEXES_SQL, [schema])
    for table, name, definition, valid in cursor.fetchall():
        if table in tables:
            tables[table]["indexes"][name] = {"definition": definition, "valid": bool(valid)}

    cursor.execute(_TRIGGERS_SQL, [schema])
    for table, name, definition, enabled in cursor.fetchall():
        if table in tables:
            tables[table]["triggers"][name] = {
                "definition": definition,
                "enabled": _TRIGGER_ENABLED.get(enabled, enabled),
            }

    cursor.execute(_POLICIES_SQL, [schema])
    for table, name, permissive, roles, command, qual, with_check in cursor.fetchall():
        if table in tables:
            tables[table]["policies"][name] = {
                "permissive": permissive,
                "roles": sorted(roles or []),
                "command": command,
                "using": qual,
                "with_check": with_check,
            }

    return {
        "format": SNAPSHOT_FORMAT,
        "schema": schema,
        "postgres_major": server_version_num // 10000,
        "tables": tables,
    }


def dump_snapshot(snapshot: dict) -> str:
    """Deterministic JSON: sorted keys, stable indentation, trailing newline."""
    return json.dumps(snapshot, indent=2, sort_keys=True) + "\n"


def load_snapshot(path) -> dict:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or "tables" not in data:
        raise ValueError(f"{path} is not a schema snapshot (no 'tables' key)")
    if data.get("format") != SNAPSHOT_FORMAT:
        raise ValueError(f"{path} has snapshot format {data.get('format')!r}; this tool reads {SNAPSHOT_FORMAT}")
    return data


# --------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------

_INDEX_NAME_RE = re.compile(r"^(CREATE (?:UNIQUE )?INDEX )(?:CONCURRENTLY )?(?:IF NOT EXISTS )?\S+( ON )")
_TRIGGER_NAME_RE = re.compile(r"^(CREATE (?:CONSTRAINT )?TRIGGER )\S+( )")

def _index_shape(definition: str | None) -> str:
    """An index definition with its own name removed, for rename detection."""
    return _INDEX_NAME_RE.sub(r"\1<name>\2", definition or "")


def _constraint_shape(con: dict) -> str:
    con = dict(con)
    if con.get("index_definition"):
        con["index_definition"] = _index_shape(con["index_definition"])
    return json.dumps(con, sort_keys=True)


def _index_def_shape(idx: dict) -> str:
    return json.dumps({**idx, "definition": _index_shape(idx.get("definition"))}, sort_keys=True)


def _trigger_shape(trg: dict) -> str:
    definition = _TRIGGER_NAME_RE.sub(r"\1<name>\2", trg.get("definition") or "")
    return json.dumps({**trg, "definition": definition}, sort_keys=True)


def _policy_shape(pol: dict) -> str:
    return json.dumps(pol, sort_keys=True)


# Named collections per table, in report order: (snapshot key, entry kind, shape).
# The shape strips the object's own name so a same-definition rename pairs up.
_COLLECTIONS = (
    ("constraints", "constraint", _constraint_shape),
    ("indexes", "index", _index_def_shape),
    ("triggers", "trigger", _trigger_shape),
    ("policies", "policy", _policy_shape),
)


def _field_changes(baseline: dict, actual: dict) -> list[dict]:
    fields = sorted(set(baseline) | set(actual))
    return [
        {"field": f, "baseline": baseline.get(f), "actual": actual.get(f)}
        for f in fields
        if baseline.get(f) != actual.get(f)
    ]


def _entry(table, kind, name, change, baseline=None, actual=None, **extra):
    if kind == "table":
        key = table
    elif kind == "column":
        key = f"{table}.{name}"
    else:
        key = f"{table}.{kind}:{name}"
    entry = {"key": key, "table": table, "kind": kind, "name": name, "change": change}
    if baseline is not None:
        entry["baseline"] = baseline
    if actual is not None:
        entry["actual"] = actual
    entry.update(extra)
    return entry


def _diff_named(table, kind, base_items: dict, act_items: dict, shape) -> list[dict]:
    """Diff a name-keyed collection, pairing a removed+added with the same shape as a rename."""
    entries = []
    removed = sorted(set(base_items) - set(act_items))
    added = sorted(set(act_items) - set(base_items))

    added_by_shape = defaultdict(list)
    for name in added:
        added_by_shape[shape(act_items[name])].append(name)
    renamed_to = {}
    for old in removed:
        candidates = added_by_shape.get(shape(base_items[old]))
        if candidates:
            renamed_to[old] = candidates.pop(0)
    renamed_new = set(renamed_to.values())

    for old in removed:
        if old in renamed_to:
            new = renamed_to[old]
            entries.append(
                _entry(
                    table, kind, new, "renamed",
                    base_items[old], act_items[new],
                    renamed_from=old, alt_keys=[f"{table}.{kind}:{old}"],
                )
            )
        else:
            entries.append(_entry(table, kind, old, "removed", baseline=base_items[old]))
    for new in added:
        if new not in renamed_new:
            entries.append(_entry(table, kind, new, "added", actual=act_items[new]))
    for name in sorted(set(base_items) & set(act_items)):
        changes = _field_changes(base_items[name], act_items[name])
        if changes:
            entries.append(_entry(table, kind, name, "changed", base_items[name], act_items[name], changes=changes))
    return entries


def table_in_scope(name: str, baseline: dict, actual: dict, apps) -> bool:
    """A table is in scope when either side labels it with one of ``apps``.

    Checking both sides keeps an app-label move (same ``db_table``, new label)
    from showing up as a spurious remove + add.
    """
    if not apps:
        return True
    labels = {baseline["tables"].get(name, {}).get("app"), actual["tables"].get(name, {}).get("app")}
    return bool(labels & set(apps))


def diff_snapshots(baseline: dict, actual: dict, apps=None) -> list[dict]:
    """Return diff entries sorted by table, then kind, then name."""
    base_tables = baseline["tables"]
    act_tables = actual["tables"]
    names = sorted(
        n for n in set(base_tables) | set(act_tables) if table_in_scope(n, baseline, actual, apps)
    )
    entries = []
    for name in names:
        if name not in act_tables:
            entries.append(_entry(name, "table", name, "removed"))
            continue
        if name not in base_tables:
            entries.append(_entry(name, "table", name, "added"))
            continue
        b, a = base_tables[name], act_tables[name]
        prop_changes = _field_changes(b.get("properties", {}), a.get("properties", {}))
        if prop_changes:
            entries.append(
                _entry(name, "table", name, "changed", b.get("properties", {}), a.get("properties", {}),
                       changes=prop_changes)
            )
        b_cols, a_cols = b.get("columns", {}), a.get("columns", {})
        for col in sorted(set(b_cols) - set(a_cols)):
            entries.append(_entry(name, "column", col, "removed", baseline=b_cols[col]))
        for col in sorted(set(a_cols) - set(b_cols)):
            entries.append(_entry(name, "column", col, "added", actual=a_cols[col]))
        for col in sorted(set(b_cols) & set(a_cols)):
            changes = _field_changes(b_cols[col], a_cols[col])
            if changes:
                entries.append(_entry(name, "column", col, "changed", b_cols[col], a_cols[col], changes=changes))
        for collection, kind, shape in _COLLECTIONS:
            entries += _diff_named(name, kind, b.get(collection, {}), a.get(collection, {}), shape)
    return entries


# --------------------------------------------------------------------------
# Expected-diff manifest
# --------------------------------------------------------------------------


def parse_expected(text: str) -> dict[str, set | None]:
    """Parse the manifest into ``{key: fields}``.

    One entry per line: ``key`` alone covers every change to that object;
    ``key field [field ...]`` covers only a ``changed`` entry whose changed
    fields are all listed. Blank lines and ``#`` comments are ignored. A bare
    key wins over field-pinned lines for the same key; pinned lines merge.
    """
    manifest: dict[str, set | None] = {}
    for raw in text.splitlines():
        parts = raw.split("#", 1)[0].split()
        if not parts:
            continue
        key, fields = parts[0], set(parts[1:])
        if not fields or (key in manifest and manifest[key] is None):
            manifest[key] = None
        else:
            manifest[key] = (manifest.get(key) or set()) | fields
    return manifest


def _manifest_items(manifest) -> list[tuple[str, str | None]]:
    """Flatten to (key, field) pairs; field None means the whole object."""
    if not manifest:
        return []
    if not isinstance(manifest, dict):  # a plain list of bare keys
        manifest = {k: None for k in manifest}
    items = []
    for key in manifest:
        fields = manifest[key]
        if fields is None:
            items.append((key, None))
        else:
            items.extend((key, f) for f in sorted(fields))
    return items


def _format_item(item) -> str:
    key, field = item
    return key if field is None else f"{key} {field}"


def classify(entries: list[dict], manifest) -> tuple[list[dict], list[str]]:
    """Mark each entry ``expected`` or ``unexpected``; return (entries, stale manifest lines).

    A renamed entry matches by its new or its old key. A bare key covers the
    whole object. A field-pinned key covers a ``changed`` entry only when every
    changed field is pinned; fields left uncovered are listed on the entry as
    ``uncovered_fields``. A manifest line (or pinned field) that matches nothing
    is *stale*.
    """
    items = _manifest_items(manifest)
    whole = {k for k, f in items if f is None}
    pinned = defaultdict(set)
    for k, f in items:
        if f is not None:
            pinned[k].add(f)
    used = set()
    out = []
    for entry in entries:
        keys = [entry["key"], *entry.get("alt_keys", [])]
        hit_whole = [k for k in keys if k in whole]
        if hit_whole:
            used.update((k, None) for k in hit_whole)
            out.append({**entry, "status": "expected"})
            continue
        pins = set().union(*(pinned.get(k, set()) for k in keys))
        if entry["change"] == "changed" and pins:
            changed = {c["field"] for c in entry["changes"]}
            for k in keys:
                used.update((k, f) for f in pinned.get(k, set()) & changed)
            uncovered = sorted(changed - pins)
            if uncovered:
                out.append({**entry, "status": "unexpected", "uncovered_fields": uncovered})
            else:
                out.append({**entry, "status": "expected"})
            continue
        out.append({**entry, "status": "unexpected"})
    stale = [_format_item(i) for i in items if i not in used]
    return out, stale


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

_SIGILS = {"added": "+", "removed": "-", "changed": "~", "renamed": ">"}


def _describe_column(col: dict) -> str:
    parts = [col.get("type", "?")]
    if col.get("collation"):
        parts.append(f'COLLATE "{col["collation"]}"')
    parts.append("NULL" if col.get("nullable") else "NOT NULL")
    if col.get("identity"):
        parts.append(f"IDENTITY {col['identity']}")
    if col.get("generated"):
        parts.append(f"GENERATED {col['generated']} AS {col.get('expression')}")
    elif col.get("default") is not None:
        parts.append(f"DEFAULT {col['default']}")
    return " ".join(parts)


def _describe(entry: dict, side: str) -> str:
    obj = entry.get(side)
    if obj is None:
        return ""
    kind = entry["kind"]
    if kind == "column":
        return _describe_column(obj)
    if kind == "constraint":
        return obj.get("expression") or obj.get("definition") or obj.get("type", "?")
    if kind == "index":
        return obj.get("definition", "") + ("" if obj.get("valid", True) else "  (INVALID)")
    if kind == "trigger":
        return obj.get("definition", "")
    if kind == "policy":
        return f"{obj.get('permissive')} {obj.get('command')} TO {','.join(obj.get('roles') or [])} USING {obj.get('using')}"
    return ""


def format_report(
    entries: list[dict], stale: list[str], baseline_label: str, actual_label: str, warnings=()
) -> str:
    lines = [f"Schema parity: baseline {baseline_label}  vs  actual {actual_label}"]
    lines.append("  (+ only in actual, - only in baseline, ~ changed, > renamed)")
    lines.extend(f"WARNING: {w}" for w in warnings)
    if not entries:
        lines.append("")
        lines.append("No differences.")
    by_table = defaultdict(list)
    for entry in entries:
        by_table[entry["table"]].append(entry)
    for table in sorted(by_table):
        lines.append("")
        lines.append(table)
        for e in by_table[table]:
            tag = f"  [{e['status']}]" if "status" in e else ""
            sigil = _SIGILS[e["change"]]
            if e["kind"] == "table" and e["change"] != "changed":
                lines.append(f"  {sigil} table {e['change']}{tag}")
                continue
            head = f"  {sigil} {e['kind']:<10} {e['name']}"
            if e["change"] == "renamed":
                lines.append(f"{head} (renamed from {e['renamed_from']}){tag}")
            elif e["change"] == "changed":
                # Every changed field is printed, for expected entries too, so a
                # reviewer sees exactly what a manifest line is blessing.
                lines.append(f"{head}{tag}")
                uncovered = set(e.get("uncovered_fields", []))
                for c in e["changes"]:
                    note = "  (not in manifest)" if c["field"] in uncovered else ""
                    lines.append(f"      {c['field']}: {c['baseline']!r} -> {c['actual']!r}{note}")
            else:
                side = "actual" if e["change"] == "added" else "baseline"
                lines.append(f"{head}  {_describe(e, side)}{tag}")
    if stale:
        lines.append("")
        lines.append("Stale expected-diff entries (listed in the manifest, did not occur):")
        lines.extend(f"  ! {k}" for k in stale)
    lines.append("")
    lines.append(summary_line(entries, stale))
    return "\n".join(lines)


def major_version_warning(baseline: dict, actual: dict) -> str | None:
    """Catalog text (format_type, pg_get_*def) can differ across Postgres majors."""
    b, a = baseline.get("postgres_major"), actual.get("postgres_major")
    if b != a:
        return (
            f"Postgres major versions differ (baseline {b}, actual {a}); definition text may differ "
            "for reasons other than schema. Compare snapshots taken on the same major."
        )
    return None


def summary(entries: list[dict], stale: list[str]) -> dict:
    expected = sum(1 for e in entries if e.get("status") == "expected")
    return {
        "total": len(entries),
        "expected": expected,
        "unexpected": len(entries) - expected,
        "stale": len(stale),
    }


def summary_line(entries: list[dict], stale: list[str]) -> str:
    s = summary(entries, stale)
    return (
        f"{s['total']} difference(s): {s['expected']} expected, {s['unexpected']} unexpected; "
        f"{s['stale']} stale manifest entr{'y' if s['stale'] == 1 else 'ies'}."
    )
