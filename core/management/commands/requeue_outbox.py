"""``manage.py requeue_outbox <id> ... | --consumer NAME``: requeue dead letters.

Shorthand for ``manage.py outbox requeue``.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from core import outbox


class Command(BaseCommand):
    help = "Put dead outbox rows back to pending (by id, or every dead row of a consumer)."

    def add_arguments(self, parser):
        parser.add_argument("ids", nargs="*")
        parser.add_argument("--consumer", default=None)

    def handle(self, *args, **opts):
        if bool(opts["ids"]) == bool(opts["consumer"]):
            raise CommandError("Give row ids or --consumer NAME (one of them).")
        n = outbox.requeue(pks=opts["ids"], consumer=opts["consumer"])
        self.stdout.write(f"requeued {n} dead row(s)")
