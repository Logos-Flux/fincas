#!/usr/bin/env python3
"""Check ``docs/findings-log.md`` (release-hardening plan, section 9.10).

Pure standard library, Python 3.12, safe under ``python -I``; no Django.

Exit 0 and print "no findings log yet" while the file is absent. Once it
exists, exit 1 naming the offending row on:

- a row with the wrong number of columns (the header fixes the count);
- a severity outside S0..S3, or a status outside the known set;
- an F-id that is not ``F-<three digits>``, or one that repeats;
- a "journey or route" cell naming a journey id outside R1..R12 / J1..J4
  (only tokens shaped like ``R7`` or ``J2`` are checked; free-text routes
  such as ``/erp/orders/`` pass);
- an ``accepted`` row whose "re-review trigger" or "repro steps" is empty.

``--bar cutover`` also fails when any S0/S1 row is ``open`` or ``accepted``
(candidate evidence item 2). ``--summary`` prints counts by severity and
status.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

DEFAULT_LOG = Path(__file__).resolve().parent.parent / "docs" / "findings-log.md"

COLUMNS = (
    "F-id",
    "severity",
    "original severity",
    "re-review trigger",
    "status",
    "SHA first seen",
    "SHA fixed",
    "journey or route",
    "role",
    "repro steps",
    "artefact path",
    "OpenProject link",
)
SEVERITIES = ("S0", "S1", "S2", "S3")
STATUSES = ("fixed", "open", "accepted", "not supported by design")
JOURNEYS = {f"R{n}" for n in range(1, 13)} | {f"J{n}" for n in range(1, 5)}
ID_RE = re.compile(r"^F-\d{3}$")
JOURNEY_TOKEN_RE = re.compile(r"^[RJ]\d+$")
EMPTY_CELLS = {"", "-", "–", "—"}
BAR_BLOCKING = {"open", "accepted"}


class LogError(Exception):
    """A row (or the table) that fails a check."""


def split_row(line: str) -> list[str]:
    """Split a markdown table line into stripped cells.

    The outer pipes are dropped; a cell may not contain a pipe (the log's
    header says so), so no escape handling is attempted.
    """
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [cell.strip() for cell in inner.split("|")]


def is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", c) for c in cells)


def parse(text: str) -> list[dict[str, str]]:
    """Return the findings table as a list of dicts keyed by column name."""
    rows: list[dict[str, str]] = []
    in_table = False
    header_seen = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line.startswith("|"):
            if in_table:
                break
            continue
        cells = split_row(line)
        if not in_table:
            if cells and cells[0] == COLUMNS[0]:
                if tuple(cells) != COLUMNS:
                    raise LogError(
                        f"line {lineno}: header columns are {cells}, expected {list(COLUMNS)}"
                    )
                in_table = True
                header_seen = True
            continue
        if is_separator(cells):
            continue
        if len(cells) != len(COLUMNS):
            label = cells[0] if cells else "?"
            raise LogError(
                f"line {lineno} ({label}): {len(cells)} columns, expected {len(COLUMNS)}"
            )
        row = dict(zip(COLUMNS, cells))
        row["_line"] = str(lineno)
        rows.append(row)
    if not header_seen:
        raise LogError(f"no table with header starting '| {COLUMNS[0]} |' found")
    return rows


def journey_tokens(cell: str) -> list[str]:
    tokens = re.split(r"[\s,;/()`]+", cell)
    return [t for t in tokens if JOURNEY_TOKEN_RE.match(t)]


def validate(rows: list[dict[str, str]]) -> list[str]:
    """Return every problem found, one line each (empty means clean)."""
    problems: list[str] = []
    seen: dict[str, str] = {}
    for row in rows:
        fid = row["F-id"]
        where = f"line {row['_line']} ({fid})"
        if not ID_RE.match(fid):
            problems.append(f"{where}: F-id does not match F-NNN")
        elif fid in seen:
            problems.append(f"{where}: duplicate F-id (first at line {seen[fid]})")
        else:
            seen[fid] = row["_line"]
        for col in ("severity", "original severity"):
            if row[col] not in SEVERITIES:
                problems.append(f"{where}: {col} {row[col]!r} not in {list(SEVERITIES)}")
        if row["status"] not in STATUSES:
            problems.append(f"{where}: status {row['status']!r} not in {list(STATUSES)}")
        for token in journey_tokens(row["journey or route"]):
            if token not in JOURNEYS:
                problems.append(
                    f"{where}: journey {token!r} outside R1-R12 / J1-J4"
                )
        if row["status"] == "accepted":
            for col in ("re-review trigger", "repro steps"):
                if row[col] in EMPTY_CELLS:
                    problems.append(f"{where}: accepted row has an empty {col!r}")
    return problems


def bar_blockers(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Rows that keep the cutover bar from passing: S0/S1 still open or accepted."""
    return [
        r
        for r in rows
        if r["severity"] in ("S0", "S1") and r["status"] in BAR_BLOCKING
    ]


def summary(rows: list[dict[str, str]]) -> str:
    by_sev = Counter(r["severity"] for r in rows)
    by_status = Counter(r["status"] for r in rows)
    lines = [f"{len(rows)} findings"]
    lines.append("by severity: " + ", ".join(f"{s} {by_sev.get(s, 0)}" for s in SEVERITIES))
    lines.append("by status: " + ", ".join(f"{s} {by_status.get(s, 0)}" for s in STATUSES))
    blockers = bar_blockers(rows)
    lines.append(
        "open S0/S1: " + (", ".join(r["F-id"] for r in blockers) if blockers else "none")
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--path", type=Path, default=DEFAULT_LOG, help="findings log (default: docs/findings-log.md)"
    )
    parser.add_argument(
        "--bar",
        choices=("cutover",),
        help="also fail while any S0/S1 row is open or accepted",
    )
    parser.add_argument("--summary", action="store_true", help="print counts by severity and status")
    args = parser.parse_args(argv)

    if not args.path.exists():
        print("no findings log yet")
        return 0

    try:
        rows = parse(args.path.read_text(encoding="utf-8"))
        problems = validate(rows)
    except LogError as exc:
        print(f"findings log: {exc}")
        return 1
    if problems:
        print("findings log: invalid")
        for problem in problems:
            print(f"  {problem}")
        return 1

    if args.summary:
        print(summary(rows))
    else:
        print(f"findings log: {len(rows)} rows ok")

    if args.bar == "cutover":
        blockers = bar_blockers(rows)
        if blockers:
            print("cutover bar: FAIL, open S0/S1 findings:")
            for r in blockers:
                print(f"  {r['F-id']} {r['severity']} {r['status']}: {r['journey or route']}")
            return 1
        print("cutover bar: no open S0/S1 findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
