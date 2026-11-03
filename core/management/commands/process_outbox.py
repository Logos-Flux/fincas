"""``manage.py process_outbox``: deliver the outbox (docs/crm/phase6-adapters.md 1.5).

Run it as a long-lived service (``--loop``; the production compose file's
``outbox`` service) or every minute from the scheduler
(``--once --max-seconds 50``). Safe to run several at once: each row is
claimed with a conditional UPDATE and a lease, and handlers run with no
transaction open. See ``docs/deployment.md`` section 8.
"""

from __future__ import annotations

import signal

from django.core.management.base import BaseCommand, CommandError

from core import outbox


class Command(BaseCommand):
    help = "Claim and handle due outbox rows (webhooks, adapter syncs)."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--once", action="store_true",
                          help="Stop when no row is due (default).")
        mode.add_argument("--loop", action="store_true",
                          help="Keep running; sleep --idle-sleep seconds when nothing is due.")
        parser.add_argument("--consumer", action="append", default=None, metavar="NAME",
                            help="Only these consumers (repeatable).")
        parser.add_argument("--limit", type=int, default=100, help="Rows claimed per pass (default 100).")
        parser.add_argument("--max-seconds", type=float, default=None,
                            help="Stop after this long (finishing the current row).")
        parser.add_argument("--idle-sleep", type=float, default=2.0,
                            help="Seconds to wait between idle passes with --loop (default 2).")
        parser.add_argument("--prune", action="store_true",
                            help="Delete finished rows past their retention first (GOLDEN_OUTBOX).")

    def handle(self, *args, **opts):
        if opts["limit"] is not None and opts["limit"] < 1:
            raise CommandError("--limit must be at least 1")
        known = outbox.consumers()
        unknown = sorted(set(opts["consumer"] or ()) - set(known))
        if unknown:
            raise CommandError(f"No outbox consumer named {', '.join(unknown)}; known: {', '.join(sorted(known))}")
        if opts["prune"]:
            pruned = outbox.prune()
            self.stdout.write("pruned: " + ", ".join(f"{k}={v}" for k, v in pruned.items()))
        stopping = {"now": False}

        def _stop(*_):
            stopping["now"] = True

        if opts["loop"]:
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    signal.signal(sig, _stop)
                except ValueError:  # not the main thread (tests)
                    pass
        result = outbox.loop(
            names=opts["consumer"], limit=opts["limit"], max_seconds=opts["max_seconds"],
            idle_sleep=opts["idle_sleep"], once=not opts["loop"], stop=lambda: stopping["now"],
        )
        self.stdout.write(
            f"outbox: claimed {result.claimed}, done {result.done}, retried {result.retried}, "
            f"dead {result.dead + result.expired_dead}, canceled {result.cancelled}, "
            f"lease lost {result.lost_lease}, claimed elsewhere {result.skipped}"
        )
