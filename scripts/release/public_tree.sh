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
#                                  [--parent PUBLIC_REF [--allow-revert PATH]...]
#       Builds the squashed release branch `release-vVERSION` from REF (default:
#       tag vVERSION): one commit whose tree is REF's tree minus every dev-only
#       path, with scripts/release/CLAUDE.public.md in place of CLAUDE.md,
#       authored as the public identity. Refuses a dirty tree, and refuses to
#       commit if any working-document path survived or the tripwire fails.
#       Without --parent the commit is an orphan (the first release).
#       With --parent (the fetched public main, e.g. public/main) the commit
#       sits on top of it, so the push to public main fast-forwards. Revert
#       guard: every path public main changed since its last release commit
#       must be identical in the new tree (same blob and mode, or absent if
#       public deleted it), or the build fails naming the path. Port the public
#       change to the dev tree, or pass --allow-revert PATH (repeatable) when
#       the release deliberately supersedes it.
#       It never pushes or tags; docs/release-process.md §3.7 does that by hand.
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

usage() { sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

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

# A release commit is the one this script writes: subject "Release vX.Y.Z",
# authored as the public identity, body naming the dev commit it came from.
# Public PRs are also authored as Logos Flux, so the subject and body decide.
is_release_commit() {
  local c=$1 ae subj body
  ae=$(git log -1 --format=%ae "$c")
  subj=$(git log -1 --format=%s "$c")
  body=$(git log -1 --format=%b "$c")
  [[ "$ae" == "$PUBLIC_EMAIL" ]] &&
    [[ "$subj" =~ ^Release\ v[0-9]+\.[0-9]+\.[0-9]+[^[:space:]]*$ ]] &&
    [[ "$body" == *"Squashed from the development history at "* ]]
}

last_release_commit() {
  local c
  while IFS= read -r c; do
    is_release_commit "$c" && { echo "$c"; return 0; }
  done < <(git rev-list --first-parent "$1")
  return 1
}

# "mode sha" of PATH in TREE-ISH, or empty when absent.
entry() { git --literal-pathspecs ls-tree "$1" -- "$2" | awk '{print $1, $3}'; }

# Whether blob NEW already holds the change from BASE to PUBLIC: a three-way
# merge of PUBLIC into NEW is clean and changes nothing. Only a hint for the
# operator; the guard still demands the exact public version.
contains_change() {
  local d rc=0
  d=$(mktemp -d)
  git cat-file blob "$1" >"$d/base"; git cat-file blob "$2" >"$d/public"; git cat-file blob "$3" >"$d/new"
  git merge-file -q -p "$d/new" "$d/base" "$d/public" >"$d/out" 2>/dev/null && cmp -s "$d/out" "$d/new" || rc=1
  rm -rf "$d"
  return $rc
}

# The revert guard: every path changed between the last release commit and the
# parent must read the same in the new tree. Prints offenders, returns 1 if any.
revert_guard() {
  local release=$1 parent=$2 tree=$3; shift 3
  declare -A allowed=() used=()
  local p; for p in "$@"; do allowed["$p"]=1; done
  local bad=() checked=0 want got base
  while IFS= read -r -d '' p; do
    checked=$((checked + 1))
    want=$(entry "$parent" "$p"); got=$(entry "$tree" "$p")
    [[ "$want" == "$got" ]] && continue
    if [[ -n "${allowed[$p]:-}" ]]; then used["$p"]=1; echo "   allowed revert: $p"; continue; fi
    base=$(entry "$release" "$p")
    if [[ -z "$want" ]]; then bad+=("$p  (deleted on public main; the release would bring it back)")
    elif [[ -z "$got" && -z "$base" ]]; then bad+=("$p  (added on public main; the release lacks it)")
    elif [[ -z "$got" ]]; then bad+=("$p  (changed on public main; the release would delete it)")
    elif [[ -n "$base" ]] && contains_change "${base#* }" "${want#* }" "${got#* }"; then
      bad+=("$p  (changed on public main; the release's version contains that change plus dev edits)")
    else bad+=("$p  (changed on public main; the release has a different version)"); fi
  done < <(git diff -z --no-renames --name-only "$release" "$parent")
  GUARD_CHECKED=$checked
  for p in "$@"; do
    [[ -n "${used[$p]:-}" ]] || echo "warn: --allow-revert $p: not a reverted public-only change"
  done
  if ((${#bad[@]})); then
    echo "FAIL: the release would revert ${#bad[@]} public-only change(s) made since ${release:0:7}:"
    printf '  %s\n' "${bad[@]}"
    echo "  Port each public change to the dev tree and rebuild, or pass --allow-revert PATH"
    echo "  if the release deliberately supersedes it."
    return 1
  fi
}

build() {
  local version=$1 from=$2 parent=$3; shift 3
  local allow=("$@")
  [[ -n "$version" ]] || usage
  version=${version#v}
  [[ -n "$from" ]] || from="v$version"
  local from_sha
  from_sha=$(git rev-parse -q --verify "$from^{commit}") || { echo "no such ref: $from"; exit 2; }
  [[ -z "$(git status --porcelain)" ]] || { echo "working tree is not clean; commit or stash first"; exit 2; }
  local branch="release-v$version"
  git rev-parse -q --verify "refs/heads/$branch" >/dev/null && { echo "branch $branch exists; delete it to rebuild"; exit 2; }
  local parent_sha="" last_release=""
  if [[ -n "$parent" ]]; then
    parent_sha=$(git rev-parse -q --verify "$parent^{commit}") || { echo "no such parent: $parent (fetch the public repo first)"; exit 2; }
    last_release=$(last_release_commit "$parent_sha") ||
      { echo "no release commit (\"Release vX.Y.Z\" by $PUBLIC_NAME) in the first-parent history of $parent"; exit 2; }
  fi

  echo "== $branch from $from (${from_sha:0:7})"
  git checkout -q --detach "$from_sha"
  git checkout -q --orphan "$branch"
  # The orphan keeps the index of $from: drop the dev-only paths from it and
  # from the working tree, so what is left is exactly the public tree.
  # The tripwire's pattern files are dev-only and are about to be removed:
  # keep copies, so the check below still has every pattern to test against.
  local patdir patfiles=()
  patdir=$(mktemp -d)
  trap 'rm -rf "$patdir"; trap - RETURN' RETURN
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
    [[ "$f" =~ $PUBLIC_ASSET_RE ]] && continue
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

  local msg=("Release v$version" "Squashed from the development history at $from_sha ($from).")
  if [[ -z "$parent_sha" ]]; then
    git -c user.name="$PUBLIC_NAME" -c user.email="$PUBLIC_EMAIL" commit -q -m "${msg[0]}" -m "${msg[1]}"
    echo "== committed $(git rev-parse --short HEAD) on $branch as $PUBLIC_NAME <$PUBLIC_EMAIL> (orphan)"
  else
    local tree commit
    tree=$(git write-tree)
    echo "== revert guard: public main ${parent_sha:0:7}, last release ${last_release:0:7} ($(git log -1 --format=%s "$last_release"))"
    GUARD_CHECKED=0
    revert_guard "$last_release" "$parent_sha" "$tree" ${allow[@]+"${allow[@]}"} || exit 1
    echo "   public-only paths checked: $GUARD_CHECKED"
    commit=$(GIT_AUTHOR_NAME="$PUBLIC_NAME" GIT_AUTHOR_EMAIL="$PUBLIC_EMAIL" \
      GIT_COMMITTER_NAME="$PUBLIC_NAME" GIT_COMMITTER_EMAIL="$PUBLIC_EMAIL" \
      git commit-tree "$tree" -p "$parent_sha" -m "${msg[0]}" -m "${msg[1]}")
    git update-ref "refs/heads/$branch" "$commit"   # HEAD is the unborn $branch: now born, index = tree
    echo "== committed ${commit:0:7} on $branch as $PUBLIC_NAME <$PUBLIC_EMAIL>, parent ${parent_sha:0:7} ($parent)"
    echo "   what the release changes on public main:"
    git diff --stat "$parent_sha" "$commit" | sed 's/^/   /'
  fi
  echo "   files: $(git ls-files | wc -l) (dev tree at $from: $(git ls-tree -r --name-only "$from_sha" | wc -l))"
  echo "   next: the verification pass, then the push and tag in docs/release-process.md §3.7"
}

parse_build() {
  local v=${1:-} from="" parent="" allow=()
  shift || true
  while (($#)); do
    case "$1" in
      --from) from=${2:?--from needs a ref}; shift 2 ;;
      --parent) parent=${2:?--parent needs a ref}; shift 2 ;;
      --allow-revert) allow+=("${2:?--allow-revert needs a path}"); shift 2 ;;
      *) echo "unknown argument: $1"; usage ;;
    esac
  done
  if ((${#allow[@]})) && [[ -z "$parent" ]]; then echo "--allow-revert needs --parent"; exit 2; fi
  build "$v" "$from" "$parent" ${allow[@]+"${allow[@]}"}
}

# Braced so bash parses the whole dispatch before running it: the build checks
# out another commit, which may rewrite this very file under the running shell.
{
  case "${1:-}" in
    --check) check ;;
    --build) shift; parse_build "$@" ;;
    *) usage ;;
  esac
  exit
}
