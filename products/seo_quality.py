"""Is this SEO value real copy? Template ("boilerplate") and truncation checks.

One definition of "generated or cut-off SEO copy", for anything that judges a
``seo_title`` / ``seo_meta_description`` value: a restore that must never
overwrite real copy, an audit command, a form warning, a storefront mirror.

The rules are data, not code. golden ships tenant-neutral defaults
(``products/data/seo_quality_defaults.json``: truncation shapes only, no
template patterns, since every shop's filler names that shop). A deployment
adds its own rules in files named by a setting, in the same shape::

    GOLDEN_SEO_PATTERN_FILES = [BASE_DIR / "myshop/data/seo_patterns.json"]

Each file may carry ``patterns`` (appended; ids must be unique across all
files) and ``truncation`` lists (merged onto the defaults, order kept,
duplicates dropped). Relative paths resolve against ``BASE_DIR``. A bad file
is ``ImproperlyConfigured`` when the products app loads, not on the first
check; the rules rebuild on ``setting_changed``, so tests can override the
setting.

Every pattern must compile identically in Python ``re`` and JavaScript
``RegExp`` (``patterns_payload()`` hands them to a page): no lookbehind, no
named groups, no inline flags; ``flags`` is ``""`` or ``"i"``.

Kinds: ``"title"`` (``seo_title``, ``seo_og_title``) and ``"meta"``
(``seo_meta_description``, ``seo_og_description``).

Pattern severities: ``template`` = the whole value is generated filler and
carries nothing worth keeping; ``padding`` = real copy with a generated
phrase added. Both make a value *replaceable*; only template (and certainly
cut-off) values are *unusable*, i.e. safe to blank.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

DEFAULTS_PATH = Path(__file__).resolve().parent / "data" / "seo_quality_defaults.json"
SETTING = "GOLDEN_SEO_PATTERN_FILES"

KINDS = ("title", "meta")

# Field name -> kind, for callers that work field by field.
FIELD_KINDS = {
    "seo_title": "title",
    "seo_og_title": "title",
    "seo_meta_description": "meta",
    "seo_og_description": "meta",
}

# Conventional search-result display limits. Reported, never enforced by
# cutting: a truncated value is worse than an over-long one.
TITLE_MAX = 60
META_MAX = 160

SEVERITY_TEMPLATE = "template"
SEVERITY_PADDING = "padding"
SEVERITIES = (SEVERITY_TEMPLATE, SEVERITY_PADDING)

_TRUNCATION_LISTS = (
    "title_bad_endings", "meta_patterns", "strict_dangling_words", "soft_dangling_words",
)
_TRUNCATION_KEYS = set(_TRUNCATION_LISTS) | {"meta_terminal_chars"}


@dataclass(frozen=True)
class BoilerplatePattern:
    id: str
    applies_to: tuple[str, ...]
    severity: str
    regex: re.Pattern
    examples: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class Truncation:
    strict_words: frozenset[str]
    soft_words: frozenset[str]
    title_bad_endings: tuple[str, ...]
    meta_terminal_chars: str
    meta_patterns: tuple[re.Pattern, ...]


@dataclass(frozen=True)
class Rules:
    patterns: tuple[BoilerplatePattern, ...]
    truncation: Truncation
    files: tuple[str, ...]


def _compile(pattern: str, flags: str, where: str) -> re.Pattern:
    if not isinstance(pattern, str) or not pattern:
        raise ImproperlyConfigured(f"{where}: pattern must be a non-empty string")
    if flags not in ("", "i"):
        raise ImproperlyConfigured(f"{where}: flags must be '' or 'i' (they mirror to JS), got {flags!r}")
    if "(?<" in pattern or "(?P" in pattern or re.search(r"\(\?[aiLmsux]", pattern):
        raise ImproperlyConfigured(f"{where}: lookbehind, named groups and inline flags do not mirror to JS")
    try:
        return re.compile(pattern, re.IGNORECASE if flags == "i" else 0)
    except re.error as exc:
        raise ImproperlyConfigured(f"{where}: bad regex {pattern!r}: {exc}") from exc


def _resolve(raw) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = Path(getattr(settings, "BASE_DIR", None) or ".") / path
    return path


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ImproperlyConfigured(f"{SETTING}: no such file {path}") from exc
    except (OSError, ValueError) as exc:
        raise ImproperlyConfigured(f"{SETTING}: cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ImproperlyConfigured(f"{path}: expected a JSON object with \"version\": 1")
    return data


def _build() -> Rules:
    files = [DEFAULTS_PATH]
    # Settings may be unconfigured in a standalone script; it then gets the
    # defaults unless it calls settings.configure(GOLDEN_SEO_PATTERN_FILES=...).
    configured = (getattr(settings, SETTING, None) if settings.configured else None) or []
    if isinstance(configured, (str, Path)):
        raise ImproperlyConfigured(f"{SETTING} must be a list of paths, not a single path")
    files += [_resolve(f) for f in configured]

    patterns: list[BoilerplatePattern] = []
    seen: dict[str, str] = {}
    lists: dict[str, list[str]] = {k: [] for k in _TRUNCATION_LISTS}
    terminal = ""
    for path in files:
        data = _read(path)
        for i, raw in enumerate(data.get("patterns") or []):
            where = f"{path.name} patterns[{i}]"
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
                raise ImproperlyConfigured(f"{where}: needs a string id")
            pid = raw["id"]
            if pid in seen:
                raise ImproperlyConfigured(f"{where}: duplicate pattern id {pid!r} (also in {seen[pid]})")
            seen[pid] = path.name
            applies = tuple(raw.get("applies_to") or ())
            if not applies or any(k not in KINDS for k in applies):
                raise ImproperlyConfigured(f"{where} ({pid}): applies_to must be a non-empty subset of {KINDS}")
            if raw.get("severity") not in SEVERITIES:
                raise ImproperlyConfigured(f"{where} ({pid}): severity must be one of {SEVERITIES}")
            examples = raw.get("examples") or []
            if not isinstance(examples, list) or not all(isinstance(e, str) for e in examples):
                raise ImproperlyConfigured(f"{where} ({pid}): examples must be a list of strings")
            patterns.append(BoilerplatePattern(
                id=pid,
                applies_to=applies,
                severity=raw["severity"],
                regex=_compile(raw.get("pattern"), raw.get("flags", ""), f"{where} ({pid})"),
                examples=tuple(examples),
                source=path.name,
            ))
        trunc = data.get("truncation") or {}
        if not isinstance(trunc, dict):
            raise ImproperlyConfigured(f"{path.name}: truncation must be an object")
        for key, value in trunc.items():
            if key.startswith("_") or key.startswith("examples_"):
                continue  # comments and the file's own test examples
            if key not in _TRUNCATION_KEYS:
                raise ImproperlyConfigured(f"{path.name}: unknown truncation key {key!r}")
            if key == "meta_terminal_chars":
                if not isinstance(value, str):
                    raise ImproperlyConfigured(f"{path.name}: meta_terminal_chars must be a string")
                terminal += "".join(c for c in value if c not in terminal)
                continue
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise ImproperlyConfigured(f"{path.name}: truncation.{key} must be a list of non-empty strings")
            for v in value:
                if v not in lists[key]:
                    lists[key].append(v)
    if not terminal:
        raise ImproperlyConfigured("seo_quality: meta_terminal_chars is empty")

    truncation = Truncation(
        strict_words=frozenset(w.lower() for w in lists["strict_dangling_words"]),
        soft_words=frozenset(w.lower() for w in lists["soft_dangling_words"]),
        title_bad_endings=tuple(lists["title_bad_endings"]),
        meta_terminal_chars=terminal,
        meta_patterns=tuple(_compile(p, "", f"truncation.meta_patterns {p!r}") for p in lists["meta_patterns"]),
    )
    return Rules(tuple(patterns), truncation, tuple(str(f) for f in files))


@lru_cache(maxsize=1)
def rules() -> Rules:
    """The merged rule set (golden defaults + ``GOLDEN_SEO_PATTERN_FILES``)."""
    return _build()


@receiver(setting_changed)
def _rebuild(*, setting, **kwargs):
    if setting in (SETTING, "BASE_DIR"):
        rules.cache_clear()


def patterns() -> tuple[BoilerplatePattern, ...]:
    return rules().patterns


def patterns_payload() -> list[dict]:
    """The template patterns as plain data, for a page script (``new RegExp(pattern, flags)``)."""
    return [
        {"id": p.id, "applies_to": list(p.applies_to), "severity": p.severity,
         "pattern": p.regex.pattern, "flags": "i" if p.regex.flags & re.IGNORECASE else ""}
        for p in patterns()
    ]


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")


def boilerplate_matches(text: str | None, kind: str) -> list[BoilerplatePattern]:
    """Every template pattern of ``kind`` that matches ``text`` (empty list if none)."""
    _check_kind(kind)
    if not text or not text.strip():
        return []
    value = text.strip()
    return [p for p in patterns() if kind in p.applies_to and p.regex.search(value)]


def is_boilerplate_seo(text: str | None, kind: str) -> bool:
    """True when ``text`` contains generated template copy (either severity)."""
    return bool(boilerplate_matches(text, kind))


def is_template_seo(text: str | None, kind: str) -> bool:
    """True when the WHOLE value is template filler (severity ``template``)."""
    return any(p.severity == SEVERITY_TEMPLATE for p in boilerplate_matches(text, kind))


_LAST_WORD = re.compile(r"(\S+)\s*$")


def _last_word(value: str, trunc: Truncation, kind: str) -> str:
    body = value if kind == "title" else value.rstrip(trunc.meta_terminal_chars).rstrip()
    last = _LAST_WORD.search(body)
    return last.group(1).lower() if last else ""


def is_cut_off(text: str | None, kind: str = "meta") -> bool:
    """True when ``text`` certainly stops mid-sentence (strict).

    A title ending on a separator ("...for Solar, Golf,") or a connector word; a
    meta whose last word is a connector ("...support, and.") or that matches a
    configured cut shape. Cut-off values carry nothing worth keeping and may
    be blanked. Empty text is not cut off.
    """
    _check_kind(kind)
    if not text or not text.strip():
        return False
    value = text.strip()
    trunc = rules().truncation
    if kind == "title":
        return value.endswith(trunc.title_bad_endings) or _last_word(value, trunc, kind) in trunc.strict_words
    if not value.rstrip(trunc.meta_terminal_chars).strip():
        return True
    if any(p.search(value) for p in trunc.meta_patterns):
        return True
    return value[-1] in trunc.meta_terminal_chars and _last_word(value, trunc, kind) in trunc.strict_words


def is_truncated(text: str | None, kind: str = "meta") -> bool:
    """True when ``text`` is cut off OR may be (soft).

    Soft signals: a meta with no closing punctuation, or a last word that is a
    configured cut point. Honest copy sometimes ends that way ("15kW output"),
    so a soft-only value may be *replaced* by better copy but is never blanked
    (see ``is_unusable``).
    """
    if is_cut_off(text, kind):
        return True
    if not text or not text.strip():
        return False
    value = text.strip()
    trunc = rules().truncation
    if kind == "meta" and value[-1] not in trunc.meta_terminal_chars:
        return True
    return _last_word(value, trunc, kind) in trunc.soft_words


def is_unusable(text: str | None, kind: str) -> bool:
    """Template filler or certainly cut off: replaceable, and safe to blank."""
    return is_template_seo(text, kind) or is_cut_off(text, kind)


def is_replaceable(text: str | None, kind: str) -> bool:
    """True when better copy may overwrite ``text``: empty, boilerplate or truncated.

    Anything else is real copy and must never be overwritten.
    """
    if not text or not text.strip():
        return True
    return is_boilerplate_seo(text, kind) or is_truncated(text, kind)


def problems(text: str | None, kind: str) -> list[str]:
    """Human-readable list of problems with ``text`` (for reports and forms)."""
    _check_kind(kind)
    if not text or not text.strip():
        return []
    out = [f"boilerplate ({p.severity}: {p.id})" for p in boilerplate_matches(text, kind)]
    if is_cut_off(text, kind):
        out.append("truncated mid-sentence")
    elif is_truncated(text, kind):
        out.append("may be truncated (no closing punctuation or a cut-point last word)")
    limit = TITLE_MAX if kind == "title" else META_MAX
    if len(text.strip()) > limit:
        out.append(f"{len(text.strip())} chars (over {limit})")
    return out
