"""Soft checks on the SEO copy staff type into Brand and Category.

The ERP is the only place SEO copy is written (docs/storefront-seo.md), so it
is also the only place it can be caught when it looks machine-made. These are
warnings, never errors: they are flashed after a save, and the row is saved
either way. Two shapes have hurt a live shop before:

* **templated** copy: one sentence stamped onto every row with only the name
  swapped ("Shop {name} Equipment | Store"), which search engines read as
  duplicate pages;
* **truncated** copy: a description cut at a character count, ending
  mid-sentence or in an ellipsis, which the storefront then shows verbatim.
"""

from __future__ import annotations

import re

from . import seo_quality

#: Lengths search engines show before cutting (guidance, not a hard rule).
TITLE_MAX = seo_quality.TITLE_MAX
META_MIN = 50
META_MAX = seo_quality.META_MAX

SEO_TITLE_HELP = (
    f"Shown as the browser tab and search-result title. Aim for {TITLE_MAX} characters or "
    "fewer. Leave empty to use the name followed by the store name."
)
SEO_META_HELP = (
    f"Search-result summary, {META_MIN}-{META_MAX} characters, written as whole sentences. "
    "Leave empty to use the first sentences of the description."
)
SEO_OG_TITLE_HELP = "Title used when the page is shared on social media. Empty: the SEO title."
SEO_OG_DESCRIPTION_HELP = "Text used when the page is shared. Empty: the meta description."
SEO_CANONICAL_HELP = "Only set this when another URL is the preferred copy of this page."

_PLACEHOLDER_RE = re.compile(
    r"\{\{|\}\}|\{[A-Za-z_][A-Za-z0-9_]*\}|%s|%\([A-Za-z_]+\)s|\[(?:name|title|brand|category)\]"
    r"|<(?:name|title|brand|category)>",
    re.IGNORECASE,
)
_ELLIPSIS_RE = re.compile(r"(\.\.\.|…)\s*$")
_SENTENCE_END_RE = re.compile(r"[.!?][\"'”’)\]]*\s*$")
_NAME_MARK = "\u0000name\u0000"


def _shape(text: str, name: str) -> str:
    """The text with its own row's name replaced by a marker, whitespace folded."""
    folded = " ".join((text or "").split())
    if name:
        folded = re.sub(re.escape(name.strip()), _NAME_MARK, folded, flags=re.IGNORECASE)
    return folded.casefold()


def _label(field: str) -> str:
    return {
        "seo_title": "SEO title",
        "seo_meta_description": "Meta description",
        "seo_og_title": "Social title",
        "seo_og_description": "Social description",
    }.get(field, field)


def seo_copy_warnings(instance) -> list[str]:
    """Human-readable warnings for one Brand or Category (empty list: looks fine)."""
    warnings: list[str] = []
    name = (getattr(instance, "title", "") or "").strip()
    fields = ("seo_title", "seo_meta_description", "seo_og_title", "seo_og_description")
    for field in fields:
        text = (getattr(instance, field, "") or "").strip()
        if not text:
            continue
        label = _label(field)
        if _PLACEHOLDER_RE.search(text):
            warnings.append(f"{label} contains a template placeholder: {text!r}.")
        kind = seo_quality.FIELD_KINDS[field]
        for pattern in seo_quality.boilerplate_matches(text, kind):
            warnings.append(
                f"{label} matches this shop's generated-copy pattern {pattern.id!r} "
                f"({pattern.severity}). Write copy specific to this page."
            )
        if _ELLIPSIS_RE.search(text):
            warnings.append(f"{label} ends in an ellipsis, so it looks cut off. Rewrite it to end on a full sentence.")
        elif seo_quality.is_cut_off(text, kind):
            warnings.append(f"{label} stops mid-sentence (it ends on a connector or separator). Finish the sentence.")
        elif field.endswith("description") and not _SENTENCE_END_RE.search(text):
            warnings.append(f"{label} does not end with a full stop, so it may be cut off mid-sentence.")
        if field.endswith("title") and len(text) > TITLE_MAX:
            warnings.append(f"{label} is {len(text)} characters; search results show about {TITLE_MAX}.")
        if field == "seo_meta_description":
            if len(text) > META_MAX:
                warnings.append(f"{label} is {len(text)} characters; search results show about {META_MAX}.")
            elif len(text) < META_MIN:
                warnings.append(f"{label} is only {len(text)} characters; aim for {META_MIN}-{META_MAX}.")
    warnings.extend(_shared_template_warnings(instance, name, fields))
    return warnings


def _shared_template_warnings(instance, name: str, fields) -> list[str]:
    """Warn when another row of the same model has the same copy, name swapped."""
    model = type(instance)
    own = {f: _shape(getattr(instance, f, ""), name) for f in fields if (getattr(instance, f, "") or "").strip()}
    if not own:
        return []
    others = model.objects.exclude(pk=instance.pk).values("title", *own.keys())
    matches: dict[str, list[str]] = {}
    for row in others.iterator():
        for field, shape in own.items():
            if row[field] and _shape(row[field], row["title"] or "") == shape:
                matches.setdefault(field, []).append(row["title"])
    warnings = []
    for field, titles in matches.items():
        noun = model._meta.verbose_name if len(titles) == 1 else model._meta.verbose_name_plural
        shown = ", ".join(sorted(titles)[:3]) + (" and more" if len(titles) > 3 else "")
        warnings.append(
            f"{_label(field)} reads like a template: {len(titles)} other {noun} "
            f"({shown}) use the same words with only the name changed. "
            "Write copy specific to this page."
        )
    return warnings
