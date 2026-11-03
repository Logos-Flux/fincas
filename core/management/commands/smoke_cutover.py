"""``manage.py smoke_cutover [--dead-max N] [--overdue-minutes M] [--json]``.

The automatable rows of the cutover runbook's smoke paths
(``docs/pin/cutover-runbook.md`` §11), run against the current database and
settings (release-hardening plan 9.9). The downstream's runbook calls it in
the first 72 hours as ``smoke_cutover || rc=$?`` and branches on the exit
code, which comes from the shared table in
:mod:`core.management.release_exit_codes`: 0 ok, 10 degraded, 20 roll back,
2 usage.

Each check prints one line::

    <name>: observed=<value> threshold=<value> -> OK|DEGRADED|ROLL BACK (<where to act>)

followed by the manual rows it does not automate (quote → order → PDF, the
storefront pages, a portal login, stock levels, the downstream's own
integrations and jobs, and ``check_schema_parity``, which has its own
baseline arguments and is not re-run here), and one guidance line. The
guidance is printed and flushed before the process exits.

Checks and where they come from:

- ``companies``: exactly one ``core.Company`` (plan 0: one instance per
  company; the runbook assumes a single ``core_company.id``). Any other
  count is ROLL BACK.
- ``migrations``: ``MigrationExecutor``'s plan to the leaf nodes is empty.
  Unapplied migrations mean the deploy did not migrate: ROLL BACK.
- ``staff_login``: at least one active user ``core.permissions.is_staff_principal``
  accepts (a superuser, or ``is_staff`` plus the ``staff`` role). Nobody can
  open ``/erp/`` otherwise: ROLL BACK.
- ``storefront_identity``: the ``storefront-api`` user exists, is active,
  not staff, not superuser, has no usable password and holds every
  ``core.storefront.STOREFRONT_PERMISSIONS`` entry. Read-only: it never
  converges the identity (``manage.py ensure_storefront_identity`` does).
  The identity is unpinned by design (``core/service_accounts.py``: the
  storefront's only write is ``add``, and the catalog reads are public), so
  its pin is reported, not required. Repairable: DEGRADED.
- ``outbox_dead``: ``dead`` outbox rows at most ``--dead-max`` (default 0).
  DEGRADED: ``manage.py outbox list --state dead`` and ``requeue_outbox``.
- ``outbox_overdue``: no due ``pending`` row has waited longer than
  ``--overdue-minutes`` (default 30) past its ``next_attempt_at``: the
  worker is not running. DEGRADED.
- ``engagements_drift``: the counts ``reconcile_engagements --check`` would
  report, from the same functions. DEGRADED: ``reconcile_engagements --fix``.
- ``parties_drift``: ``parties.reconcile.check()`` drift issues (conflicts
  and pending facts never fail, as in the command). DEGRADED:
  ``reconcile_parties --fix``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from core.management import release_exit_codes as rc

RUNBOOK = "docs/pin/cutover-runbook.md"

#: Smoke rows from runbook §11 this command cannot run (and why).
MANUAL_ROWS = (
    ("quote_order_pdf", "create a quote, convert it to an order, open both PDFs (runbook §11: needs a browser and a test customer)"),
    ("storefront_pages", "home, a category and a product page on the storefront host return 200 with real prices (runbook §11)"),
    ("storefront_credential", "mint a storefront token; catalog 200, orders 403 (runbook §9)"),
    ("portal_login", "a test customer signs in and sees their orders (runbook §11)"),
    ("stock_visible", "a known SKU's stock matches the pre-cutover count (runbook §11)"),
    ("downstream_integrations", "the downstream's own publish, import and scheduled jobs (runbook §11)"),
    ("schema_parity", "not re-run here: `manage.py check_schema_parity --compare <baseline> --expect-diff <file>` with the deployment's own baseline (runbook §8)"),
)


@dataclass
class Check:
    name: str
    observed: str
    threshold: str
    status: str
    where: str

    def line(self) -> str:
        return f"{self.name}: observed={self.observed} threshold={self.threshold} -> {self.status} ({self.where})"


def _status(ok: bool, fail_status: str) -> str:
    return rc.STATUS_OK if ok else fail_status


def check_companies() -> Check:
    from core.models import Company

    rows = list(Company.objects.order_by("created_at").values_list("pk", "name"))
    observed = f"{len(rows)}"
    if len(rows) != 1 and rows:
        observed += " (" + ", ".join(f"{pk}={name!r}" for pk, name in rows[:5]) + (", ..." if len(rows) > 5 else "") + ")"
    return Check("companies", observed, "1", _status(len(rows) == 1, rc.STATUS_ROLL_BACK),
                 "runbook §7; plan 7.1: one Company per instance")


def check_migrations() -> Check:
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    pending = [f"{m.app_label}.{m.name}" for m, _backwards in plan]
    observed = f"{len(pending)} unapplied" + (f" ({', '.join(pending[:5])}{', ...' if len(pending) > 5 else ''})" if pending else "")
    return Check("migrations", observed, "0 unapplied", _status(not pending, rc.STATUS_ROLL_BACK),
                 "runbook §6/§10: migrate did not run")


def check_staff_login() -> Check:
    from core.permissions import is_staff_principal
    from core.service_accounts import service_account_user_ids

    # Service accounts (api-service is a superuser by spec) never sign in to
    # the back office, so they are not a staff login.
    candidates = (User.objects.filter(is_active=True).filter(is_staff=True)
                  | User.objects.filter(is_active=True, is_superuser=True)).exclude(
        pk__in=service_account_user_ids())
    principals = [u for u in candidates.distinct().order_by("pk") if is_staff_principal(u)]
    superusers = sum(1 for u in principals if u.is_superuser)
    observed = f"{len(principals)} staff principal(s), {superusers} superuser(s)"
    return Check("staff_login", observed, ">=1", _status(bool(principals), rc.STATUS_ROLL_BACK),
                 "runbook §11 back office; §7 backfill_tenancy, manage.py sync_roles")


def check_storefront_identity() -> Check:
    from core.service_accounts import service_company
    from core.storefront import STOREFRONT_PERMISSIONS, STOREFRONT_USERNAME

    user = User.objects.filter(username=STOREFRONT_USERNAME).first()
    if user is None:
        return Check("storefront_identity", "missing", "present, 7/7 permissions", rc.STATUS_DEGRADED,
                     "runbook §9: manage.py ensure_storefront_identity")
    held = {tuple(p.split(".", 1)) for p in user.get_all_permissions()}
    missing = [f"{a}.{c}" for a, c in STOREFRONT_PERMISSIONS if (a, c) not in held]
    problems = []
    if not user.is_active:
        problems.append("inactive")
    if user.is_staff:
        problems.append("is_staff")
    if user.is_superuser:
        problems.append("is_superuser")
    if user.has_usable_password():
        problems.append("usable password")
    if missing:
        problems.append("missing " + ", ".join(missing))
    pinned = service_company(user)
    observed = (
        f"present, {len(STOREFRONT_PERMISSIONS) - len(missing)}/{len(STOREFRONT_PERMISSIONS)} permissions, "
        f"pinned={pinned.pk if pinned is not None else 'none (by design)'}"
        + (", " + "; ".join(problems) if problems else "")
    )
    return Check("storefront_identity", observed, f"present, {len(STOREFRONT_PERMISSIONS)}/{len(STOREFRONT_PERMISSIONS)} permissions, not staff",
                 _status(not problems, rc.STATUS_DEGRADED), "runbook §9: manage.py ensure_storefront_identity")


def check_outbox(dead_max: int, overdue_minutes: float) -> list[Check]:
    from core import outbox

    info = outbox.stats()
    dead = info["dead"]
    checks = [Check("outbox_dead", str(dead), f"<={dead_max}", _status(dead <= dead_max, rc.STATUS_DEGRADED),
                    "docs/deployment.md §8: manage.py outbox list --state dead, requeue_outbox")]
    oldest = info["oldest_due"]
    waited = 0.0 if oldest is None else (info["now"] - oldest).total_seconds() / 60
    checks.append(Check(
        "outbox_overdue", f"{waited:.0f} min" if oldest is not None else "no due rows",
        f"<={overdue_minutes:g} min", _status(waited <= overdue_minutes, rc.STATUS_DEGRADED),
        "docs/deployment.md §8: is process_outbox running?",
    ))
    return checks


def check_engagements_drift() -> Check:
    from crm.management.commands import reconcile_engagements as rec

    counts = {
        "stale next action": len(list(rec.drift())),
        "quote without engagement": len(rec.quotes_without_engagement()),
        "revision off its chain": len(rec.revisions_off_thread()),
        "lead without engagement": len(rec.leads_without_engagement()),
        "activity not mirrored": len(rec.unmirrored_activities()),
        "email log without communication": len(rec.unlinked_email_logs()),
        "document email off its engagement": len(list(rec.unlinked_document_emails())),
    }
    total = sum(counts.values())
    detail = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
    return Check("engagements_drift", f"{total}" + (f" ({detail})" if detail else ""), "0",
                 _status(total == 0, rc.STATUS_DEGRADED), "manage.py reconcile_engagements --fix, then --check")


def check_parties_drift() -> Check:
    from parties.reconcile import CONFLICT, DRIFT, PENDING, check

    issues = check()
    drift = sum(1 for i in issues if i.level == DRIFT)
    conflicts = sum(1 for i in issues if i.level == CONFLICT)
    pending = sum(1 for i in issues if i.level == PENDING)
    observed = f"{drift} drift" + (f" (+{conflicts} conflict, {pending} pending; informational)" if conflicts or pending else "")
    return Check("parties_drift", observed, "0 drift", _status(drift == 0, rc.STATUS_DEGRADED),
                 "manage.py reconcile_parties --fix, then --check")


def run_checks(dead_max: int = 0, overdue_minutes: float = 30.0) -> list[Check]:
    checks = [check_companies(), check_migrations(), check_staff_login(), check_storefront_identity()]
    checks.extend(check_outbox(dead_max, overdue_minutes))
    checks.append(check_engagements_drift())
    checks.append(check_parties_drift())
    return checks


GUIDANCE = {
    rc.STATUS_OK: "smoke_cutover: OK. Run the manual rows above, then go (runbook §11).",
    rc.STATUS_DEGRADED: "smoke_cutover: DEGRADED. The release can stay up; act on each DEGRADED line, then re-run.",
    rc.STATUS_ROLL_BACK: f"smoke_cutover: ROLL BACK. Do not open the window; follow {RUNBOOK} §12 (valid until T0 + 72 h).",
}


class Command(BaseCommand):
    help = "Post-cutover smoke checks (runbook §11); exit 0 ok / 10 degraded / 20 roll back."

    def add_arguments(self, parser):
        parser.add_argument("--dead-max", type=int, default=0, help="dead outbox rows tolerated (default 0)")
        parser.add_argument("--overdue-minutes", type=float, default=30.0,
                            help="minutes a due outbox row may wait for a worker (default 30)")
        parser.add_argument("--json", action="store_true", help="print the checks and the verdict as JSON")

    def handle(self, *args, **opts):
        if opts["dead_max"] < 0 or opts["overdue_minutes"] < 0:
            self.stderr.write("smoke_cutover: thresholds must be >= 0")
            self.stderr.flush()
            sys.exit(rc.USAGE)
        checks = run_checks(opts["dead_max"], opts["overdue_minutes"])
        status = rc.worst(c.status for c in checks)
        code = rc.EXIT_FOR_STATUS[status]
        if opts["json"]:
            self.stdout.write(json.dumps({
                "checked_at": timezone.now().isoformat(),
                "checks": [asdict(c) for c in checks],
                "manual": [{"name": n, "how": h} for n, h in MANUAL_ROWS],
                "status": status, "exit_code": code, "guidance": GUIDANCE[status],
            }, indent=2))
        else:
            for check in checks:
                self.stdout.write(check.line())
            for name, how in MANUAL_ROWS:
                self.stdout.write(f"{name}: manual -> {how}")
            self.stdout.write(GUIDANCE[status])
        self.stdout.flush()
        sys.stdout.flush()
        if code != rc.OK:
            sys.exit(code)

