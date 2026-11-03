"""No Django template may contain a multi-line {# #} comment.

Django's {# #} comment is SINGLE-LINE only. A multi-line one is not a
comment at all -- the tag is emitted verbatim into the output. Inside a
<style> block that is invalid CSS; in the body it is visible junk text on
the page.

Both shipped. `{# Send forms, deliberately siblings of ... #}` rendered on
every quote and sales-order edit page in production, and a multi-line
comment inside base_document.html's <style> printed into the PDF stylesheet.
Neither raised anything -- they just render.

Use {% comment %}...{% endcomment %} for multi-line, or /* */ inside <style>.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

APP_ROOT = Path(settings.BASE_DIR)


def _template_files():
    seen = set()
    for path in APP_ROOT.rglob("*.html"):
        parts = set(path.parts)
        if parts & {".venv", "node_modules", "staticfiles", "static"}:
            continue
        if path not in seen:
            seen.add(path)
            yield path


class TemplateCommentHygieneTests(SimpleTestCase):
    def test_no_multiline_django_comments(self):
        offenders = []
        for path in _template_files():
            try:
                src = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for match in re.finditer(r"\{#", src):
                tail = src[match.start():]
                end = tail.find("#}")
                if end == -1 or "\n" in tail[:end]:
                    line = src[: match.start()].count("\n") + 1
                    offenders.append(
                        f"{path.relative_to(APP_ROOT)}:{line}"
                    )
        self.assertEqual(
            offenders, [],
            "Multi-line {# #} is not a comment in Django — it renders "
            "verbatim into the page. Use {% comment %}...{% endcomment %}, "
            "or /* */ inside <style>. Offenders: " + ", ".join(offenders),
        )

    def test_the_check_actually_detects_one(self):
        # Guard against the regex silently matching nothing forever.
        sample = "ok {# one line #}\nbad {# start\nstill going #}\n"
        found = []
        for match in re.finditer(r"\{#", sample):
            tail = sample[match.start():]
            end = tail.find("#}")
            if end == -1 or "\n" in tail[:end]:
                found.append(match.start())
        self.assertEqual(len(found), 1)
