#!/usr/bin/env bash
# Isolation tripwire: fails when a tracked file names something that must not
# be in this repository (a downstream deployment's identifiers, private
# infrastructure). Run by CI, `make tripwire` and scripts/release/public_tree.sh.
#
# The script holds no names itself. It reads every pattern file matching
# scripts/release/tripwire-*.txt (override with TRIPWIRE_PATTERN_FILES, a
# space-separated list); a missing or empty set of files means nothing to
# check, and the run passes. Pattern file format:
#
#   # comment
#   [all]          patterns checked in every tracked file
#   some-name
#   [shipping]     patterns checked only in files that ship in the public
#   other\.host    tree (not covered by scripts/release/dev-only.txt)
#
# Lines before any section header belong to [all]. One extended regex per
# line, matched case-insensitively. Keep patterns in the subset ERE and
# Python's `re` share (no [[:classes:]], no \b): core/tripwire_patterns.py
# reads the same files for the release notes and the tests. The pattern files
# themselves are never scanned.
#
# Exit codes: 0 clean · 1 a pattern matched · 2 usage or environment.
set -euo pipefail

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "tripwire: not inside a git checkout" >&2; exit 2; }
cd "$ROOT"
DEV_ONLY=scripts/release/dev-only.txt

if [[ -n "${TRIPWIRE_PATTERN_FILES:-}" ]]; then
  read -r -a FILES <<<"$TRIPWIRE_PATTERN_FILES"
else
  shopt -s nullglob
  FILES=(scripts/release/tripwire-*.txt)
  shopt -u nullglob
fi

ALL=()
SHIPPING=()
for f in "${FILES[@]}"; do
  [[ -f "$f" ]] || { echo "tripwire: pattern file $f not found" >&2; exit 2; }
  section=all
  while IFS= read -r line || [[ -n "$line" ]]; do
    # Trim both ends, as core/tripwire_patterns.py does: a stray trailing
    # space must not turn "name" into "name " and silently skip it.
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" == "#"* ]] && continue
    [[ "$line" == "--" ]] && { echo "tripwire: $f: '--' is not a pattern" >&2; exit 2; }
    case "$line" in
      "[all]") section=all ;;
      "[shipping]") section=shipping ;;
      "["*"]") echo "tripwire: $f: unknown section $line" >&2; exit 2 ;;
      *) if [[ $section == all ]]; then ALL+=("$line"); else SHIPPING+=("$line"); fi ;;
    esac
  done <"$f"
done

# Never scan the pattern files themselves. A file outside the checkout (the
# release build passes copies from a temp dir) needs no exclude, and an
# exclude pathspec outside the repository makes git grep fail.
EXCLUDE=()
for f in "${FILES[@]}"; do
  abs=$(cd "$(dirname "$f")" && pwd)/$(basename "$f")
  [[ "$abs" == "$ROOT"/* ]] && EXCLUDE+=(":(exclude)${abs#"$ROOT"/}")
done

scan() {
  # scan LABEL PATTERN... -- PATHSPEC...
  local label=$1; shift
  local args=()
  while [[ $1 != -- ]]; do args+=(-e "$1"); shift; done
  shift
  local out rc=0
  out=$(git grep -n -i -I -E "${args[@]}" -- . "$@" 2>&1) || rc=$?
  if ((rc == 0)); then
    echo "TRIPWIRE FAILED ($label): prohibited references found:"
    echo "$out"
    return 1
  elif ((rc != 1)); then
    echo "tripwire: git grep failed ($label): $out" >&2
    exit 2
  fi
  echo "  $label: clean ($(( ${#args[@]} / 2 )) patterns)"
}

echo "Running isolation tripwire (${#FILES[@]} pattern file(s), ${#ALL[@]} all-files + ${#SHIPPING[@]} shipping-files patterns)..."
failed=0

if ((${#ALL[@]})); then
  scan "all tracked files" "${ALL[@]}" -- "${EXCLUDE[@]}" || failed=1
fi

if ((${#SHIPPING[@]})); then
  SHIP_EXCLUDE=("${EXCLUDE[@]}")
  if [[ -f "$DEV_ONLY" ]]; then
    while IFS= read -r spec; do
      [[ "$spec" =~ ^[[:space:]]*(#|$) ]] && continue
      SHIP_EXCLUDE+=(":(exclude)$spec")
    done <"$DEV_ONLY"
  fi
  scan "shipping files" "${SHIPPING[@]}" -- "${SHIP_EXCLUDE[@]}" || failed=1
fi

if ((failed)); then
  exit 1
fi
echo "Tripwire check passed."
