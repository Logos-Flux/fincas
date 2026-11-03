"""Tabler alerts carry their own text colour (release harness R11.01,
``R11-dashboard-alert-warning-contrast``).

``.alert`` in Tabler is a 10 % tint of the alert colour on the surface, so
an Argon-era ``text-white`` on it put white text on a pale background and
failed axe colour contrast (the dashboard's "N overdue quotes"). The theme
token was never wrong; the markup forced the colour. Keep ``text-white``
off every ``alert-*`` in the back office.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase

from golden_erp.paths import TEMPLATES_DIR

PATTERN = re.compile(r'class="[^"]*\balert-(?:primary|secondary|success|danger|warning|info|light|dark)\b[^"]*\btext-white\b')


class AlertMarkupTests(SimpleTestCase):
    def test_no_alert_forces_white_text(self):
        hits = []
        for path in Path(TEMPLATES_DIR).rglob("*.html"):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if PATTERN.search(line):
                    hits.append(f"{path.relative_to(TEMPLATES_DIR)}:{n}")
        self.assertEqual(hits, [], "alerts with text-white (fails contrast on Tabler's tinted alerts)")
