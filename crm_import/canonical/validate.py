"""``validate_canonical(path)``: check a canonical export directory.

Streams every file once, keeps only ids (not records) in memory, and returns
a :class:`ValidationResult` of structured issues with file, line and field.
Nothing raises on bad *data*; only an unreadable directory raises.

Rules are listed in docs/crm/importing.md ("Validation rules"); every issue
code is in ``schema.ISSUE_CODES``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from core.plugins.payroll import AdapterError, require_known_version

from . import schema as s
from .schema import F

DEFAULT_MAX_ERRORS = 1000


@dataclass(frozen=True)
class Issue:
    """One finding. ``line`` is 1-based (None for manifest-level issues)."""

    code: str
    file: str
    line: int | None
    field: str
    message: str
    severity: str = "error"  # "error" | "warning"

    def to_dict(self, *, messages: bool = True) -> dict:
        out = {"code": self.code, "file": self.file, "line": self.line, "field": self.field}
        if messages:
            out["message"] = self.message
        return out

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        fld = f" {self.field}" if self.field else ""
        return f"{where}{fld}: {self.code} — {self.message}"


@dataclass
class ValidationResult:
    directory: str
    schema_version: int | None = None
    source: str | None = None
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    coverage: dict[str, str] = field(default_factory=dict)
    suppressed: int = 0  # issues not recorded once max_errors was reached

    @property
    def ok(self) -> bool:
        return not self.errors and not self.suppressed

    def to_dict(self, *, messages: bool = True) -> dict:
        return {
            "ok": self.ok,
            "schema_version": self.schema_version,
            "source": self.source,
            "counts": dict(self.counts),
            "coverage": dict(self.coverage),
            "errors": [i.to_dict(messages=messages) for i in self.errors],
            "warnings": [i.to_dict(messages=messages) for i in self.warnings],
            "suppressed": self.suppressed,
        }

    def raise_for_errors(self) -> None:
        """Raise ``CanonicalFormatError`` naming the first errors, if any."""
        if self.ok:
            return
        from .io import CanonicalFormatError

        shown = "; ".join(str(i) for i in self.errors[:5])
        more = len(self.errors) + self.suppressed - min(len(self.errors), 5)
        raise CanonicalFormatError(
            f"Canonical export {self.directory} failed validation ({shown}"
            f"{f'; and {more} more' if more > 0 else ''}) — cause: the export adapter "
            "wrote records that break the canonical schema. Fix: run "
            "`manage.py check_canonical_export <dir>` for the full list and fix the "
            "adapter or the export. See docs/crm/importing.md."
        )


def _parse_constant(name):  # json.loads would accept NaN / Infinity
    raise ValueError(f"{name} is not valid JSON")


def _describe(spec: F) -> str:
    if spec.kind == "enum":
        return "one of " + ", ".join(repr(c) for c in spec.choices)
    if spec.kind == "ref":
        return "a reference {object_type, source_id}"
    return {
        "str": "a string", "bool": "true or false", "int": "an integer",
        "decimal": "a decimal string such as \"1200.50\"", "currency": "an ISO 4217 code such as \"USD\"",
        "key": "a key matching [a-z][a-z0-9_]{0,31}", "kind_name": "a name matching [a-z][a-z0-9_]{0,31}",
        "instant": "a UTC instant such as \"2026-01-31T09:00:00Z\"", "date": "a date \"YYYY-MM-DD\"",
        "instant_or_date": "a UTC instant or a date", "list": "a list", "object": "an object",
        "dict": "an object", "source": "\"<provider>:<account id>\"", "provider": "a provider id",
        "time_zone": "an IANA time zone name", "reason_code": "a coverage reason code",
    }.get(spec.kind, spec.kind)


def _is_instant(value: str) -> bool:
    if not s.INSTANT_RE.match(value):
        return False
    try:
        dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def _is_date(value: str) -> bool:
    if not s.DATE_RE.match(value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


@dataclass
class _Ctx:
    file: str
    line: int | None
    refs: list | None = None  # (path, object_type, source_id) found while checking


class _Validator:
    def __init__(self, directory: Path, max_errors: int):
        self.dir = directory
        self.max_errors = max_errors
        self.result = ValidationResult(directory=str(directory))
        self.ids: dict[str, set[str]] = {t: set() for t in s.OBJECT_TYPES}
        # stage source_id -> the pipelines holding it (ids are unique per pipeline only)
        self.stage_pipeline: dict[str, set[str]] = {}
        self.archived_rows: dict[str, int] = {}
        self.pipeline_applies: dict[str, str] = {}  # pipeline source_id -> applies_to
        self.refs: list[tuple[str, int, str, str, str]] = []  # file, line, path, type, id
        self.pipeline_uses: list[tuple[str, int, str, str, str]] = []  # file, line, pipe, stage, kind
        self.unreliable: set[str] = set()  # types whose file was not fully read
        self.manifest_version: int | None = None
        self.source: str | None = None

    # -- recording ---------------------------------------------------------
    def add(self, code: str, ctx: _Ctx, fld: str, message: str, severity: str = "error") -> None:
        issue = Issue(code, ctx.file, ctx.line, fld, message, severity)
        if severity == "warning":
            self.result.warnings.append(issue)
            return
        if len(self.result.errors) >= self.max_errors:
            self.result.suppressed += 1
            return
        self.result.errors.append(issue)

    # -- generic shape checks ----------------------------------------------
    def check_keys(self, obj: Mapping, required: Mapping[str, F], optional: Mapping[str, F],
                   path: str, ctx: _Ctx) -> None:
        prefix = f"{path}." if path else ""
        for key in required:
            if key not in obj:
                self.add("missing_key", ctx, prefix + key, f"required key {key!r} is missing")
        for key in obj:
            if key not in required and key not in optional:
                hint = " (source fields go in `properties`)" if path == "data" else ""
                self.add("unknown_key", ctx, prefix + key, f"key {key!r} is not part of the schema{hint}")
        for key, value in obj.items():
            spec = required.get(key) or optional.get(key)
            if spec is not None:
                self.check(value, spec, prefix + key, ctx)

    def check(self, value: Any, spec: F, path: str, ctx: _Ctx) -> bool:
        """Check one value. Returns True when its type is right (so callers
        can run cross-field rules on it)."""
        if value is None:
            if spec.nullable or spec.kind == "json":
                return True
            self.add("wrong_type", ctx, path, f"null is not allowed; expected {_describe(spec)}")
            return False
        k = spec.kind

        def wrong() -> bool:
            self.add("wrong_type", ctx, path, f"got {type(value).__name__}; expected {_describe(spec)}")
            return False

        def bad(why: str) -> bool:
            self.add("invalid_value", ctx, path, why)
            return True

        if k == "json":
            return True
        if k == "bool":
            return True if isinstance(value, bool) else wrong()
        if k == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                return wrong()
            if spec.min_value is not None and value < spec.min_value:
                return bad(f"{value} is below the minimum {spec.min_value}")
            if spec.max_value is not None and value > spec.max_value:
                return bad(f"{value} is above the maximum {spec.max_value}")
            return True
        if k in ("dict", "object"):
            if not isinstance(value, dict):
                return wrong()
            if k == "object":
                self.check_keys(value, spec.fields or {}, spec.optional, path, ctx)
            return True
        if k == "list":
            if not isinstance(value, list):
                return wrong()
            for i, item in enumerate(value):
                self.check(item, spec.item, f"{path}[{i}]", ctx)
            return True
        if k == "ref":
            return self.check_ref(value, spec, path, ctx)
        # every remaining kind is a string
        if not isinstance(value, str):
            return wrong()
        if k == "str":
            if spec.non_empty and not value.strip():
                return bad("must not be empty")
        elif k == "enum":
            if value not in spec.choices:
                return bad(f"{value!r} is not {_describe(spec)}")
        elif k == "key":
            if not s.KIND_RE.match(value):
                return bad(f"{value!r} is not {_describe(spec)}")
        elif k == "kind_name":
            if not s.KIND_RE.match(value):
                return bad(f"{value!r} is not {_describe(spec)}")
        elif k == "decimal":
            if not s.DECIMAL_RE.match(value):
                return bad(f"{value!r} is not {_describe(spec)}")
        elif k == "currency":
            if not s.CURRENCY_RE.match(value):
                return bad(f"{value!r} is not {_describe(spec)}")
        elif k == "instant":
            if not _is_instant(value):
                return bad(f"{value!r} is not {_describe(spec)} (UTC, with Z)")
        elif k == "date":
            if not _is_date(value):
                return bad(f"{value!r} is not {_describe(spec)}")
        elif k == "instant_or_date":
            if not (_is_instant(value) or _is_date(value)):
                return bad(f"{value!r} is not {_describe(spec)}")
        elif k == "source":
            if len(value) > 64 or not s.SOURCE_RE.match(value):
                return bad(f"{value!r} is not {_describe(spec)} (at most 64 characters)")
        elif k == "provider":
            if not s.PROVIDER_RE.match(value):
                return bad(f"{value!r} is not {_describe(spec)} ([a-z][a-z0-9_]{{0,31}})")
        elif k == "time_zone":
            try:
                zoneinfo.ZoneInfo(value)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                return bad(f"{value!r} is not {_describe(spec)}")
        elif k == "reason_code":
            if value not in s.COVERAGE_REASONS:
                self.add("unknown_reason_code", ctx, path,
                         f"{value!r} is not a coverage reason; use one of {', '.join(s.COVERAGE_REASONS)}")
        if spec.max_length is not None and len(value) > spec.max_length:
            return bad(f"longer than {spec.max_length} characters")
        return True

    def check_ref(self, value: Any, spec: F, path: str, ctx: _Ctx) -> bool:
        if not isinstance(value, dict):
            self.add("wrong_type", ctx, path, f"got {type(value).__name__}; expected {_describe(spec)}")
            return False
        self.check_keys(value, {"object_type": s.S(), "source_id": s.S(non_empty=True, max_length=255)},
                        {}, path, ctx)
        otype, sid = value.get("object_type"), value.get("source_id")
        if not isinstance(otype, str) or not isinstance(sid, str) or not sid.strip():
            return False
        if otype not in s.REF_TARGET_TYPES:
            self.add("invalid_value", ctx, f"{path}.object_type", f"{otype!r} is not a canonical object type")
            return False
        if otype not in spec.ref_types:
            self.add("wrong_ref_type", ctx, path,
                     f"points at a {otype}; this field takes {' / '.join(spec.ref_types)}")
            return False
        if ctx.refs is not None:
            ctx.refs.append((path, otype, sid))
        return True

    # -- manifest ------------------------------------------------------------
    def check_manifest(self) -> dict | None:
        ctx = _Ctx(s.MANIFEST_FILE, None)
        path = self.dir / s.MANIFEST_FILE
        if not path.is_file():
            self.add("manifest_missing", ctx, "",
                     "no manifest.json — the adapter writes it last, so its absence means the "
                     "export is incomplete; the importer refuses the directory")
            return None
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"), parse_constant=_parse_constant)
        except UnicodeDecodeError:
            self.add("invalid_encoding", ctx, "", "manifest.json is not UTF-8")
            return None
        except ValueError as exc:
            self.add("invalid_json", ctx, "", f"manifest.json is not valid JSON ({exc})")
            return None
        if not isinstance(manifest, dict):
            self.add("not_an_object", ctx, "", "manifest.json must be a JSON object")
            return None
        version = manifest.get("schema_version")
        if isinstance(version, int) and not isinstance(version, bool):
            try:
                require_known_version(manifest, s.KNOWN_MAX_VERSION)
            except AdapterError:
                self.add("unknown_version", ctx, "schema_version", s.newer_version_message(version, "manifest.json"))
                return None  # a newer format is never misread
            self.manifest_version = version
            self.result.schema_version = version
        self.check_keys(manifest, s.MANIFEST_REQUIRED, {}, "", ctx)

        source, provider = manifest.get("source"), manifest.get("provider")
        if isinstance(source, str):
            self.source = source
            self.result.source = source
            if isinstance(provider, str) and source.split(":", 1)[0] != provider:
                self.add("source_mismatch", ctx, "source",
                         f"source {source!r} does not start with the provider {provider!r}")
        adapter = manifest.get("adapter")
        if isinstance(adapter, dict) and isinstance(adapter.get("schema_version"), int) \
                and self.manifest_version is not None and adapter["schema_version"] != self.manifest_version:
            self.add("version_mismatch", ctx, "adapter.schema_version",
                     f"adapter writes version {adapter['schema_version']} but the manifest says "
                     f"{self.manifest_version}")

        objects = manifest.get("objects")
        if isinstance(objects, dict):
            for otype in s.OBJECT_TYPES:
                if otype not in objects:
                    self.add("object_type_missing", ctx, f"objects.{otype}",
                             f"the manifest says nothing about {otype}; every type needs a coverage "
                             "entry (\"unknown\" with reason file_absent when the source has none)")
            for otype in s.OBJECT_TYPES:
                if otype in objects:
                    self.check_manifest_object(otype, objects[otype], ctx)
            for otype in objects:
                if otype not in s.OBJECT_FILES:
                    self.add("unknown_key", ctx, f"objects.{otype}", f"{otype!r} is not a canonical object type")
        files = manifest.get("files")
        if isinstance(files, dict):
            known = set(s.OBJECT_FILES.values())
            for name, digest in files.items():
                if name not in known:
                    self.add("unknown_key", ctx, f"files.{name}", f"{name!r} is not a canonical file")
                elif not isinstance(digest, str) or not s.SHA256_RE.match(digest):
                    self.add("invalid_value", ctx, f"files.{name}", "expected \"sha256:<64 hex digits>\"")
        return manifest

    def check_manifest_object(self, otype: str, entry: Any, ctx: _Ctx) -> None:
        path = f"objects.{otype}"
        if not isinstance(entry, dict):
            self.add("wrong_type", ctx, path, "expected an object")
            return
        self.check_keys(entry, s.MANIFEST_OBJECT_REQUIRED, s.MANIFEST_OBJECT_OPTIONAL, path, ctx)
        if "file" in entry and entry["file"] != s.OBJECT_FILES[otype]:
            self.add("invalid_value", ctx, f"{path}.file",
                     f"{otype} records live in {s.OBJECT_FILES[otype]!r}, not {entry['file']!r}")
        coverage = entry.get("coverage")
        reasons = entry.get("reasons") or []
        if coverage in s.COVERAGE_VALUES:
            self.result.coverage[otype] = coverage
        if coverage in ("partial", "unknown") and not reasons:
            self.add("coverage_reason_missing", ctx, f"{path}.reasons",
                     f"coverage {coverage!r} needs at least one reason saying what is missing")
        if coverage == "complete" and reasons:
            self.add("inconsistent", ctx, f"{path}.reasons", "coverage \"complete\" cannot carry reasons")
        if coverage == "complete" and entry.get("archived") == "not_enumerated":
            self.add("inconsistent", ctx, f"{path}.archived",
                     "coverage \"complete\" but archived records were not enumerated; that is partial "
                     "coverage with reason archived_not_enumerated")

    # -- records -------------------------------------------------------------
    def check_files(self, manifest: dict) -> None:
        objects = manifest.get("objects") if isinstance(manifest.get("objects"), dict) else {}
        files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
        listed: set[str] = set()
        for otype, fname in s.OBJECT_FILES.items():
            entry = objects.get(otype)
            if not isinstance(entry, dict):
                self.unreliable.add(otype)
                continue
            listed.add(fname)
            path = self.dir / fname
            mctx = _Ctx(s.MANIFEST_FILE, None)
            if not path.is_file():
                if entry.get("coverage") == "unknown" and entry.get("rows") == 0 and fname not in files:
                    self.result.counts[otype] = 0
                else:
                    self.add("file_missing", _Ctx(fname, None), "",
                             f"the manifest lists {fname} but the directory has no such file")
                    self.unreliable.add(otype)
                continue
            rows, digest, complete = self.read_file(otype, path)
            self.result.counts[otype] = rows
            if not complete:
                continue
            if fname not in files:
                self.add("missing_key", mctx, f"files.{fname}", f"no sha256 for {fname}")
            elif isinstance(files[fname], str) and files[fname] != f"sha256:{digest}":
                self.add("hash_mismatch", mctx, f"files.{fname}",
                         f"{fname} does not match its manifest hash — the file changed after export")
            declared_rows = entry.get("rows")
            if isinstance(declared_rows, int) and declared_rows != rows:
                self.add("row_count_mismatch", mctx, f"objects.{otype}.rows",
                         f"the manifest says {declared_rows} rows; {fname} has {rows}")
            # declared_total is the source's own count of LIVE records (a count
            # probe never sees the recycle bin), so archived rows are not in it.
            declared, live = entry.get("declared_total"), rows - self.archived_rows.get(otype, 0)
            if entry.get("coverage") == "complete" and isinstance(declared, int) \
                    and not isinstance(declared, bool) and declared != live:
                self.add("inconsistent", mctx, f"objects.{otype}.declared_total",
                         f"coverage \"complete\" but the source declared {declared} live records and {live} "
                         "were exported; that is partial coverage with reason count_mismatch")
        for path in sorted(self.dir.glob("*.jsonl")):
            if path.name not in listed:
                self.add("unlisted_file", _Ctx(path.name, None), "",
                         f"{path.name} is not described by the manifest; the importer would ignore it")

    def read_file(self, otype: str, path: Path) -> tuple[int, str, bool]:
        """Stream one JSONL file. Returns (rows, sha256 hex, fully read)."""
        fname = path.name
        digest = hashlib.sha256()
        rows = 0
        previous: str | None = None
        seen: dict[str, int] = {}  # source_id or alias -> line
        state: dict[str, Any] = {"keys": {}, "stage_ids": {}}
        with path.open("rb") as fh:
            for line_no, raw in enumerate(fh, 1):
                digest.update(raw)
                ctx = _Ctx(fname, line_no, refs=[])
                stripped = raw.strip()
                if not stripped:
                    self.add("blank_line", ctx, "", "blank line; every line must be one record")
                    continue
                rows += 1
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    self.add("invalid_encoding", ctx, "", "line is not UTF-8")
                    self.unreliable.add(otype)  # its ids are unknown: no dangling-ref cascade
                    continue
                try:
                    record = json.loads(text.rstrip("\r\n"), parse_constant=_parse_constant)
                except ValueError as exc:
                    self.add("invalid_json", ctx, "", f"not valid JSON ({exc})")
                    self.unreliable.add(otype)
                    continue
                if not isinstance(record, dict):
                    self.add("not_an_object", ctx, "", "each line must be a JSON object")
                    self.unreliable.add(otype)
                    continue
                version = record.get("schema_version")
                if isinstance(version, int) and not isinstance(version, bool):
                    try:
                        require_known_version(record, s.KNOWN_MAX_VERSION)
                    except AdapterError:
                        self.add("unknown_version", ctx, "schema_version",
                                 s.newer_version_message(version, "this record")
                                 + f" The rest of {fname} was not read.")
                        self.unreliable.add(otype)
                        for rest in fh:  # keep the row count honest
                            rows += 1 if rest.strip() else 0
                        return rows, digest.hexdigest(), False
                    if self.manifest_version is not None and version != self.manifest_version:
                        self.add("version_mismatch", ctx, "schema_version",
                                 f"record is version {version}; the manifest is {self.manifest_version}")
                self.check_record(otype, record, ctx, state)
                if record.get("archived") is True:
                    self.archived_rows[otype] = self.archived_rows.get(otype, 0) + 1
                sid = record.get("source_id")
                if isinstance(sid, str) and sid.strip():
                    if previous is not None and sid < previous:
                        self.add("unsorted", ctx, "source_id",
                                 f"{sid!r} comes after {previous!r}; files are sorted by source_id")
                    previous = sid
                    self.claim(sid, "source_id", seen, ctx, otype)
                    aliases = record.get("aliases")
                    if isinstance(aliases, list):
                        for i, alias in enumerate(aliases):
                            if isinstance(alias, str) and alias.strip():
                                self.claim(alias, f"aliases[{i}]", seen, ctx, otype)
                for path_, rtype, rid in ctx.refs:
                    self.refs.append((fname, line_no, path_, rtype, rid))
        return rows, digest.hexdigest(), True

    def claim(self, sid: str, fld: str, seen: dict[str, int], ctx: _Ctx, otype: str) -> None:
        if sid in seen:
            self.add("duplicate_source_id", ctx, fld,
                     f"{sid!r} is already used on line {seen[sid]}; a source id (or alias) names one record")
            return
        seen[sid] = ctx.line
        self.ids[otype].add(sid)

    def check_record(self, otype: str, record: dict, ctx: _Ctx, state: dict) -> None:
        self.check_keys(record, s.ENVELOPE_REQUIRED, s.ENVELOPE_OPTIONAL, "", ctx)
        rtype = record.get("object_type")
        if isinstance(rtype, str) and rtype != otype:
            self.add("wrong_object_type", ctx, "object_type",
                     f"a {rtype!r} record in {s.OBJECT_FILES[otype]}, which holds {otype!r} records")
            return
        source = record.get("source")
        if self.source is not None and isinstance(source, str) and source != self.source:
            self.add("source_mismatch", ctx, "source", f"{source!r} differs from the manifest's {self.source!r}")
        data = record.get("data")
        if not isinstance(data, dict):
            return
        self.check_keys(data, s.DATA_SCHEMAS[otype], {}, "data", ctx)
        rule = getattr(self, f"rules_{otype}", None)
        if rule is not None:
            rule(record, data, ctx, state)

    # -- per-type cross-field rules -------------------------------------------
    def one_primary(self, data: dict, key: str, ctx: _Ctx, prefix: str = "data") -> None:
        items = data.get(key)
        if isinstance(items, list):
            primaries = [i for i, x in enumerate(items) if isinstance(x, dict) and x.get("primary") is True]
            if len(primaries) > 1:
                self.add("inconsistent", ctx, f"{prefix}.{key}",
                         f"{len(primaries)} entries are primary (indexes {primaries}); at most one may be")

    def rules_owner(self, record, data, ctx, state):
        self.one_primary(data, "teams", ctx)

    def rules_company(self, record, data, ctx, state):
        self.one_primary(data, "phones", ctx)
        self.one_primary(data, "addresses", ctx)

    def rules_contact(self, record, data, ctx, state):
        for key in ("emails", "phones", "addresses"):
            self.one_primary(data, key, ctx)

    def rules_pipeline(self, record, data, ctx, state):
        sid = record.get("source_id")
        key = data.get("key")
        if isinstance(key, str):
            if key in state["keys"]:
                self.add("duplicate_key", ctx, "data.key",
                         f"pipeline key {key!r} is already used on line {state['keys'][key]}")
            else:
                state["keys"][key] = ctx.line
        if isinstance(sid, str) and isinstance(data.get("applies_to"), str):
            self.pipeline_applies[sid] = data["applies_to"]
        stages = data.get("stages")
        if not isinstance(stages, list):
            return
        stage_keys: set[str] = set()
        stage_ids: set[str] = set()
        for i, stage in enumerate(stages):
            if not isinstance(stage, dict):
                continue
            skey, ssid = stage.get("key"), stage.get("source_id")
            if isinstance(skey, str):
                if skey in stage_keys:
                    self.add("duplicate_key", ctx, f"data.stages[{i}].key",
                             f"stage key {skey!r} appears twice in this pipeline")
                stage_keys.add(skey)
            if isinstance(ssid, str) and ssid.strip():
                if ssid in stage_ids:
                    self.add("duplicate_source_id", ctx, f"data.stages[{i}].source_id",
                             f"stage id {ssid!r} appears twice in this pipeline")
                    continue
                stage_ids.add(ssid)
                if isinstance(sid, str):
                    self.stage_pipeline.setdefault(ssid, set()).add(sid)

    def _pipeline_use(self, data, ctx, kind):
        pref, sref = data.get("pipeline_ref"), data.get("stage_ref")
        if isinstance(pref, dict) and isinstance(sref, dict):
            pid, stid = pref.get("source_id"), sref.get("source_id")
            if isinstance(pid, str) and isinstance(stid, str):
                self.pipeline_uses.append((ctx.file, ctx.line, pid, stid, kind))

    def rules_deal(self, record, data, ctx, state):
        self._pipeline_use(data, ctx, "deal")

    def rules_relationship(self, record, data, ctx, state):
        self._pipeline_use(data, ctx, "ticket")

    def rules_task(self, record, data, ctx, state):
        due, is_date = data.get("due_at"), data.get("due_is_date")
        if isinstance(due, str) and isinstance(is_date, bool):
            if is_date and not _is_date(due):
                self.add("invalid_value", ctx, "data.due_at", "due_is_date is true, so due_at must be a date")
            elif not is_date and not _is_instant(due):
                self.add("invalid_value", ctx, "data.due_at", "due_is_date is false, so due_at must be a UTC instant")
        if data.get("status") == "done" and data.get("completed_at") is None and "completed_at" in data:
            self.add("inconsistent", ctx, "data.completed_at", "a done task needs completed_at")

    def rules_consent(self, record, data, ctx, state):
        suppress, cstate = data.get("suppress"), data.get("state")
        if suppress is not None and cstate == "granted":
            self.add("inconsistent", ctx, "data.state", f"a suppression ({suppress}) cannot come with a granted state")
        if "state" in data and "suppress" in data and cstate is None and suppress is None:
            self.add("inconsistent", ctx, "data.state",
                     "a consent record needs a state, a suppression, or both; this one has neither")
        if cstate is not None and "purpose" in data and data.get("purpose") is None:
            self.add("inconsistent", ctx, "data.purpose", "a consent state needs a purpose")

    def rules_property_def(self, record, data, ctx, state):
        otype, name = data.get("object_type"), data.get("name")
        if isinstance(otype, str) and isinstance(name, str):
            key = f"{otype}.{name}"
            if key in state["keys"]:
                self.add("duplicate_key", ctx, "data.name",
                         f"property {key!r} is already defined on line {state['keys'][key]}")
            else:
                state["keys"][key] = ctx.line

    # -- references ------------------------------------------------------------
    def check_refs(self) -> None:
        for fname, line, path, rtype, rid in self.refs:
            cover_type = "pipeline" if rtype == s.STAGE else rtype
            present = rid in self.stage_pipeline if rtype == s.STAGE else rid in self.ids[rtype]
            if present:
                continue
            ctx = _Ctx(fname, line)
            if cover_type in self.unreliable:
                continue  # its file already failed; one error is enough
            coverage = self.result.coverage.get(cover_type, "unknown")
            if coverage == "complete":
                self.add("dangling_ref", ctx, path,
                         f"{rtype} {rid!r} is not in the export, and {cover_type} coverage is complete")
            else:
                self.add("unresolved_ref", ctx, path,
                         f"{rtype} {rid!r} is not in the export ({cover_type} coverage is {coverage}); "
                         "the row will wait for it at apply time", severity="warning")
        for fname, line, pid, stid, kind in self.pipeline_uses:
            ctx = _Ctx(fname, line)
            applies = self.pipeline_applies.get(pid)
            if applies is not None and applies != kind:
                self.add("pipeline_kind_mismatch", ctx, "data.pipeline_ref",
                         f"pipeline {pid!r} applies to {applies}s, not {kind}s")
            owners = self.stage_pipeline.get(stid)
            if owners and pid in self.pipeline_applies and pid not in owners:
                self.add("stage_not_in_pipeline", ctx, "data.stage_ref",
                         f"stage {stid!r} belongs to pipeline {', '.join(sorted(owners))!r}, not {pid!r}")


def validate_canonical(path: str | Path, *, max_errors: int = DEFAULT_MAX_ERRORS) -> ValidationResult:
    """Validate the canonical export directory at ``path``.

    Returns every issue found (up to ``max_errors`` errors; the rest are
    counted in ``suppressed``). ``result.ok`` is the import gate.
    """
    directory = Path(path)
    if not directory.is_dir():
        raise FileNotFoundError(f"{directory} is not a directory")
    v = _Validator(directory, max_errors)
    manifest = v.check_manifest()
    if manifest is not None:
        v.check_files(manifest)
        v.check_refs()
    return v.result
