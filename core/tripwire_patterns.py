"""The isolation tripwire's pattern files, read from Python.

``scripts/tripwire.sh`` checks the repository against every
``scripts/release/tripwire-*.txt`` it finds (format in its header): an
``[all]`` section checked in every tracked file and a ``[shipping]`` section
checked in the files that reach the public tree. The development repository
keeps its downstream and estate names in a dev-only file there, so shipped
code never spells them; a checkout without pattern files has nothing to
check. This module reads the same files for code that must not repeat those
names either: the release notes (:mod:`core.release_notes`) and the
"no instance vocabulary" tests.

Patterns are extended regexes in the subset Python's ``re`` shares, matched
case-insensitively. Touches no database and no setting.
"""

from __future__ import annotations

import re
from pathlib import Path

PATTERN_GLOB = "scripts/release/tripwire-*.txt"
SECTIONS = ("all", "shipping")


def load(root) -> dict[str, list[str]]:
    """``{"all": [...], "shipping": [...]}`` from every pattern file under ``root``."""
    found: dict[str, list[str]] = {name: [] for name in SECTIONS}
    for path in sorted(Path(root).glob(PATTERN_GLOB)):
        section = "all"
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                if section not in found:
                    raise ValueError(f"{path}: unknown section {line}")
                continue
            found[section].append(line)
    return found


def compile_all(root) -> re.Pattern | None:
    """One case-insensitive regex for every pattern in every section, or None."""
    patterns = [p for section in SECTIONS for p in load(root)[section]]
    if not patterns:
        return None
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)
