"""``manage.py demo_roll_dates``: bring a demo database's dates up to today.

    manage.py demo_roll_dates                    # shift by today - anchor
    manage.py demo_roll_dates --dry-run          # report, write nothing
    manage.py demo_roll_dates --anchor 2026-10-01  # the data's real date (no anchor stored yet)
    manage.py demo_roll_dates --show             # print the anchor and exit

Moves every demo date and datetime forward by whole days so the data's
anchor becomes today (in ``TIME_ZONE``), then records today as the anchor;
a second run on the same day moves nothing. What moves and what never does:
``core/demo_clock.py``. A demo deployment's reset job runs it after every
nightly restore.

Refuses unless ``DEMO_USERNAMES`` is set (a public demo deployment) or
``--force`` is given: it rewrites dates with ``QuerySet.update()``, which
on a real company would rewrite its history.
"""

from __future__ import annotations

import datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core import demo_clock


def _date(value):
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f"{value!r} is not a date (YYYY-MM-DD).") from exc


class Command(BaseCommand):
    help = "Shift demo dates forward so the data's anchor date becomes today (demo deployments only)."

    def add_arguments(self, parser):
        parser.add_argument("--anchor", type=_date,
                            help="The date the data was written for, when no anchor is known yet "
                                 "(ignored once a seed or a roll has set one).")
        parser.add_argument("--today", type=_date, help="Roll to this date instead of today.")
        parser.add_argument("--dry-run", action="store_true", help="Report what would move; write nothing.")
        parser.add_argument("--show", action="store_true", help="Print the stored anchor and exit.")
        parser.add_argument("--force", action="store_true",
                            help="Run although DEMO_USERNAMES is unset (never on a real company's database).")

    def handle(self, *args, **opts):
        if opts["show"]:
            # key=value: demo setup scripts read zone= for the reset timer.
            anchor = demo_clock.get_anchor()
            self.stdout.write(f"anchor={anchor or 'none'} today={demo_clock.local_today()} "
                              f"zone={demo_clock.demo_zone().key}")
            return
        if not settings.DEMO_USERNAMES and not opts["force"]:
            raise CommandError("Refusing: DEMO_USERNAMES is not set, so this is not a demo deployment. "
                               "demo_roll_dates rewrites every date in the database; pass --force only "
                               "on a throwaway demo copy.")
        try:
            result = demo_clock.roll(today=opts["today"], anchor=opts["anchor"], dry_run=opts["dry_run"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        prefix = "dry run: " if opts["dry_run"] else ""
        if result.anchor_ignored:
            self.stderr.write(f"Ignoring --anchor {result.anchor_ignored}: the data is already anchored to "
                              f"{result.anchor} by a seed or a roll.")
        # Demo setup scripts match "no anchor stored" and "rolled " below to
        # decide whether to re-take the baseline, and reset scripts match
        # "no anchor stored": keep the words.
        if result.initialised:
            self.stdout.write(f"{prefix}no anchor stored: anchored the data to {result.today} "
                              f"({result.zone}); nothing moved. If the data is older, re-run with "
                              "--anchor <the date it was written>.")
            return
        if not result.delta_days:
            self.stdout.write(f"{prefix}already anchored to {result.today} ({result.zone}); nothing moved.")
            return
        rows = sum(result.rows.values())
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}rolled {result.delta_days} day(s): {result.anchor} -> {result.today} ({result.zone}), "
            f"{rows} row update(s) over {sum(1 for n in result.rows.values() if n)} table(s); "
            f"{result.books_kept} journal entr{'y' if result.books_kept == 1 else 'ies'} kept in closed books."))
        if opts["verbosity"] > 1:
            for label, count in sorted(result.rows.items()):
                if count:
                    self.stdout.write(f"  {label}: {count}")
