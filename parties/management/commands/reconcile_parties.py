"""``manage.py reconcile_parties [--check | --fix]`` (phase2-party.md 2.1, 7.1).

Reports drift between Customer / CustomerContact / CustomerAddress / Lead
rows and their parties, which appears when a write bypasses ``save()``
(``QuerySet.update``, ``bulk_create``). ``--check`` (the default) exits 1 on
any drift; ``--fix`` re-runs ``parties.sync`` for each drifting row, then
checks again. Conflicts (duplicates that need a person) and pending backfill
facts are listed but never fail ``--check``.
"""

from __future__ import annotations

import uuid
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Company
from parties.reconcile import CONFLICT, DRIFT, PENDING, check, fix


class Command(BaseCommand):
    help = "Report (and with --fix repair) drift between customers and their parties."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--check", action="store_true", help="Report drift; exit 1 if any (default)")
        mode.add_argument("--fix", action="store_true", help="Re-run the sync for every drifting row")
        parser.add_argument("--company", help="Company id; default: every company")
        parser.add_argument("--limit", type=int, default=50, help="Issues to list (default 50; 0 = counts only)")

    def handle(self, *args, **opts):
        company = None
        if opts["company"]:
            try:
                company = Company.objects.get(pk=uuid.UUID(opts["company"]))
            except (ValueError, Company.DoesNotExist):
                raise CommandError(f"--company {opts['company']!r}: no such company id") from None
        issues = check(company)
        if opts["fix"]:
            # A row is checked against its parent's party, so a fixed customer
            # can surface its contacts' drift: repeat while it shrinks.
            done = Counter()
            for _ in range(3):
                if not any(i.level in (DRIFT, PENDING) for i in issues):
                    break
                with transaction.atomic():
                    done.update(fix(issues))
                before, issues = issues, check(company)
                if len(issues) >= len(before):
                    break
            self.stdout.write("Fixed: " + (", ".join(f"{k} {v}" for k, v in sorted(done.items())) or "nothing"))
        self._print(issues, opts["limit"])
        drift = sum(1 for i in issues if i.level == DRIFT)
        if drift:
            raise CommandError(f"{drift} drift issue(s); run `manage.py reconcile_parties --fix`.")

    def _print(self, issues, limit):
        counts = Counter((i.level, i.model) for i in issues)
        for level in (DRIFT, CONFLICT, PENDING):
            row = ", ".join(f"{model} {n}" for (lvl, model), n in sorted(counts.items()) if lvl == level)
            self.stdout.write(f"{level}: {row or 'none'}")
        for issue in issues[: max(limit, 0)]:
            self.stdout.write(f"  {issue}")
        if limit and len(issues) > limit:
            self.stdout.write(f"  ... {len(issues) - limit} more")
