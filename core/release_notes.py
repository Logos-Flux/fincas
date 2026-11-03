"""What changed between two git refs, for ``docs/releases/<tag>.md``
(``manage.py release_notes <from> <to>``; release-hardening plan 9.9, 7.5).

Everything here reads git (``subprocess.run`` with argument lists, never a
shell) and the files *as they are at the refs*, so it works from any
checkout of the repository and never imports the code it describes. It
touches no database and no Django setting: the command is usable with
``--skip-checks`` on a bare checkout.

What is collected:

- **Migrations added** (``git diff --name-status from..to -- */migrations/0*.py``),
  each classified from its source with :mod:`ast`: ``schema`` (any
  operation but a data one), ``state (AlterField only)``, ``data``
  (``RunPython`` / ``RunSQL``) or ``data + schema``; and whether it reverses:
  a ``RunPython`` whose reverse is ``RunPython.noop`` or missing (one
  positional argument, no ``reverse_code=``) is **one-way**, as is a
  ``RunSQL`` without ``reverse_sql``. The one-way list is plan 7.5.
- **Required secrets**: ``core.checks.DEPLOY_SECRETS`` at ``to``, with the
  keys that are new since ``from`` marked. The dict is read with
  ``ast.literal_eval`` from ``git show to:core/checks.py``; a ref without the
  dict (before 2026-10-15) reads as empty.
- **Settings**: top-level ``NAME = ...`` lines added or removed in
  ``golden_erp/settings/*.py``, and ``os.environ[...]`` / ``os.environ.get`` /
  ``os.getenv`` / ``getattr(settings, "...")`` reads added anywhere outside
  tests whose name was not read anywhere in the ``from`` tree.
- **Commits**: non-merge subjects in the range, grouped by their
  conventional prefix (``feat``, ``fix``, ...). Another tracker's ticket
  references are cut from a subject, and a subject that matches any isolation
  tripwire pattern of the checkout (``scripts/release/tripwire-*.txt``, read
  by :mod:`core.tripwire_patterns`) is withheld: the notes ship in the public
  tree, so they must pass the same check as everything else there. Only the
  count of withheld subjects is rendered.

The markdown it renders is split into the numbered sections of the release
notes outline (1 before you upgrade, 4 migrations, 5 everything else), each
wrapped in ``<!-- release_notes:begin:N -->`` / ``end`` markers so
:func:`write` can refresh a file without touching the hand-written parts.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from core import tripwire_patterns

MARKER_BEGIN = "<!-- release_notes:begin:{n} -->"
MARKER_END = "<!-- release_notes:end:{n} -->"
GENERATED_SECTIONS = (1, 4, 5)

#: The release-notes outline (plan 9.11). ``{title}`` and ``{range}`` are
#: filled by :func:`write`; sections 2 and 3 are placeholders for a person.
OUTLINE = """# {title}

<!-- release_notes: generated sections 1, 4 and 5 come from
     `manage.py release_notes {range}`; sections 0, 2 and 3 are written by hand.
     Re-running with --write refreshes only the marked blocks. -->

## 0. About this release

_Written by hand before the tag._

## 1. Before you upgrade

_Written by hand before the tag: the numbered upgrade steps with their
expected output. The block below is generated._

{section_1}

## 2. Behavior changes staff will notice

_Written by hand before the tag._

## 3. Known limitations

_Written by hand before the tag._

## 4. Migrations

{section_4}

## 5. Everything else

{section_5}
"""


class ReleaseNotesError(Exception):
    """A refusal (exit code USAGE): shallow clone, unknown ref, not a repo."""


# --- git ---------------------------------------------------------------------


def git(repo, *args, check=True) -> str:
    """Run ``git <args>`` in ``repo`` and return stdout (argument list, no shell)."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=False,
    )
    if check and result.returncode != 0:
        raise ReleaseNotesError(f"git {' '.join(args)}: {result.stderr.strip() or result.returncode}")
    return result.stdout


