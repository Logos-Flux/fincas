#!/usr/bin/env python3
"""Write ``report.md`` for a harness run from ``steps.jsonl`` and ``meta.json``.

    scripts/harness/report.py --run-dir .runs/<sha>

Order (docs/release-hardening-plan.md, design block): SHA, database and
provenance, pass/fail per journey, then the first failing step with its
server-log lines, screenshot, trace and video paths and a one-line repro
command; passing steps collapsed; a link to the common-failures section of
docs/release-testing.md. Exit 0 always: the run's exit code is Playwright's.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DOCS = "docs/release-testing.md"
COMMON_FAILURES = f"{DOCS}#common-failures"


def load_steps(run: Path) -> dict[tuple[str, int], dict]:
    """The last record per (journey, step): a retry replaces its earlier attempt."""
    steps: dict[tuple[str, int], dict] = {}
    path = run / "steps.jsonl"
    if not path.exists():
        return steps
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            steps[(rec["journey"], rec["step"])] = rec
    return steps


def rel(run: Path, value: str | None) -> str:
    if not value:
        return "-"
    try:
        return str(Path(value).relative_to(run.parent.parent))
    except ValueError:
        return value


def journey_outcome(records: list[dict]) -> str:
    """PASS, FAIL, "FAIL (known: …)" when every failure is a known finding, or skipped."""
    failed = [r for r in records if r["status"] == "failed"]
    if failed:
        unknown = [r for r in failed if not r.get("known")]
        if unknown:
            return "FAIL"
        return "FAIL (known: " + ", ".join(sorted({r["known"] for r in failed})) + ")"
    if all(r["status"] == "skipped" for r in records):
        return "skipped"
    return "PASS"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    run = Path(args.run_dir).resolve()
    meta = json.loads((run / "meta.json").read_text()) if (run / "meta.json").exists() else {}
    steps = load_steps(run)
    by_journey: dict[str, list[dict]] = {}
    for (journey, _n), rec in sorted(steps.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        by_journey.setdefault(journey, []).append(rec)

    lines = [f"# Harness run {meta.get('short_sha', '?')}", ""]
    lines += [
        f"- **SHA:** `{meta.get('sha', '?')}` ({meta.get('branch', '?')}) on {meta.get('host', '?')}, "
        f"{meta.get('started', '?')} to {meta.get('ended', '?')}",
        f"- **Database:** `{meta.get('database', '?')}` (mode {meta.get('mode', '?')})",
        f"- **Provenance:** `{json.dumps(meta.get('provenance', {}), sort_keys=True)}`",
        f"- **Time:** total {meta.get('total_seconds', '?')} s "
        f"(seed {meta.get('seed_seconds', '?')} s, journeys {meta.get('journey_seconds', '?')} s); "
        f"journey filter `{meta.get('journey', 'all')}`"
        + (f", step {meta['step']}" if meta.get("step") else "")
        + (f", from step {meta['from_step']}" if meta.get("from_step") else "")
        + f", retries {meta.get('retries', 0)}",
        "",
        "## Journeys",
        "",
        "| journey | result | steps |",
        "|---|---|---|",
    ]
    if not by_journey:
        lines.append("| (none ran) | FAIL | 0 |")
    for journey, records in by_journey.items():
        passed = sum(r["status"] == "passed" for r in records)
        lines.append(f"| {journey} | **{journey_outcome(records)}** | {passed}/{len(records)} passed |")
    if meta.get("playwright_exit") not in (0, None) and all(
        journey_outcome(r) == "PASS" for r in by_journey.values()
    ):
        lines += ["", f"Playwright exited {meta['playwright_exit']} although every recorded step passed: "
                      "a failure outside a step (setup, teardown, a crash). See `playwright.json` in the run dir."]

    all_failed = [r for records in by_journey.values() for r in records if r["status"] == "failed"]
    # The first failure that is NOT a known finding leads; else the first known one.
    first_fail = next((r for r in all_failed if not r.get("known")), all_failed[0] if all_failed else None)
    known = [r for records in by_journey.values() for r in records if r.get("known")]
    if known:
        lines += ["", "## Known findings reproduced", ""]
        for r in known:
            lines.append(f"- `{r['known']}` at {r['journey']}.{r['step']:02d} {r['name']}"
                         f"{'' if r['status'] == 'failed' else ' (did not block)'}: "
                         f"{(r.get('verdict') or '').split(': ', 1)[-1][:200]}")
    if first_fail:
        r = first_fail
        heading = "First failing step" if not r.get("known") else f"First failing step (known finding {r['known']})"
        lines += ["", f"## {heading}: {r['journey']}.{r['step']:02d} {r['name']}", ""]
        lines.append(f"- **URL:** {r.get('url') or '-'}")
        lines.append(f"- **Window:** {r['started']} to {r['ended']} ({r['duration_ms']} ms), retry {r.get('retry', 0)}")
        if r.get("error"):
            lines += ["- **Step error:**", "", "```", r["error"][:3000], "```"]
        if r.get("responses"):
            lines.append(f"- **Page answered:** {', '.join(r['responses'])}")
        if r.get("console"):
            lines.append("- **Console:** " + " | ".join(r["console"]))
        if r.get("axe"):
            lines.append("- **axe serious:** " + "; ".join(f"{a['id']}×{a['nodes']} ({a.get('sample')})" for a in r["axe"]))
        if r.get("messages"):
            lines.append("- **Page messages:** " + " | ".join(r["messages"]))
        if (r.get("fit360") or {}).get("hscroll"):
            f = r["fit360"]
            lines.append(f"- **360 px:** horizontal scroll ({f['scrollWidth']} > {f['innerWidth']})")
        if r.get("server"):
            lines += ["- **Server log inside the step:**", "", "```"]
            for entry in r["server"][:5]:
                lines.append(entry[:4000])
            lines.append("```")
        lines.append(f"- **Screenshot:** `{rel(run, r.get('screenshot'))}`")
        lines.append(f"- **Trace:** `{rel(run, r.get('trace'))}` (open: `bunx playwright show-trace <path>`)")
        lines.append(f"- **Video:** `{rel(run, r.get('video'))}`")
        lines.append(f"- **Repro:** `make harness JOURNEY={r['journey']} STEP={r['step']} DB={meta.get('database', '<db>')}`")
        lines.append(f"- **Step log:** `{rel(run, str(run / 'steps.jsonl'))}` (every step's checks, as JSON lines)")
        lines.append(f"- Common failures: {COMMON_FAILURES}")

    for journey, records in by_journey.items():
        lines += ["", "<details>", f"<summary>{journey}: every step</summary>", "",
                  "| step | result | ms | note |", "|---|---|---|---|"]
        for r in records:
            note = r.get("verdict") or (r.get("error") or "").split("\n")[0][:120]
            if r["status"] == "failed" and not r.get("error"):
                note = (r.get("server") or [""])[0].split("\n")[0][:120] or note
            lines.append(f"| {r['step']:02d} {r['name']} | {r['status']} | {r['duration_ms']} | {note} |")
        lines += ["", "</details>"]

    lines += ["", f"Artefacts: `{run}` (server.log, steps.jsonl, steps/, video/, playwright-report/). "
                  f"How to read a failure: {COMMON_FAILURES}", ""]
    (run / "report.md").write_text("\n".join(lines), encoding="utf-8")
    summary = ", ".join(f"{j} {journey_outcome(r)}" for j, r in by_journey.items()) or "no journeys ran"
    print(f"harness: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
