"""Best-effort first-page text extract from a PDF buffer.

No network. No side effects. All failures return empty string — callers
treat missing text the same as no match.
"""

from __future__ import annotations

import logging
from io import BytesIO

logger = logging.getLogger(__name__)

# Keep extraction cheap: we only look at the first N pages because the
# identifiers (SKU, MPN, model numbers) sit on the title page.
_MAX_PAGES = 2
_MAX_CHARS = 8000


def extract_first_page_text(pdf_bytes: bytes) -> str:
    """Return up to ~8k chars from the first 2 pages of *pdf_bytes*.

    Returns ``""`` on any failure (non-PDF, encrypted, truncation, unreadable).
    """
    if not pdf_bytes:
        return ""
    try:
        from pypdf import PdfReader  # imported lazily — not needed for scoring-only runs
    except ImportError:
        logger.debug("text_extract: pypdf not installed, skipping")
        return ""
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        if getattr(reader, "is_encrypted", False):
            return ""
        parts: list[str] = []
        for page in list(reader.pages)[:_MAX_PAGES]:
            try:
                text = page.extract_text() or ""  # type: ignore[attr-defined]
            except Exception:
                text = ""
            if text:
                parts.append(text)
            if sum(len(p) for p in parts) >= _MAX_CHARS:
                break
        combined = "\n".join(parts).strip()
        if len(combined) > _MAX_CHARS:
            combined = combined[:_MAX_CHARS]
        return combined
    except Exception:
        logger.debug("text_extract: failed to parse PDF (%d bytes)", len(pdf_bytes), exc_info=True)
        return ""
