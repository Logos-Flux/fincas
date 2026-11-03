"""``integrations_poll``: inbound by polling (docs/crm/phase6-adapters.md 2.4).

For every enabled, unpaused account whose adapter declares ``POLL`` (and,
for an ``OAUTH_USER`` adapter, for every active, unpaused per-user grant):

- call ``adapter.poll(cursor)`` until a page says ``more=False`` or the time
  budget is spent;
- ``ingest`` each item in its own short transaction, inside
  ``core.outbox.origin(<provider>)``;
- save the cursor after each page. A failed item is logged with its
  provider id and the page is retried next run (the cursor does not move),
  unless the adapter marked it permanent (``IngestOutcome.skip``);
- the **first sync** (empty cursor) runs inside
  ``core.plugins.dispatch.suppressed()`` with ``cursor["since"]`` set to
  ``settings["initial_since"]`` days back (default 90), so a backfill
  announces nothing. ``sync_state["initial_sync_done"]`` records it.

A rate-limit or throttle stops that account for this run; a rejected
credential pauses it (2.9). Nothing sleeps.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core.models import IntegrationAccount, IntegrationCredential
from core.plugins.payroll import AdapterError

from .ratelimit import paused_for

logger = logging.getLogger("golden_erp.integrations.poll")

DEFAULT_INITIAL_SINCE_DAYS = 90


@dataclass
class PollReport:
    accounts: int = 0
    skipped_paused: int = 0
    pages: int = 0
    items: int = 0
    changed: int = 0
    failed: int = 0
    stopped: list = field(default_factory=list)  # (account id, reason code)


def _origin(provider):
    from core import outbox

    return outbox.origin(provider)


def poll_accounts(*, provider: str | None = None, account_id=None, max_seconds: float = 50.0,
                  max_pages: int = 100, transport=None, clock=time.monotonic) -> PollReport:
    from core.plugins.crm_adapter import Capability

    from .accounts import installed_adapters
    from .transport import get_transport

    report = PollReport()
    adapters = installed_adapters()
    qs = IntegrationAccount.objects.filter(enabled=True).order_by("company_id", "provider")
    if provider:
        qs = qs.filter(provider=provider)
    if account_id:
        qs = qs.filter(pk=account_id)
    deadline = clock() + max_seconds
    for account in qs:
        cls = adapters.get(account.provider)
        if cls is None or Capability.POLL not in cls.capabilities:
            continue
        if paused_for(account) > 0:
            report.skipped_paused += 1
            continue
        report.accounts += 1
        adapter = cls(account, transport if transport is not None else get_transport(account))
        if Capability.OAUTH_USER in cls.capabilities:
            grants = IntegrationCredential.objects.filter(
                account=account, kind=IntegrationCredential.Kind.OAUTH_REFRESH, owner__isnull=False,
                revoked_at__isnull=True,
            ).select_related("owner").order_by("created_at")
            for grant in grants:
                if paused_for(grant) > 0:
                    report.skipped_paused += 1
                    continue
                _poll_one(adapter, account, grant, report, deadline, max_pages, clock)
        else:
            _poll_one(adapter, account, None, report, deadline, max_pages, clock)
        if clock() >= deadline:
            break
    return report


def _poll_one(adapter, account, grant, report, deadline, max_pages, clock) -> None:
    from core.plugins.crm_adapter import CredentialsRejected, RateLimited, Throttled
    from core.plugins.dispatch import suppressed

    holder = grant if grant is not None else account
    state = dict(holder.sync_state or {})
    cursor = dict(state.get("cursor") or {})
    first = not state.get("initial_sync_done")
    if first and "since" not in cursor:
        days = (account.settings or {}).get("initial_since", DEFAULT_INITIAL_SINCE_DAYS)
        cursor["since"] = (timezone.now() - timedelta(days=days)).isoformat()
    quiet = suppressed() if first else contextlib.nullcontext()
    pages = 0
    with quiet:
        while pages < max_pages and clock() < deadline:
            try:
                page = adapter.poll(cursor, credential=grant)
            except (RateLimited, Throttled, CredentialsRejected) as exc:
                report.stopped.append((str(account.pk), type(exc).__name__))
                return
            except AdapterError as exc:
                _record_error(account, type(exc).__name__)
                logger.warning("[integrations] poll %s account=%s failed: %s", account.provider, account.pk,
                               adapter.redact(str(exc)))
                report.stopped.append((str(account.pk), type(exc).__name__))
                return
            pages += 1
            report.pages += 1
            for item in page.items:
                report.items += 1
                try:
                    with _origin(account.provider), transaction.atomic():
                        outcomes = adapter.ingest(item)
                except Exception as exc:
                    report.failed += 1
                    logger.warning("[integrations] poll %s account=%s item failed: %s", account.provider,
                                   account.pk, type(exc).__name__)
                    _record_error(account, f"ingest_{type(exc).__name__}")
                    return  # the cursor does not pass a failed item: retried next run
                report.changed += sum(1 for o in outcomes if o.changed)
            cursor = dict(page.cursor or {})
            state["cursor"] = cursor
            if not page.more:
                state["initial_sync_done"] = True
            if grant is not None:
                # The active refresh row of this owner: a refresh that rotated
                # the token mid-run moved the grant to a new row.
                IntegrationCredential._base_manager.filter(
                    account=account, owner_id=grant.owner_id, kind=IntegrationCredential.Kind.OAUTH_REFRESH,
                    revoked_at__isnull=True,
                ).update(sync_state=state)
            else:
                IntegrationAccount._base_manager.filter(pk=account.pk).update(sync_state=state)
            holder.sync_state = state
            if not page.more:
                break
    IntegrationAccount._base_manager.filter(pk=account.pk).update(last_success_at=timezone.now())


def _record_error(account, code: str) -> None:
    IntegrationAccount._base_manager.filter(pk=account.pk).update(
        last_error=code[:500], last_error_at=timezone.now())
