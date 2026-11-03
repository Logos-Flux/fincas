"""``manage.py outbox list|requeue|cancel``: operate the outbox by hand.

``list`` shows consumer, event, object, attempts, error and age, never the
payload (it may hold a provider body). ``requeue`` puts ``dead`` rows back to
``pending`` with a fresh attempt budget, by id or by consumer after a fix.
``cancel`` gives up on ``pending`` or ``dead`` rows for good.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core import outbox
from core.models import OutboxEvent


class Command(BaseCommand):
    help = "List, requeue or cancel outbox rows."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        ls = sub.add_parser("list", help="List rows (no payloads).")
        ls.add_argument("--state", choices=[c for c, _ in OutboxEvent.State.choices], default=None)
        ls.add_argument("--consumer", default=None)
        ls.add_argument("--limit", type=int, default=50)
        rq = sub.add_parser("requeue", help="Requeue dead rows.")
        rq.add_argument("ids", nargs="*")
        rq.add_argument("--consumer", default=None, help="Requeue every dead row of this consumer.")
        cn = sub.add_parser("cancel", help="Cancel pending or dead rows.")
        cn.add_argument("ids", nargs="+")

    def handle(self, *args, **opts):
        action = opts["action"]
        if action == "list":
            return self._list(opts)
        if action == "requeue":
            if not opts["ids"] and not opts["consumer"]:
                raise CommandError("Give row ids or --consumer NAME.")
            if opts["ids"] and opts["consumer"]:
                raise CommandError("Give row ids or --consumer, not both.")
            n = outbox.requeue(pks=opts["ids"], consumer=opts["consumer"])
            self.stdout.write(f"requeued {n} dead row(s)")
            return
        n = outbox.cancel(pks=opts["ids"])
        self.stdout.write(f"canceled {n} row(s)")

    def _list(self, opts):
        qs = OutboxEvent.objects.order_by("-created_at")
        if opts["state"]:
            qs = qs.filter(state=opts["state"])
        if opts["consumer"]:
            qs = qs.filter(consumer=opts["consumer"])
        now = timezone.now()
        rows = list(qs.values("pk", "consumer", "event", "state", "object_type", "object_id", "attempts",
                              "max_attempts", "last_error", "created_at", "next_attempt_at")[:opts["limit"]])
        for r in rows:
            age = int((now - r["created_at"]).total_seconds() // 60)
            obj = f"{r['object_type']}:{r['object_id']}" if r["object_type"] else "-"
            self.stdout.write(
                f"{r['pk']}  {r['consumer']:<20} {r['event']:<26} {r['state']:<9} "
                f"{r['attempts']}/{r['max_attempts']}  {obj}  age={age}m  {r['last_error'] or ''}".rstrip()
            )
        if not rows:
            self.stdout.write("no rows")
