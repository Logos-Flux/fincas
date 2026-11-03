"""``manage.py crm_emit_due``: send ``task_due`` for tasks past their due time.

Run it every 5 minutes from the deployment's scheduler (cron, a systemd
timer, a platform scheduled job); see ``docs/deployment.md``. Safe to run
concurrently and to re-run: each task fires once per due time
(``crm.due``).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from crm.due import emit_due


class Command(BaseCommand):
    help = "Send task_due once for every open CRM task whose due time has passed."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None,
                            help="Handle at most this many tasks (the oldest due first).")

    def handle(self, *args, **opts):
        result = emit_due(limit=opts["limit"])
        self.stdout.write(f"task_due sent: {result.sent}; already claimed: {result.skipped}")