def check_repo(repo, refs) -> None:
    """Refuse a shallow clone or an unknown ref (plan 9.9).

    A shallow clone has no history for ``git diff from..to`` to walk, and a
    missing ref would silently compare against nothing; both exit 2 with the
    fix in the message.
    """
    toplevel = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False,
    )
    if toplevel.returncode != 0:
        raise ReleaseNotesError(f"{repo} is not a git checkout; release_notes needs one")
    if git(repo, "rev-parse", "--is-shallow-repository").strip() == "true":
        raise ReleaseNotesError("shallow clone: run git fetch --unshallow (or check out with fetch-depth: 0)")
    for ref in refs:
        probe = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True, text=True, check=False,
        )
        if probe.returncode != 0:
            raise ReleaseNotesError(f"unknown ref {ref!r}: run git fetch --tags origin, then try again")


def show(repo, ref, path) -> str | None:
    """The file at ``ref:path``, or None when it does not exist there."""
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"], capture_output=True, text=True, check=False,
    )
    return result.stdout if result.returncode == 0 else None


# --- migrations ---------------------------------------------------------------

DATA_OPERATIONS = {"RunPython", "RunSQL"}
STATE_ONLY_OPERATIONS = {"AlterField", "AlterModelOptions", "AlterModelManagers"}


@dataclass
class MigrationInfo:
    path: str
    app: str
    name: str
    operations: list[str] = field(default_factory=list)
    one_way: list[str] = field(default_factory=list)  # reasons, empty when reversible
    status: str = "A"

    @property
    def label(self) -> str:
        return f"{self.app}.{self.name}"

    @property
    def has_data(self) -> bool:
        return any(op in DATA_OPERATIONS for op in self.operations)

    @property
    def kind(self) -> str:
        if self.has_data:
            return "data + schema" if any(op not in DATA_OPERATIONS for op in self.operations) else "data"
        if self.operations and all(op in STATE_ONLY_OPERATIONS for op in self.operations):
            return "state (AlterField only)"
        return "schema"

    @property
    def reverse(self) -> str:
        if not self.has_data:
            return "-"
        return "one-way (" + "; ".join(self.one_way) + ")" if self.one_way else "reversible"


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _is_noop(node) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "noop"


def classify_migration(source: str) -> tuple[list[str], list[str]]:
    """``(operation names, one-way reasons)`` read from a migration's source.

    Only the calls listed in the ``Migration`` class's ``operations`` are
    inspected (a helper function's own calls, ``Decimal(...)`` or
    ``apps.get_model``, are not operations), so the classification is what
    Django would run, not what the file mentions.
    """
    tree = ast.parse(source)
    operations: list[str] = []
    one_way: list[str] = []
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "operations" for t in stmt.targets):
                continue
            if isinstance(stmt.value, (ast.List, ast.Tuple)):
                calls.extend(e for e in stmt.value.elts if isinstance(e, ast.Call))
    for node in calls:
        name = _call_name(node)
        if not name:
            continue
        operations.append(name)
        if name == "RunPython":
            reverse = node.args[1] if len(node.args) > 1 else None
            for kw in node.keywords:
                if kw.arg == "reverse_code":
                    reverse = kw.value
            if reverse is None:
                one_way.append("RunPython without a reverse")
            elif _is_noop(reverse):
                one_way.append("RunPython reverse is noop")
        elif name == "RunSQL":
            reverse = node.args[1] if len(node.args) > 1 else None
            for kw in node.keywords:
                if kw.arg == "reverse_sql":
                    reverse = kw.value
            if reverse is None:
                one_way.append("RunSQL without reverse_sql")
            elif _is_noop(reverse):
                one_way.append("RunSQL reverse is noop")
    return operations, one_way


