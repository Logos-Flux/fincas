"""The inbound webhook endpoint (docs/crm/phase6-adapters.md 2.8).

``POST /api/v1/integrations/<provider>/<account uuid>/webhook/`` — a core
route, anonymous, CSRF-exempt, body capped at 1 MB:

1. **Account.** Unknown account, an account of another provider, a disabled
   account or an uninstalled adapter: 404 with the same body, so the answer
   never says which.
2. **Verify.** ``adapter.verify_webhook(headers, body)`` checks the
   provider's signature with the account's ``webhook_secret`` credential
   (constant-time). A failed check is 401, logged with the account and a
   reason code, never the body or a header value. A signed ``timestamp``
   older than 300 s (``webhooks.services.is_fresh_timestamp``'s window) or
   more than 30 s in the future (clock skew) is 401 too.
3. **Enqueue.** The verified body becomes one ``<provider>.inbound`` outbox
   row of the account's company, payload = the adapter's envelope,
   ``idempotency_key`` = ``<account id>:`` + the provider's delivery id, else
   ``sha256(body)`` (account-scoped: the consumer is shared by every tenant).
   A replay hits ``outbox_idem_uniq`` and is acknowledged (200) with no
   second row.
4. **Answer fast.** 200 as soon as the row is written; ``process_outbox``
   does the work.

Tenancy: the account id in the URL fixes the company, and the signature is
checked with *that* account's secret, so a body signed for tenant A and
posted to tenant B's URL is 401. Nothing here reads a company from the body.
"""

from __future__ import annotations

import hashlib
import logging
import re

from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from core.models import IntegrationAccount

logger = logging.getLogger("golden_erp.integrations.webhook")

MAX_BODY_BYTES = 1024 * 1024
FRESHNESS_S = 300
FUTURE_SKEW_S = 30
_DELIVERY_ID = re.compile(r"^[\x21-\x7e]{1,200}$")


def _not_found():
    return JsonResponse({"detail": "Not found."}, status=404)


def _refused():
    return JsonResponse({"detail": "Signature verification failed."}, status=401)


def is_fresh(timestamp, *, window_s: float = FRESHNESS_S, now=None) -> bool:
    """``webhooks.services.is_fresh_timestamp``'s window on a datetime, allowing 30 s of provider clock skew."""
    if timestamp is None:
        return True
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    skew = ((now or timezone.now()) - timestamp).total_seconds()
    return -FUTURE_SKEW_S <= skew <= window_s


@csrf_exempt
def webhook_view(request, provider: str, account_id):
    """See the module docstring."""
    if request.method != "POST":
        return HttpResponse(status=405, headers={"Allow": "POST"})
    from core import outbox
    from core.integrations.accounts import adapter_class
    from core.integrations.transport import get_transport
    from core.plugins.crm_adapter import Capability

    account = IntegrationAccount.objects.filter(pk=account_id, provider=provider, enabled=True).first()
    cls = adapter_class(provider) if account is not None else None
    if account is None or cls is None or Capability.WEBHOOK not in cls.capabilities:
        return _not_found()
    length = request.META.get("CONTENT_LENGTH") or "0"
    if not length.isdigit() or int(length) > MAX_BODY_BYTES:
        return JsonResponse({"detail": "Body too large."}, status=413)
    body = request.body
    if len(body) > MAX_BODY_BYTES:
        return JsonResponse({"detail": "Body too large."}, status=413)

    adapter = cls(account, get_transport(account))
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        check = adapter.verify_webhook(headers, body)
    except Exception as exc:  # a missing/unreadable secret is a refusal, not a 500
        logger.warning("[integrations] webhook refused: provider=%s account=%s reason=%s",
                       provider, account.pk, type(exc).__name__)
        return _refused()
    if not check.ok:
        logger.warning("[integrations] webhook refused: provider=%s account=%s reason=%s",
                       provider, account.pk, check.reason or "bad_signature")
        return _refused()
    if not is_fresh(check.timestamp):
        logger.warning("[integrations] webhook refused: provider=%s account=%s reason=stale",
                       provider, account.pk)
        return _refused()
    try:
        item = adapter.webhook_envelope(headers, body)
    except ValueError:
        return JsonResponse({"detail": "Body is not valid JSON."}, status=400)

    delivery_id = check.delivery_id if _DELIVERY_ID.match(check.delivery_id or "") else ""
    # Scoped by account: the consumer is shared by every tenant, and two
    # tenants can receive the same delivery id or the same body.
    key = f"{account.pk}:" + (delivery_id or "sha256:" + hashlib.sha256(body).hexdigest())
    consumer = f"{provider}.inbound"
    created = outbox.enqueue(
        consumer,
        outbox.EventSpec(idempotency_key=key, payload=item, event="webhook", company_id=account.company_id),
        ignore_suppression=True,
    )
    logger.info("[integrations] webhook accepted: provider=%s account=%s replay=%s",
                provider, account.pk, not created)
    return JsonResponse({"accepted": True, "duplicate": not created}, status=200)
