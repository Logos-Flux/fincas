"""Unit tests for ``scripts/findings_check.py`` (release-hardening plan 9.10).

The script is loaded by path (it is not a package), fed temporary logs,
and judged by its exit code and output. No database, no Django settings.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "findings_check.py"

HEADER = (
    "| F-id | severity | original severity | re-review trigger | status | SHA first seen "
    "| SHA fixed | journey or route | role | repro steps | artefact path | OpenProject link |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
)


def row(
    fid="F-001",
    severity="S2",
    original="S2",
    trigger="–",
    status="fixed",
    first="abc1234",
    fixed="–",
    journey="R1",
    role="staff",
    repro="click save twice",
    artefact="–",
    op="–",
):
    cells = [fid, severity, original, trigger, status, first, fixed, journey, role, repro, artefact, op]
    return "| " + " | ".join(cells) + " |\n"


def load_script():
    spec = importlib.util.spec_from_file_location("findings_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FindingsCheckTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mod = load_script()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, body: str, preamble: str = "# Findings log\n\nSome prose.\n\n") -> Path:
        path = Path(self.tmp.name) / "findings-log.md"
        path.write_text(preamble + HEADER + body, encoding="utf-8")
        return path

    def run_check(self, path: Path, *extra: str):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.mod.main(["--path", str(path), *extra])
        return code, out.getvalue()

    # -- passes -----------------------------------------------------------

    def test_missing_file_passes(self):
        code, out = self.run_check(Path(self.tmp.name) / "absent.md")
        self.assertEqual(code, 0)
        self.assertIn("no findings log yet", out)

    def test_good_log_passes(self):
        path = self.write(
            row("F-001", journey="R1")
            + row("F-002", severity="S1", original="S1", status="open", journey="/erp/orders/")
            + row(
                "F-003",
                severity="S2",
                original="S0",
                trigger="any deployment with more than one Company",
                status="not supported by design",
                journey="/erp/ dashboard, J4",
            )
            + row("F-004", status="accepted", trigger="a second tenant", repro="log in as B, open A's order")
        )
        code, out = self.run_check(path)
        self.assertEqual(code, 0, out)
        self.assertIn("4 rows ok", out)

    def test_summary_prints_counts(self):
        path = self.write(row("F-001") + row("F-002", severity="S1", original="S1", status="open"))
        code, out = self.run_check(path, "--summary")
        self.assertEqual(code, 0, out)
        self.assertIn("2 findings", out)
        self.assertIn("S1 1", out)
        self.assertIn("open 1", out)
        self.assertIn("open S0/S1: F-002", out)

    def test_journey_token_inside_free_text_is_checked(self):
        path = self.write(row("F-001", journey="`POST /api/v1/orders/` on R99"))
        code, out = self.run_check(path)
        self.assertEqual(code, 1)
        self.assertIn("R99", out)

    def test_real_log_passes_when_present(self):
        real = SCRIPT.parent.parent / "docs" / "findings-log.md"
        if not real.exists():
            self.skipTest("docs/findings-log.md not present")
        code, out = self.run_check(real)
        self.assertEqual(code, 0, out)

    # -- each failure kind ------------------------------------------------

    def assert_fails(self, body: str, mention: str):
        path = self.write(body)
        code, out = self.run_check(path)
        self.assertEqual(code, 1, out)
        self.assertIn(mention, out)

    def test_wrong_column_count_fails(self):
        self.assert_fails("| F-001 | S2 | S2 | – | fixed | abc1234 |\n", "F-001")

    def test_unknown_severity_fails(self):
        self.assert_fails(row(severity="S9"), "S9")

    def test_unknown_original_severity_fails(self):
        self.assert_fails(row(original="high"), "high")

    def test_unknown_status_fails(self):
        self.assert_fails(row(status="wontfix"), "wontfix")

    def test_bad_id_pattern_fails(self):
        self.assert_fails(row(fid="F-12"), "F-12")
        self.assert_fails(row(fid="F-1234"), "F-1234")

    def test_duplicate_id_fails(self):
        self.assert_fails(row("F-007") + row("F-007"), "duplicate")

    def test_journey_out_of_range_fails(self):
        self.assert_fails(row(journey="R13"), "R13")
        self.assert_fails(row(journey="J5"), "J5")

    def test_accepted_without_trigger_fails(self):
        self.assert_fails(row(status="accepted", trigger="–", repro="steps"), "re-review trigger")

    def test_accepted_without_repro_fails(self):
        self.assert_fails(row(status="accepted", trigger="a second tenant", repro=""), "repro steps")

    def test_wrong_header_fails(self):
        path = Path(self.tmp.name) / "findings-log.md"
        path.write_text("| F-id | severity |\n|---|---|\n| F-001 | S2 |\n", encoding="utf-8")
        code, out = self.run_check(path)
        self.assertEqual(code, 1)
        self.assertIn("header", out)

    def test_no_table_fails(self):
        path = Path(self.tmp.name) / "findings-log.md"
        path.write_text("# Findings log\n\nnothing here\n", encoding="utf-8")
        code, out = self.run_check(path)
        self.assertEqual(code, 1)
        self.assertIn("no table", out)

    # -- the cutover bar ---------------------------------------------------

    def test_bar_cutover_fails_on_open_s1(self):
        path = self.write(row("F-001") + row("F-002", severity="S1", original="S1", status="open"))
        code, out = self.run_check(path, "--bar", "cutover")
        self.assertEqual(code, 1)
        self.assertIn("F-002", out)
        self.assertIn("FAIL", out)

    def test_bar_cutover_fails_on_accepted_s0(self):
        path = self.write(
            row("F-001", severity="S0", original="S0", status="accepted", trigger="x", repro="y")
        )
        code, out = self.run_check(path, "--bar", "cutover")
        self.assertEqual(code, 1)
        self.assertIn("F-001", out)

    def test_bar_cutover_passes_when_s0_s1_closed(self):
        path = self.write(
            row("F-001", severity="S1", original="S1", status="fixed")
            + row("F-002", severity="S2", original="S2", status="open")
            + row("F-003", severity="S2", original="S0", status="not supported by design", trigger="t")
        )
        code, out = self.run_check(path, "--bar", "cutover")
        self.assertEqual(code, 0, out)
        self.assertIn("no open S0/S1", out)

    def test_bar_off_by_default(self):
        path = self.write(row("F-002", severity="S1", original="S1", status="open"))
        code, _ = self.run_check(path)
        self.assertEqual(code, 0)