def migrations_changed(repo, from_ref, to_ref) -> list[MigrationInfo]:
    """Every migration file whose status changed in the range, classified."""
    out = git(repo, "diff", "--name-status", f"{from_ref}..{to_ref}", "--", "*/migrations/0*.py")
    found: list[MigrationInfo] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0][:1], parts[-1]
        if path.endswith("__init__.py"):
            continue
        app = Path(path).parent.parent.name
        name = Path(path).stem
        info = MigrationInfo(path=path, app=app, name=name, status=status)
        if status != "D":
            source = show(repo, to_ref, path)
            if source is not None:
                try:
                    info.operations, info.one_way = classify_migration(source)
                except SyntaxError:
                    info.operations, info.one_way = ["(unparseable)"], ["could not parse the file"]
        found.append(info)
    found.sort(key=lambda m: (m.app, m.name))
    return found


# --- secrets and settings -------------------------------------------------------


def deploy_secrets(repo, ref) -> dict[str, str]:
    """``core.checks.DEPLOY_SECRETS`` as it is at ``ref`` ({} when absent)."""
    source = show(repo, ref, "core/checks.py")
    if source is None:
        return {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "DEPLOY_SECRETS" for t in node.targets
        ):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return {}
            return dict(value) if isinstance(value, dict) else {}
    return {}


_SETTING_LINE = re.compile(r"^[+-]([A-Z][A-Z0-9_]*)\s*=")


def settings_changed(repo, from_ref, to_ref) -> tuple[list[str], list[str]]:
    """``(new names, removed names)`` assigned at top level in ``golden_erp/settings/*.py``."""
    diff = git(repo, "diff", f"{from_ref}..{to_ref}", "--", "golden_erp/settings/*.py")
    added: set[str] = set()
    removed: set[str] = set()
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        m = _SETTING_LINE.match(line)
        if not m:
            continue
        (added if line[0] == "+" else removed).add(m.group(1))
    return sorted(added - removed), sorted(removed - added)


_ENV_READ = re.compile(
    r"os\.environ(?:\.get)?\s*[\[(]\s*[\"']([A-Z][A-Z0-9_]*)[\"']"
    r"|os\.getenv\(\s*[\"']([A-Z][A-Z0-9_]*)[\"']"
    r"|getattr\(\s*settings\s*,\s*[\"']([A-Z][A-Z0-9_]*)[\"']"
)
_DIFF_FILE = re.compile(r"^diff --git a/(.*?) b/(.*)$")
_TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|tests?\.py$)")


def env_reads_added(repo, from_ref, to_ref) -> list[str]:
    """Names newly read from the environment or ``settings`` in the range.

    A name counts when an added line reads it, no removed line in the diff
    read it (a moved line is not new), and the ``from`` tree has no read of
    it anywhere (``git grep``).
    """
    diff = git(repo, "diff", f"{from_ref}..{to_ref}", "--", "*.py")
    added: set[str] = set()
    removed: set[str] = set()
    skip = False
    for line in diff.splitlines():
        header = _DIFF_FILE.match(line)
        if header:
            skip = bool(_TEST_PATH.search(header.group(2)))
            continue
        if skip or line.startswith("+++") or line.startswith("---"):
            continue
        if line[:1] not in "+-":
            continue
        names = {n for match in _ENV_READ.finditer(line) for n in match.groups() if n}
        (added if line[0] == "+" else removed).update(names)
    candidates = sorted(added - removed)
    new: list[str] = []
    for name in candidates:
        hit = subprocess.run(
            ["git", "-C", str(repo), "grep", "-q", "-E", "-e", rf"[\"']{name}[\"']", from_ref, "--", "*.py"],
            capture_output=True, text=True, check=False,
        )
        if hit.returncode != 0:
            new.append(name)
    return new


# --- commits -------------------------------------------------------------------

_SUBJECT = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?!?:\s*(?P<rest>.+)$")
GROUP_ORDER = ("feat", "fix", "perf", "refactor", "docs", "ci", "build", "test", "chore", "other")
GROUP_TITLES = {
    "feat": "Features", "fix": "Fixes", "perf": "Performance", "refactor": "Refactors",
    "docs": "Documentation", "ci": "CI", "build": "Build", "test": "Tests", "chore": "Chores",
    "other": "Other",
}


