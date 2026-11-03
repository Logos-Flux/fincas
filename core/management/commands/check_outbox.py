"""``manage.py check_outbox``: exit 1 when the outbox needs a human.

Red when any row is ``dead``; when a due ``pending`` row has waited longer
than ``--max-age-minutes`` past its ``next_attempt_at`` (no worker running;
a row waiting out its backoff is not late); when a ``running`` row's lease
expired over 5 minutes ago; or when rows belong to a consumer no longer
registered. Prints counts per consumer and state. Without a worker nothing is
lost: rows wait as ``pending`` and this says so.
"""

from __future__ import annotations

import datetime as dt
import sys

from django.core.management.base import BaseCommand

from core import outbox


class Command(BaseCommand):
    help = "Report outbox counts; exit 1 on dead, overdue, stuck or orphaned rows."

    def add_arguments(self, parser):
        parser.add_argument("--max-age-minutes", type=float, default=30.0)

    def handle(self, *args, **opts):
        info = outbox.stats()
        for consumer, states in sorted(info["counts"].items()):
            line = ", ".join(f"{state}={n}" for state, n in sorted(states.items()))
            self.stdout.write(f"{consumer}: {line}")
        if not info["counts"]:
            self.stdout.write("outbox empty")
        problems = []
        if info["dead"]:
            problems.append(f"{info['dead']} dead row(s); see `manage.py outbox list --state dead`")
        oldest = info["oldest_due"]
        limit = dt.timedelta(minutes=opts["max_age_minutes"])
        if oldest is not None and info["now"] - oldest > limit:
            age = int((info["now"] - oldest).total_seconds() // 60)
            problems.append(f"a due row has waited {age} min (is process_outbox running?)")
        if info["stuck_running"]:
            problems.append(f"{info['stuck_running']} running row(s) with a lease expired over 5 min ago")
        if info["orphaned"]:
            problems.append(f"rows for unregistered consumer(s) {', '.join(info['orphaned'])} (plugin removed?)")
        for problem in problems:
            self.stderr.write(f"FAIL: {problem}")
        if problems:
            sys.exit(1)
        self.stdout.write("OK")
