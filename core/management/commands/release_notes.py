"""``manage.py release_notes <from> <to> [--write docs/releases/<tag>.md]``.

Lists what an operator must know between two git refs (release-hardening
plan 9.9): migrations added with their kind and whether they reverse (the
one-way list of plan 7.5), ``core.checks.DEPLOY_SECRETS`` keys, settings
added or removed, new environment reads, and the commit subjects. The
output seeds ``docs/releases/<tag>.md`` and is checked by hand; see
:mod:`core.release_notes` for what each section reads.

Needs a git checkout with full history: a shallow clone or an unknown ref is
refused with exit 2 (``core.management.release_exit_codes.USAGE``) and the
fix in the message (``git fetch --unshallow``). Touches no database and no
setting, so ``--skip-checks`` is safe and the wheel ships it unchanged.

    manage.py release_notes v0.4.0-rc.4 HEAD
    manage.py release_notes v0.4.0-rc.4 v0.4.0 --write docs/releases/v0.4.0.md
"""

from __future__ import annotations

import os
import sys

from django.core.management.base import BaseCommand

from core import release_notes
from core.management import release_exit_codes as rc


class Command(BaseCommand):
    help = "Markdown release notes between two git refs: migrations, secrets, settings, commits."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("from_ref", help="older ref (a tag, e.g. v0.4.0-rc.4)")
        parser.add_argument("to_ref", help="newer ref (a tag, a branch, HEAD)")
        parser.add_argument("--write", metavar="PATH",
                            help="create PATH from the outline, or refresh its generated blocks")
        parser.add_argument("--title", help="document title for a new --write file (default: Fincas <stem>)")
        parser.add_argument("--repo", default=None,
                            help="git checkout to read (default: the current directory's repository)")

    def handle(self, *args, **opts):
        repo = opts["repo"] or os.getcwd()
        try:
            notes = release_notes.collect(repo, opts["from_ref"], opts["to_ref"])
        except release_notes.ReleaseNotesError as exc:
            self.stderr.write(f"release_notes: {exc}")
            self.stderr.flush()
            sys.exit(rc.USAGE)
        if opts["write"]:
            outcome = release_notes.write(opts["write"], notes, title=opts["title"])
            self.stdout.write(
                f"{outcome} {opts['write']}: {len(notes.migrations_added)} migration(s), "
                f"{len(notes.one_way)} one-way, {len(notes.secrets_new)} new secret(s), "
                f"{len(notes.settings_new)} new setting(s), {len(notes.env_reads_new)} new env read(s)"
            )
            return
        self.stdout.write(release_notes.render(notes))