# A parenthetical that cites a number in another tracker, e.g. "(downstream
# OP #3221)": anything with a "#N" inside that is not the bare "(#N)" PR
# suffix. Dropped from subjects: a deployment's ticket references are not
# release notes, and the notes ship in the public tree.
_FOREIGN_REF = re.compile(r"\s*\((?!#\d+\))[^()]*#\d+[^()]*\)")


def withheld_pattern(repo):
    """The checkout's tripwire patterns as one regex (None when it has none)."""
    toplevel = git(repo, "rev-parse", "--show-toplevel").strip()
    return tripwire_patterns.compile_all(toplevel)


def commits(repo, from_ref, to_ref, withhold=None) -> tuple[dict[str, list[str]], int]:
    """``(subjects grouped by conventional prefix, number withheld)``.

    Non-merge subjects in the range; one that ``withhold`` (a compiled
    regex, see :func:`withheld_pattern`) matches is counted, not listed.
    """
    out = git(repo, "log", "--no-merges", "--format=%s", f"{from_ref}..{to_ref}")
    groups: dict[str, list[str]] = {}
    withheld = 0
    for subject in out.splitlines():
        subject = _FOREIGN_REF.sub("", subject).strip()
        if not subject:
            continue
        if withhold is not None and withhold.search(subject):
            withheld += 1
            continue
        m = _SUBJECT.match(subject)
        kind = m.group("type") if m and m.group("type") in GROUP_TITLES else "other"
        groups.setdefault(kind, []).append(subject)
    return groups, withheld


# --- collect + render -------------------------------------------------------------


@dataclass
class Notes:
    from_ref: str
    to_ref: str
    migrations: list[MigrationInfo]
    secrets_to: dict[str, str]
    secrets_new: list[str]
    settings_new: list[str]
    settings_removed: list[str]
    env_reads_new: list[str]
    commits: dict[str, list[str]]
    commits_withheld: int = 0

    @property
    def migrations_added(self) -> list[MigrationInfo]:
        return [m for m in self.migrations if m.status in ("A", "C", "R")]

    @property
    def one_way(self) -> list[MigrationInfo]:
        return [m for m in self.migrations_added if m.one_way]


def collect(repo, from_ref, to_ref) -> Notes:
    check_repo(repo, (from_ref, to_ref))
    before = deploy_secrets(repo, from_ref)
    after = deploy_secrets(repo, to_ref)
    settings_new, settings_removed = settings_changed(repo, from_ref, to_ref)
    grouped, withheld = commits(repo, from_ref, to_ref, withhold=withheld_pattern(repo))
    return Notes(
        from_ref=from_ref, to_ref=to_ref,
        migrations=migrations_changed(repo, from_ref, to_ref),
        secrets_to=after, secrets_new=sorted(set(after) - set(before)),
        settings_new=settings_new, settings_removed=settings_removed,
        env_reads_new=env_reads_added(repo, from_ref, to_ref),
        commits=grouped, commits_withheld=withheld,
    )


def _wrap(n: int, body: str) -> str:
    return f"{MARKER_BEGIN.format(n=n)}\n{body.rstrip()}\n{MARKER_END.format(n=n)}"


def render_section_1(notes: Notes) -> str:
    lines = [f"_Generated from `{notes.from_ref}..{notes.to_ref}`._", ""]
    lines.append("**Required secrets** (`core.checks.DEPLOY_SECRETS`; `migrate` refuses without them under production settings, `core.E060`):")
    lines.append("")
    if notes.secrets_to:
        for name, purpose in notes.secrets_to.items():
            tag = " **(new in this range)**" if name in notes.secrets_new else ""
            lines.append(f"- `{name}`: {purpose}{tag}")
    else:
        lines.append("- none declared at this ref")
    lines.append("")
    for heading, names in (
        ("**Settings added** (`golden_erp/settings/*.py`):", notes.settings_new),
        ("**Settings removed:**", notes.settings_removed),
        ("**New environment / settings reads** (`os.environ`, `os.getenv`, `getattr(settings, ...)`, outside tests):",
         notes.env_reads_new),
    ):
        lines.append(heading)
        lines.append("")
        lines.extend([f"- `{name}`" for name in names] or ["- none"])
        lines.append("")
    return "\n".join(lines)


