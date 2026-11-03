"""
Auto-match engine for Tigris product docs → products.

Pure-python scoring. No I/O, no ORM, fully testable without Django.

Tier order (highest wins):
  1. SKU exact — the Tigris key or PDF title page contains a variant SKU verbatim.
  2. MPN exact — key/text contains a variant or product MPN.
  3. Brand + family token — key/text contains a brand slug/title AND a
     product token (title word, part_number, or slug fragment).
  4. Model substring — key/text contains a word-shaped substring of a SKU/MPN
     that is long enough to be distinguishing (>=5 chars after stripping).

Each tier carries a fixed confidence so downstream callers can gate on a
single number without parsing match_type:

  SKU exact         0.95
  MPN exact         0.88
  brand+family      0.65
  model substring   0.40
  no match          0.00 (no candidate emitted)

A single Tigris key may match multiple products; the engine returns the
best-scoring candidate per key, breaking ties by product title length
(longer = more specific, mirroring fixture intent).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Tunables — confidence per tier
# ---------------------------------------------------------------------------

CONF_SKU = 0.95
CONF_MPN = 0.88
CONF_BRAND_FAMILY = 0.65
CONF_MODEL_SUBSTR = 0.40

# Only a SKU/MPN whose alphanumeric core is at least this long qualifies
# for the "model substring" tier (short substrings are noise).
MIN_SUBSTR_LEN = 5

# For the brand+family tier we require a family token of at least this many
# characters so we don't match on "a" / "pro" / "2".
MIN_FAMILY_TOKEN = 4

# Ranking for match_type when confidences tie (higher wins).
_RANK = {
    "sku": 4,
    "mpn": 3,
    "brand_family": 2,
    "model_substr": 1,
}

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoredCandidate:
    product_id: object  # UUID or str in prod; str/int in tests
    product_title: str
    brand_title: str
    brand_slug: str
    confidence: float
    match_type: str  # sku | mpn | brand_family | model_substr
    matched_token: str
    matched_sources: tuple[str, ...]  # subset of {"key", "pdf_text"}
    reason: str

    def detail_dict(self) -> dict:
        return {
            "match_type": self.match_type,
            "matched_token": self.matched_token,
            "matched_sources": list(self.matched_sources),
            "reason": self.reason,
            "brand_title": self.brand_title,
            "product_title": self.product_title,
        }


@dataclass
class IndexEntry:
    """Pre-built lookup for one product (+ its variants)."""

    product_id: object
    product_title: str
    brand_title: str
    brand_slug: str
    part_number: str
    product_mpn: str
    sku_set: set[str] = field(default_factory=set)  # normalized SKUs
    mpn_set: set[str] = field(default_factory=set)  # normalized MPNs
    family_tokens: set[str] = field(default_factory=set)  # lowercased words
    sku_substrs: set[str] = field(default_factory=set)  # alnum cores >= MIN_SUBSTR_LEN


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

_ALNUM = re.compile(r"[a-z0-9]+")
_WORD = re.compile(r"[a-z0-9][a-z0-9\-]*")


def _norm(s: str) -> str:
    """Lowercase + collapse whitespace/punct to single space, trimmed."""
    if not s:
        return ""
    parts = _ALNUM.findall(s.lower())
    return " ".join(parts)


def _alnum_core(s: str) -> str:
    """Longest alphanum run in s, lowercased (for model-substr tier)."""
    if not s:
        return ""
    runs = _ALNUM.findall(s.lower())
    if not runs:
        return ""
    return max(runs, key=len)


def _slug_tokens(s: str) -> set[str]:
    if not s:
        return set()
    return {t for t in _ALNUM.findall(s.lower()) if len(t) >= MIN_FAMILY_TOKEN}


def _title_words(title: str) -> set[str]:
    if not title:
        return set()
    return {w for w in _ALNUM.findall(title.lower()) if len(w) >= MIN_FAMILY_TOKEN}


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------

def build_index(entries: list[dict]) -> list[IndexEntry]:
    """Build IndexEntry list from raw product dicts.

    Each *entry* dict expects:
      product_id: str|UUID
      product_title: str
      brand_title: str
      brand_slug: str
      part_number: str
      product_mpn: str
      variant_skus: list[str]
      variant_mpns: list[str]
    Missing keys default to "" / [].
    """
    out: list[IndexEntry] = []
    for e in entries:
        pid = e.get("product_id")
        title = (e.get("product_title") or "").strip()
        brand_title = (e.get("brand_title") or "").strip()
        brand_slug = (e.get("brand_slug") or "").strip()
        part_number = (e.get("part_number") or "").strip()
        product_mpn = (e.get("product_mpn") or "").strip()
        variant_skus: list[str] = list(e.get("variant_skus") or [])
        variant_mpns: list[str] = list(e.get("variant_mpns") or [])

        sku_set = {s.strip() for s in variant_skus if s and s.strip()}
        # Also consider the parent part_number as an SKU-equivalent for the
        # exact-SKU tier (standalone products carry their code there).
        if part_number:
            sku_set.add(part_number)

        mpn_set = {m.strip() for m in variant_mpns if m and m.strip()}
        if product_mpn:
            mpn_set.add(product_mpn)

        # Family tokens: words from title + slug + part_number.
        family: set[str] = set()
        family.update(_title_words(title))
        family.update(_slug_tokens(e.get("product_slug") or ""))
        if part_number:
            family.update(_slug_tokens(part_number))

        # SKU/MPN substring cores for the loose tier.
        substrs: set[str] = set()
        for raw in list(sku_set) + list(mpn_set):
            core = _alnum_core(raw)
            if len(core) >= MIN_SUBSTR_LEN:
                substrs.add(core)

        out.append(IndexEntry(
            product_id=pid,
            product_title=title,
            brand_title=brand_title,
            brand_slug=brand_slug,
            part_number=part_number,
            product_mpn=product_mpn,
            sku_set=sku_set,
            mpn_set=mpn_set,
            family_tokens=family,
            sku_substrs=substrs,
        ))
    return out


# ---------------------------------------------------------------------------
# Token bag from a Tigris key + optional PDF text
# ---------------------------------------------------------------------------

def _token_bag(key: str, pdf_text: str) -> tuple[str, str]:
    """Return (key_norm, pdf_norm) — normalized bags for substring search."""
    return _norm(key), _norm(pdf_text or "")


def _contains_token(bag: str, token: str) -> bool:
    """Whole-token match: bag contains normalized token as substring.

    We normalize the token the same way as the bag, so punctuation/dash
    differences don't break matches (e.g. IQ8HC-72 vs iq8hc 72).
    """
    if not bag or not token:
        return False
    tok = _norm(token)
    if not tok:
        return False
    return tok in bag


def _brand_present(key_norm: str, pdf_norm: str, entry: IndexEntry) -> tuple[bool, str]:
    """True if brand slug or token appears in key/pdf. Returns (present, source)."""
    sources: list[str] = []
    # Try brand slug first (most distinctive).
    for cand in (entry.brand_slug, entry.brand_title.lower() if entry.brand_title else ""):
        if not cand:
            continue
        slug_norm = _norm(cand)
        if not slug_norm:
            continue
        # For brand_title we match on any long word, not the whole normalized title.
        if cand == entry.brand_title.lower():
            words = [w for w in cand.lower().split() if len(w) >= 3]
            matched = any(_norm(w) in key_norm or _norm(w) in pdf_norm for w in words)
        else:
            matched = slug_norm in key_norm or slug_norm in pdf_norm
        if matched:
            if slug_norm in key_norm:
                sources.append("key")
            if slug_norm in pdf_norm:
                sources.append("pdf_text")
            return True, "+".join(sources) if sources else "key"
    return False, ""


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_one(key: str, pdf_text: str, index: list[IndexEntry]) -> list[ScoredCandidate]:
    """Score *key* (+ *pdf_text*) against every entry in *index*.

    Returns candidates sorted best-first (highest confidence, then rank, then
    title specificity). Empty list when nothing qualifies.
    """
    key_norm, pdf_norm = _token_bag(key, pdf_text)
    candidates: list[ScoredCandidate] = []

    for entry in index:
        best: ScoredCandidate | None = None

        # Tier 1 — exact SKU.
        for sku in entry.sku_set:
            srcs: list[str] = []
            if _contains_token(key_norm, sku):
                srcs.append("key")
            if _contains_token(pdf_norm, sku):
                srcs.append("pdf_text")
            if srcs:
                cand = ScoredCandidate(
                    product_id=entry.product_id,
                    product_title=entry.product_title,
                    brand_title=entry.brand_title,
                    brand_slug=entry.brand_slug,
                    confidence=CONF_SKU,
                    match_type="sku",
                    matched_token=sku,
                    matched_sources=tuple(srcs),
                    reason=f"SKU {sku!r} found in {'+'.join(srcs)}",
                )
                if best is None or (cand.confidence, _RANK[cand.match_type]) > (best.confidence, _RANK[best.match_type]):
                    best = cand
        # Short-circuit: SKU match outranks everything else for this product.
        if best is not None and best.match_type == "sku":
            candidates.append(best)
            continue

        # Tier 2 — exact MPN.
        for mpn in entry.mpn_set:
            srcs = []
            if _contains_token(key_norm, mpn):
                srcs.append("key")
            if _contains_token(pdf_norm, mpn):
                srcs.append("pdf_text")
            if srcs:
                cand = ScoredCandidate(
                    product_id=entry.product_id,
                    product_title=entry.product_title,
                    brand_title=entry.brand_title,
                    brand_slug=entry.brand_slug,
                    confidence=CONF_MPN,
                    match_type="mpn",
                    matched_token=mpn,
                    matched_sources=tuple(srcs),
                    reason=f"MPN {mpn!r} found in {'+'.join(srcs)}",
                )
                if best is None or (cand.confidence, _RANK[cand.match_type]) > (best.confidence, _RANK[best.match_type]):
                    best = cand
        if best is not None and best.match_type == "mpn":
            candidates.append(best)
            continue

        # Tier 3 — brand + family token (requires both signals).
        has_brand, brand_src = _brand_present(key_norm, pdf_norm, entry)
        if has_brand and entry.family_tokens:
            for tok in entry.family_tokens:
                srcs = []
                if tok in key_norm:
                    srcs.append("key")
                if tok in pdf_norm:
                    srcs.append("pdf_text")
                if not srcs:
                    continue
                cand = ScoredCandidate(
                    product_id=entry.product_id,
                    product_title=entry.product_title,
                    brand_title=entry.brand_title,
                    brand_slug=entry.brand_slug,
                    confidence=CONF_BRAND_FAMILY,
                    match_type="brand_family",
                    matched_token=tok,
                    matched_sources=tuple(srcs),
                    reason=f"brand {entry.brand_title!r} + family token {tok!r} in {'+'.join(srcs)}",
                )
                if best is None or (cand.confidence, _RANK[cand.match_type]) > (best.confidence, _RANK[best.match_type]):
                    best = cand
                    break  # one family token is enough
        if best is not None and best.match_type == "brand_family":
            candidates.append(best)
            continue

        # Tier 4 — model substring (loose, low confidence).
        for core in entry.sku_substrs:
            tok = _norm(core)
            if tok in key_norm or tok in pdf_norm:
                srcs = []
                if tok in key_norm:
                    srcs.append("key")
                if tok in pdf_norm:
                    srcs.append("pdf_text")
                cand = ScoredCandidate(
                    product_id=entry.product_id,
                    product_title=entry.product_title,
                    brand_title=entry.brand_title,
                    brand_slug=entry.brand_slug,
                    confidence=CONF_MODEL_SUBSTR,
                    match_type="model_substr",
                    matched_token=core,
                    matched_sources=tuple(srcs),
                    reason=f"model substring {core!r} in {'+'.join(srcs)}",
                )
                if best is None or (cand.confidence, _RANK[cand.match_type]) > (best.confidence, _RANK[best.match_type]):
                    best = cand
                    break
        if best is not None:
            candidates.append(best)

    # Rank all product candidates for this key.
    candidates.sort(key=lambda c: (c.confidence, _RANK[c.match_type], len(c.product_title)), reverse=True)
    return candidates


def best_match(key: str, pdf_text: str, index: list[IndexEntry]) -> ScoredCandidate | None:
    """Top candidate for *key*, or None."""
    cands = score_one(key, pdf_text, index)
    return cands[0] if cands else None


# ---------------------------------------------------------------------------
# Thresholds — the management command uses these to gate DB writes.
# ---------------------------------------------------------------------------

# HIGH-confidence threshold: only SKU/MPN tiers qualify.
HIGH_CONFIDENCE_MIN = 0.85

# REVIEW threshold: brand+family and above go to the CSV queue; below that is unassigned.
REVIEW_CONFIDENCE_MIN = 0.40

DOC_TYPE_FROM_KEY = {
    "spec": "datasheet",
    "spec_sheet": "datasheet",
    "datasheet": "datasheet",
    "manual": "manual",
    "install": "installation",
    "installation": "installation",
    "warr": "warranty",
    "warranty": "warranty",
    "compliance": "compliance",
    "cert": "compliance",
}


def infer_doc_type(key: str) -> str:
    """Heuristic document_type from key/filename."""
    low = key.lower()
    for hint, dtype in DOC_TYPE_FROM_KEY.items():
        if hint in low:
            return dtype
    return "datasheet"
