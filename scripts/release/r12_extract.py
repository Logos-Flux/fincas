#!/usr/bin/env python3
"""Turn docs/getting-started.md into the shell script journey R12 runs.

    python3 -I scripts/release/r12_extract.py DOC --sub OLD=NEW ... [--drop-match TEXT ...]

The page is the single source: every fenced block whose info string starts
with ``bash`` is copied, in order, into one script. Two tags change that:

- ``bash r12:skip``       not run (system packages, tear-down);
- ``bash r12:background`` started in the background, its output in
  ``server.log`` and its pid in ``server.pid`` (the dev server).

``--sub OLD=NEW`` replaces a literal string everywhere (the venv's Python,
the package to install, the ports and the container name, so a run never
collides with anything already on the machine). Each OLD must occur at least
once, or the page and r12_fresh_install.sh have drifted apart: exit 1.
``--drop-match TEXT`` drops every command line containing TEXT (the SQLite
variant drops the Docker lines); it must match too.

Pure standard library, safe under ``python -I``. Writes the script to stdout.
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path

FENCE = re.compile(r"^(?P<indent> *)```(?P<info>[^`]*)$")


def blocks(text: str) -> list[tuple[str, str]]:
    """(tag, body) for every ```bash block; tag is '', 'skip' or 'background'."""
    out: list[tuple[str, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        info = m.group("info").strip().split()
        indent = len(m.group("indent"))
        body: list[str] = []
        i += 1
        while i < len(lines) and not FENCE.match(lines[i]):
            body.append(lines[i][indent:] if lines[i][:indent].strip() == "" else lines[i])
            i += 1
        i += 1  # the closing fence
        if not info or info[0] != "bash":
            continue
        tags = [t.removeprefix("r12:") for t in info[1:] if t.startswith("r12:")]
        unknown = set(tags) - {"skip", "background"}
        if unknown:
            raise SystemExit(f"unknown r12 tag(s) {sorted(unknown)} in a bash block")
        out.append((tags[0] if tags else "", "\n".join(body)))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("doc", type=Path)
    parser.add_argument("--sub", action="append", default=[], metavar="OLD=NEW")
    parser.add_argument("--drop-match", action="append", default=[], metavar="TEXT")
    args = parser.parse_args(argv)

    found = blocks(args.doc.read_text(encoding="utf-8"))
    run = [(tag, body) for tag, body in found if tag != "skip"]
    if not run:
        print(f"{args.doc}: no bash blocks to run", file=sys.stderr)
        return 1
    if sum(tag == "background" for tag, _ in run) != 1:
        print(f"{args.doc}: expected exactly one r12:background block (the server)", file=sys.stderr)
        return 1

    script = []
    for n, (tag, body) in enumerate(run, 1):
        for drop in args.drop_match:
            body = "\n".join(line for line in body.splitlines() if drop not in line)
        if tag == "background":
            # The server: exactly one command; nohup so the journey outlives
            # this script, stdin closed so it never waits for a terminal.
            cmd = body.strip()
            if "\n" in cmd:
                print("the r12:background block must hold a single command", file=sys.stderr)
                return 1
            body = f"nohup {cmd} > server.log 2>&1 < /dev/null &\necho $! > server.pid"
        first = next((ln for ln in body.splitlines() if ln.strip()), "")
        label = shlex.quote(f"== block {n}: {first[:70]}")
        script.append(f'echo "$(date +%s.%N)" {label}\n{body}')
    text = "\n\n".join(script) + "\n"

    for sub in args.sub:
        old, sep, new = sub.partition("=")
        if not sep or not old:
            print(f"--sub needs OLD=NEW, got {sub!r}", file=sys.stderr)
            return 2
        if old not in text:
            print(f"{args.doc} no longer contains {old!r}: update r12_fresh_install.sh with the page", file=sys.stderr)
            return 1
        text = text.replace(old, new)
    for drop in args.drop_match:
        if not any(drop in body for _, body in run):
            print(f"{args.doc} has no command containing {drop!r}", file=sys.stderr)
            return 1

    sys.stdout.write("set -euo pipefail\n\n" + text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
