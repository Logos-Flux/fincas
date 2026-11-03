"""Fixture plumbing for the HubSpot adapter tests.

``fixtures/crawl-small`` (a recorded crawl) and ``fixtures/export-csv`` (a
file export) are complete inputs with their canonical output under
``expected/``. ``fixtures/cases/<name>/`` are overlays on one of them, each
triggering one limit of spec 2.13:

- ``case.json``: ``description``, ``base``, optional ``remove`` (paths
  deleted from the copy), ``kwargs`` (export keyword arguments), and either
  ``refuses`` (a substring of the expected ``AdapterError``) or ``added`` (the
  coverage reason codes, per object type, the case adds to its base's
  manifest; nothing else may change) and optional ``history``;
- ``overlay/``: files copied over the base;
- ``expected/manifest.expected.json``: the manifest (its sha256 digests pin
  every canonical file byte for byte).

Set ``HUBSPOT_EXPORT_UPDATE_EXPECTED=1`` to rewrite every ``expected/`` file
from the current adapter, then review the diff.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CASES = FIXTURES / "cases"
BASES = {
    "crawl-small": {"time_zone": None},
    "export-csv": {"time_zone": "America/New_York"},
}
UPDATE = os.environ.get("HUBSPOT_EXPORT_UPDATE_EXPECTED") == "1"


def expected_name(file_name: str) -> str:
    stem, dot, ext = file_name.partition(".")
    return f"{stem}.expected.{ext}"


def case_dirs() -> list[Path]:
    return sorted(p for p in CASES.iterdir() if p.is_dir())


def case_spec(case: Path) -> dict:
    return json.loads((case / "case.json").read_text(encoding="utf-8"))


def copy_input(source: Path, out: Path) -> Path:
    shutil.copytree(source, out, ignore=shutil.ignore_patterns("expected"))
    return out


def build_case(case: Path, out: Path) -> tuple[Path, dict]:
    """The case's input directory (base + removals + overlay) and its export kwargs."""
    spec = case_spec(case)
    copy_input(FIXTURES / spec["base"], out)
    for rel in spec.get("remove", []):
        target = out / rel
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    overlay = case / "overlay"
    if overlay.is_dir():
        for path in sorted(overlay.rglob("*")):
            if path.is_file():
                dest = out / path.relative_to(overlay)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
    kwargs = {"mapping": None, **BASES[spec["base"]], **spec.get("kwargs", {})}
    return out, kwargs


def reason_codes(manifest: dict) -> dict[str, set[str]]:
    return {otype: {r["code"] for r in entry.get("reasons", [])} for otype, entry in manifest["objects"].items()}
