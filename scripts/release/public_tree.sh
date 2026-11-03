#!/usr/bin/env bash
# The public tree is the dev tree minus scripts/release/dev-only.txt
# (docs/release-process.md §3.2 and §3.6). Two modes:
#
#   scripts/release/public_tree.sh --check
#       CI gate on the dev repo. Fails when a tracked file looks like a working
#       document (handoffs, plans, findings, test runs, rehearsal records,
#       screenshots, our own deploy files) but no line of dev-only.txt covers
#       it, so a new working doc cannot drift into a release unnoticed. Warns
#       about list entries that match nothing (stale lines rot).
#
#   scripts/release/public_tree.sh --build VERSION [--from REF]
#       Builds the squashed release branch `release-vVERSION` from REF (default:
#       tag vVERSION): an orphan branch whose single commit is REF's tree minus
#       every dev-only path, with scripts/release/CLAUDE.public.md in place of
#       CLAUDE.md, authored as the public identity. Refuses a dirty tree, and
#       refuses to commit if any working-document path survived or the tripwire
#       fails. It never pushes; docs/release-process.md §3.7 does that by hand.
#
# Exit codes: 0 ok · 1 a check failed · 2 usage or environment.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"
LIST=scripts/release/dev-only.txt
PUBLIC_CLAUDE=scripts/release/CLAUDE.public.md
PUBLIC_NAME="Logos Flux"
PUBLIC_EMAIL="lf@logosflux.io"

# A tracked path that matches this is a working document unless dev-only.txt
# names it. Keep it broad: a false positive costs one line in the list, a false
# negative ships a handoff to the public.
WORKING_DOC_RE='(^|/)(HANDOFF|TODO|TODOS|RELEASE_CHECKLIST[^/]*|REPO-REVIEW|NOTES|SCRATCH)\.md$|^docs/.*(handoff|findings|persona|perf/|sweep|hardening|testing-plan|testing-gaps|decisions-pending|rehearsal|baseline|visual-baselines|/pin/).*|^docs/.*\.(png|jpg|jpeg|webp)$|(^|/)fly\.toml$|^deploy/|^\.gitleaksignore$|^CLAUDE\.md$|(^|/)\.agents/|(^|/)\.claude/'

# Published on purpose, so never a working document: images the README and
# the docs embed (README screenshots are fetched from docs/images/ on GitHub
# and PyPI). Anything else under docs/ with an image extension still needs a
# dev-only line or a move here.
PUBLIC_ASSET_RE='^docs/images/'

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

pathspecs() { grep -vE '^\s*(#|$)' "$LIST"; }

covered_files() {
  # Every tracked file at least one dev-only line matches (NUL-safe).
  local specs=()
  while IFS= read -r line; do specs+=("$line"); done < <(pathspecs)
  git ls-files -z -- "${specs[@]}"
}

check() {
  local rc=0
  mapfile -d '' covered < <(covered_files)
  declare -A is_covered=()
  for f in "${covered[@]}"; do is_covered["$f"]=1; done

  echo "dev-only.txt: $(pathspecs | wc -l) lines cover ${#covered[@]} tracked files"

  # 1. Working documents not on the list.
  local missing=()
  while IFS= read -r -d '' f; do
    if [[ "$f" =~ $WORKING_DOC_RE ]] && ! [[ "$f" =~ $PUBLIC_ASSET_RE ]] && [[ -z "${is_covered[$f]:-}" ]]; then
      missing+=("$f")
    fi
  done < <(git ls-files -z)
  if ((${#missing[@]})); then
    rc=1
    echo "FAIL: ${#missing[@]} tracked file(s) look like working documents but no line of $LIST covers them:"
    printf '  %s\n' "${missing[@]}"
    echo "  Add a pathspec to $LIST, or rename the file if it is really an external document."
  fi

  # 2. List entries that match nothing (warn: the list rots otherwise).
  while IFS= read -r spec; do
    if [[ -z "$(git ls-files -- "$spec" | head -1)" ]]; then
      echo "warn: '$spec' matches no tracked file"
    fi
  done < <(pathspecs)

  # 3. The public CLAUDE.md template exists.
  [[ -f "$PUBLIC_CLAUDE" ]] || { rc=1; echo "FAIL: $PUBLIC_CLAUDE is missing"; }

  ((rc == 0)) && echo "release tree check: OK"
  return $rc
}

build() {
  local version=$1 from=${2:-}
  [[ -n "$version" ]] || usage
  version=${version#v}
  [[ -n "$from" ]] || from="v$version"
  local from_sha
  from_sha=$(git rev-parse -q --verify "$from^{commit}") || { echo "no such ref: $from"; exit 2; }
  [[ -z "$(git status --porcelain)" ]] || { echo "working tree is not clean; commit or stash first"; exit 2; }
  local branch="release-v$version"
  git rev-parse -q --verify "refs/heads/$branch" >/dev/null && { echo "branch $branch exists; delete it to rebuild"; exit 2; }

  echo "== $branch from $from (${from_sha:0:7})"
  git checkout -q --detach "$from_sha"
  git checkout -q --orphan "$branch"
  # The orphan keeps the index of $from: drop the dev-only paths from it and
  # from the working tree, so what is left is exactly the public tree.
  # The tripwire's pattern files are dev-only and are about to be removed:
  # keep copies, so the check below still has every pattern to test against.
  local patdir patfiles=()
  patdir=$(mktemp -d)
  trap 'rm -rf "$patdir"' RETURN
  shopt -s nullglob
  for f in scripts/release/tripwire-*.txt; do cp "$f" "$patdir/"; patfiles+=("$patdir/$(basename "$f")"); done
  shopt -u nullglob
  local specs=()
  while IFS= read -r line; do specs+=("$line"); done < <(pathspecs)
  git ls-files -z -- "${specs[@]}" | xargs -0 -r git rm -q -r -f --
  cp "$PUBLIC_CLAUDE" CLAUDE.md && git add CLAUDE.md

  # Nothing that looks like a working document may remain.
  local left=()
  while IFS= read -r -d '' f; do
    [[ "$f" == "CLAUDE.md" ]] && continue
    [[ "$f" =~ $WORKING_DOC_RE ]] && left+=("$f")
  done < <(git ls-files -z)
  if ((${#left[@]})); then
    echo "FAIL: working documents survived the sweep; add them to $LIST and rebuild:"
    printf '  %s\n' "${left[@]}"
    exit 1
  fi
  if ((${#patfiles[@]})); then
    TRIPWIRE_PATTERN_FILES="${patfiles[*]}" bash scripts/tripwire.sh
  else
    bash scripts/tripwire.sh
  fi

  git -c user.name="$PUBLIC_NAME" -c user.email="$PUBLIC_EMAIL" commit -q -m "Release v$version" \
    -m "Squashed from the development history at $from_sha ($from)."
  echo "== committed $(git rev-parse --short HEAD) on $branch as $PUBLIC_NAME <$PUBLIC_EMAIL>"
  echo "   files: $(git ls-files | wc -l) (dev tree at $from: $(git ls-tree -r --name-only "$from_sha" | wc -l))"
  echo "   next: the verification pass, then the push in docs/release-process.md §3.7"
}

case "${1:-}" in
  --check) check ;;
  --build) shift; v=${1:-}; shift || true; from=""; [[ "${1:-}" == "--from" ]] && from=${2:-}; build "$v" "$from" ;;
  *) usage ;;
esac
