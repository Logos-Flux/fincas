"""Harness-only views (mounted by ``config.harness_urls``; see ``config/settings/harness.py``).

Nothing here ships: ``config/`` is golden's project shell. Every view is
loopback-only by the settings' ALLOWED_HOSTS guard.
"""

from __future__ import annotations

import datetime
import json
import threading
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

# ── the dev stub identity provider ────────────────────────────────────────────


class StubIdentityProvider:
    """A local issuer shaped like Supabase Auth, enough for the portal's provider.

    RSA key and JWKS (``core.identity.providers.SupabaseJWT`` verifies through
    ``jwks_url``), a subject store the Admin API endpoint answers from (the
    portal's link step asks it whether a subject confirmed its address), and a
    token mint for the journeys. One key per server process.
    """

    KID = "harness-1"

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({"kid": self.KID, "alg": "RS256", "use": "sig"})
        self.jwks = {"keys": [jwk]}
        self.users: dict[str, dict] = {}
        self.lock = threading.Lock()

    def register(self, email: str, *, subject: str | None = None, confirmed: bool = True) -> dict:
        subject = subject or str(uuid.uuid5(uuid.NAMESPACE_URL, f"harness-idp:{email.lower()}"))
        record = {
            "id": subject,
            "email": email,
            "email_confirmed_at": "2026-01-01T00:00:00Z" if confirmed else None,
            "user_metadata": {"email_verified": confirmed},
        }
        with self.lock:
            self.users[subject] = record
        return record

    def token(self, subject: str, email: str, *, confirmed: bool = True, ttl_s: int = 3600, **claims) -> str:
        now = datetime.datetime.now(datetime.UTC)
        payload = {
            "iss": settings.HARNESS_IDP_ISSUER,
            "aud": "authenticated",
            "sub": subject,
            "email": email,
            "iat": int(now.timestamp()),
            "exp": int(now.timestamp()) + ttl_s,
            "user_metadata": {"email_verified": confirmed},
            "role": "authenticated",
            **claims,
        }
        return jwt.encode(payload, self.key, algorithm="RS256", headers={"kid": self.KID})


_idp: StubIdentityProvider | None = None
_idp_lock = threading.Lock()


def idp() -> StubIdentityProvider:
    global _idp
    with _idp_lock:
        if _idp is None:
            _idp = StubIdentityProvider()
        return _idp


@require_GET
def idp_jwks(request):
    return JsonResponse(idp().jwks)


@require_GET
def idp_admin_user(request, subject):
    """``GET /auth/v1/admin/users/<id>`` as Supabase answers it (service key required)."""
    key = settings.HARNESS_IDP_SERVICE_KEY
    if request.headers.get("apikey") != key or request.headers.get("Authorization") != f"Bearer {key}":
        return JsonResponse({"error": "service key required"}, status=401)
    record = idp().users.get(subject)
    if record is None:
        return JsonResponse({"error": "user not found"}, status=404)
    return JsonResponse(record)


@csrf_exempt
@require_POST
def idp_token(request):
    """Mint a portal token: ``{"email": ..., "subject"?: ..., "confirmed"?: true, "ttl_s"?: 3600}``.

    The subject is registered with the stub's Admin API at the same time, so
    the portal's confirmation check sees it.
    """
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    email = (body.get("email") or "").strip()
    if not email:
        return JsonResponse({"error": "email required"}, status=400)
    confirmed = bool(body.get("confirmed", True))
    record = idp().register(email, subject=body.get("subject"), confirmed=confirmed)
    token = idp().token(record["id"], email, confirmed=confirmed, ttl_s=int(body.get("ttl_s", 3600)))
    return JsonResponse({"token": token, "subject": record["id"], "provider": settings.HARNESS_IDP_PROVIDER})


# ── the server-error fixture ───────────────────────────────────────────────────


class HarnessBoom(RuntimeError):
    """Raised on purpose by ``/harness/boom/raise/``."""


def boom(request):
    """A 302 to the view that raises: the page the harness lands on looks fine."""
    return HttpResponseRedirect("/harness/boom/raise/")


def boom_raise(request):
    raise HarnessBoom("harness boom: the server-error capture must fail this step")


# ── the local webhook receiver ────────────────────────────────────────────────