def render_section_4(notes: Notes) -> str:
    added = notes.migrations_added
    lines = [f"_Generated from `{notes.from_ref}..{notes.to_ref}`._", "",
             f"Migrations added: {len(added)}", ""]
    if added:
        lines.append("| Migration | Kind | Reverse | Time (7.1 dry run) |")
        lines.append("|---|---|---|---|")
        for m in added:
            lines.append(f"| `{m.label}` | {m.kind} | {m.reverse} | _ |")
        lines.append("")
    others = [m for m in notes.migrations if m.status not in ("A", "C", "R")]
    if others:
        lines.append("Migration files modified or deleted in the range (a released migration must not change):")
        lines.append("")
        lines.extend(f"- `{m.label}` ({m.status})" for m in others)
        lines.append("")
    lines.append("**One-way migrations** (plan 7.5: no reverse, or a `noop` reverse; roll back by restoring the backup, runbook §12):")
    lines.append("")
    if notes.one_way:
        lines.extend(f"- `{m.label}`: {'; '.join(m.one_way)}" for m in notes.one_way)
    else:
        lines.append("- none")
    return "\n".join(lines)


def render_section_5(notes: Notes) -> str:
    lines = [f"_Generated from `{notes.from_ref}..{notes.to_ref}` (non-merge commit subjects)._", ""]
    if not notes.commits:
        lines.append("- no commits in the range")
    for kind in GROUP_ORDER:
        subjects = notes.commits.get(kind)
        if not subjects:
            continue
        lines.append(f"**{GROUP_TITLES[kind]}**")
        lines.append("")
        lines.extend(f"- {s}" for s in subjects)
        lines.append("")
    if notes.commits_withheld:
        lines.append(f"_{notes.commits_withheld} subject(s) withheld: they name a deployment or private infrastructure._")
        lines.append("")
    return "\n".join(lines)


def render_sections(notes: Notes) -> dict[int, str]:
    return {
        1: _wrap(1, render_section_1(notes)),
        4: _wrap(4, render_section_4(notes)),
        5: _wrap(5, render_section_5(notes)),
    }


def render(notes: Notes) -> str:
    """The three generated sections as one markdown document (stdout mode)."""
    sections = render_sections(notes)
    return "\n".join([
        f"# Release notes: {notes.from_ref}..{notes.to_ref}", "",
        "## 1. Before you upgrade", "", sections[1], "",
        "## 4. Migrations", "", sections[4], "",
        "## 5. Everything else", "", sections[5], "",
    ])


def write(path, notes: Notes, title: str | None = None) -> str:
    """Create ``path`` from the outline, or refresh its marked blocks.

    A file with markers keeps everything outside them (sections 0, 2, 3 and
    the hand-written part of 1). A marked block missing from an existing
    file is appended under a heading so nothing generated is lost.
    Returns ``"created"`` or ``"updated"``.
    """
    path = Path(path)
    sections = render_sections(notes)
    range_text = f"{notes.from_ref} {notes.to_ref}"
    if not path.exists():
        text = OUTLINE.format(
            title=title or f"Fincas {path.stem}", range=range_text,
            section_1=sections[1], section_4=sections[4], section_5=sections[5],
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return "created"
    text = path.read_text(encoding="utf-8")
    for n, block in sections.items():
        begin, end = MARKER_BEGIN.format(n=n), MARKER_END.format(n=n)
        start, stop = text.find(begin), text.find(end)
        if start == -1 or stop == -1 or stop < start:
            text = text.rstrip("\n") + f"\n\n## {n}. (generated)\n\n{block}\n"
            continue
        text = text[:start] + block + text[stop + len(end):]
    path.write_text(text, encoding="utf-8")
    return "updated"
