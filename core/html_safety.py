"""HTML-safety helpers for ERP views (M3 Track G, S4).

Two distinct sinks need two distinct guards:

- ``display_html`` builds an HTML fragment from user-controlled field
  values that the document form later injects via ``innerHTML``. Values
  must be escaped; the ``<br>`` separators we add ourselves stay raw.
- ``dumps_script_safe`` serializes context JSON that templates embed
  inside a ``<script>`` block via ``{{ ...|safe }}``. Escaping
  ``<``/``>``/``&`` as ``\\uXXXX`` keeps the HTML parser from seeing a
  literal ``</script>`` while remaining transparent to ``JSON.parse``
  (unicode escapes decode back to the same characters at runtime).
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from django.utils.html import escape


def display_html(lines: Iterable[str | None]) -> str:
    """Join truthy lines into an innerHTML-safe fragment."""
    return "<br>".join(escape(line) for line in lines if line)


def dumps_script_safe(obj: Any) -> str:
    """JSON-encode for embedding inside an HTML <script> block."""
    return json.dumps(obj).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