class WebhookReceiver:
    """Records deliveries, deduping by ``X-Golden-Delivery-Id``.

    A repeat with a known id is counted, never re-applied (the outbox's
    at-least-once contract, docs/release-hardening-plan.md section 8). With
    ``HARNESS_WEBHOOK_LOG`` set, every POST is also appended to that
    JSON-lines file, so a receiver restart loses nothing.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.deliveries: list[dict] = []
        self.seen: dict[str, int] = {}

    def record(self, request) -> dict:
        delivery_id = request.headers.get("X-Golden-Delivery-Id") or ""
        try:
            payload = json.loads(request.body or b"{}")
        except ValueError:
            payload = {"_raw": request.body.decode("utf-8", "replace")}
        entry = {
            "received_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "delivery_id": delivery_id,
            "event": request.headers.get("X-Golden-Event", ""),
            "subscription": request.headers.get("X-Golden-Delivery", ""),
            "signature": request.headers.get("X-Golden-Signature", ""),
            "payload": payload,
        }
        with self.lock:
            duplicate = bool(delivery_id) and delivery_id in self.seen
            if duplicate:
                self.seen[delivery_id] += 1
            else:
                if delivery_id:
                    self.seen[delivery_id] = 1
                self.deliveries.append(entry)
        entry = {**entry, "duplicate": duplicate}
        path = getattr(settings, "HARNESS_WEBHOOK_LOG", None)
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        return entry

    def listing(self) -> dict:
        with self.lock:
            return {
                "count": len(self.deliveries),
                "repeats": {k: v - 1 for k, v in self.seen.items() if v > 1},
                "deliveries": list(self.deliveries),
            }

    def clear(self) -> None:
        with self.lock:
            self.deliveries.clear()
            self.seen.clear()


receiver = WebhookReceiver()


# ── read-only database facts for the journeys ─────────────────────────────────

STATE_LIMIT = 200


@require_GET
def state(request, label):
    """``GET /harness/state/<app_label>.<Model>/?field=value&…`` -> ``{count, rows}``.

    Filters are ORM lookups (``status=sent``, ``quote__quote_number=Q-…``);
    ``_order`` sorts (default pk), ``_limit`` caps the rows (default 200).
    Rows are ``values()`` of the base manager, JSON-encoded (Decimal and UUID
    as strings, datetimes ISO-8601). Loopback only, harness settings only.
    """
    from django.apps import apps
    from django.core.exceptions import FieldError
    from django.core.serializers.json import DjangoJSONEncoder

    try:
        model = apps.get_model(label)
    except (LookupError, ValueError):
        return JsonResponse({"error": f"unknown model {label!r}"}, status=404)
    filters = {k: v for k, v in request.GET.items() if not k.startswith("_")}
    try:
        qs = model._base_manager.filter(**filters).order_by(request.GET.get("_order") or "pk")
        limit = min(int(request.GET.get("_limit", STATE_LIMIT)), STATE_LIMIT)
        rows = list(qs.values()[:limit])
        count = qs.count()
    except (FieldError, ValueError, TypeError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"count": count, "rows": rows}, encoder=DjangoJSONEncoder)


@require_GET
def state_gl(request):
    """``GET /harness/state/gl/?company=<uuid>``: trial balance and journal totals."""
    from django.core.serializers.json import DjangoJSONEncoder
    from django.db.models import Sum

    from accounting.models import Account, JournalEntry, JournalLine

    company = request.GET.get("company")
    if not company:
        return JsonResponse({"error": "company required"}, status=400)
    posted = JournalLine._base_manager.filter(entry__company_id=company, entry__status=JournalEntry.Status.POSTED)
    totals = posted.aggregate(debit=Sum("debit_amount"), credit=Sum("credit_amount"))
    by_account = list(
        posted.values("account__account_number", "account__name")
        .annotate(debit=Sum("debit_amount"), credit=Sum("credit_amount")).order_by("account__account_number")
    )
    entries = list(
        JournalEntry._base_manager.filter(company_id=company).order_by("-created_at")
        .values("id", "entry_number", "entry_date", "status", "entry_source", "source_reference",
                "posting_key")[:50]
    )
    # Every posting path balances (R9): entries whose own legs do not, and
    # the posted total per source (invoice, payment, refund, receipt, ...).
    per_entry = (
        JournalLine._base_manager.filter(entry__company_id=company)
        .values("entry_id", "entry__entry_number", "entry__entry_source", "entry__status")
        .annotate(debit=Sum("debit_amount"), credit=Sum("credit_amount"))
    )
    unbalanced = [
        {"id": r["entry_id"], "entry_number": r["entry__entry_number"], "status": r["entry__status"],
         "debit": r["debit"], "credit": r["credit"]}
        for r in per_entry if (r["debit"] or 0) != (r["credit"] or 0)
    ]
    by_source = list(
        posted.values("entry__entry_source").annotate(debit=Sum("debit_amount"), credit=Sum("credit_amount"))
        .order_by("entry__entry_source")
    )
    accounts = Account._base_manager.filter(company_id=company).count()
    return JsonResponse(
        {"debit": totals["debit"] or 0, "credit": totals["credit"] or 0,
         "balanced": (totals["debit"] or 0) == (totals["credit"] or 0),
         "by_account": by_account, "entries": entries, "accounts": accounts,
         "entry_count": JournalEntry._base_manager.filter(company_id=company).count(),
         "unbalanced": unbalanced, "by_source": by_source},
        encoder=DjangoJSONEncoder,
    )


@csrf_exempt
@require_http_methods(["GET", "POST", "DELETE"])
def webhooks(request):
    if request.method == "POST":
        entry = receiver.record(request)
        return JsonResponse({"ok": True, "duplicate": entry["duplicate"], "delivery_id": entry["delivery_id"]})
    if request.method == "DELETE":
        receiver.clear()
        return HttpResponse(status=204)
    return JsonResponse(receiver.listing())
