"""Run a registered connection test and persist it as a ``core.IntegrationCheck``.

The generic half of the Integrations page's "Run test" action. The tests
themselves are registered by whichever app owns the integration
(``core.integration_registry.register_connection_test``); this module only
runs one, makes sure nothing secret survives into the stored row, and records
who ran it.

What is stored: target, ok, an error class, one operator sentence, HTTP status,
latency and a small ``observed`` dict. The sentence and every observed string
are redacted against every configured local credential value and against
token-shaped substrings; ``observed`` keeps only scalars and short plain
strings (model ids, hosts), never a body, header or URL with a query string.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from . import integration_registry
from .models import IntegrationCheck

logger = logging.getLogger(__name__)

_REDACTED = "[redacted]"
_TOKENISH = re.compile(
    r"(?i)(bearer\s+|basic\s+|api[_-]?key[=:]\s*|apikey[=:]\s*|key=|(?:access_)?token[=:]\s*"
    r"|password[=:]\s*|passwd[=:]\s*|secret[=:]\s*)[A-Za-z0-9._\-+/=]{6,}")
_SAFE_STR = re.compile(r"[A-Za-z0-9 ._:/\-]{0,120}")
_OBSERVED_LIST_MAX = 10


def redact(text: str, extra=()) -> str:
    """Remove configured credential values, ``extra`` secrets and token-shaped strings from ``text``.

    ``extra`` carries secrets the server environment does not hold: a
    Connected account's decrypted credentials (``run_account_check``).
    """
    if not text:
        return text
    values = set(integration_registry.local_secret_values())
    values.update(v for v in extra if isinstance(v, str) and len(v) >= 4)
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, _REDACTED)
    return _TOKENISH.sub(lambda m: m.group(1) + _REDACTED, text)


def _safe_scalar(value: Any, extra=()) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and _SAFE_STR.fullmatch(value):
        return redact(value, extra)
    return None


def sanitize_observed(observed: Any, extra=()) -> dict[str, Any]:
    """Keep only scalar values and short plain strings (or short lists of them)."""
    if not isinstance(observed, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in observed.items():
        if not isinstance(key, str) or not _SAFE_STR.fullmatch(key):
            continue
        if isinstance(value, (list, tuple)):
            out[key] = [v for v in (_safe_scalar(v, extra) for v in value[:_OBSERVED_LIST_MAX]) if v is not None]
        else:
            out[key] = _safe_scalar(value, extra)
    return out


def result(ok: bool, what: str, *, error_class: str = "", http_status: int | None = None,
           latency_ms: int | None = None, observed: dict[str, Any] | None = None) -> dict[str, Any]:
    """The dict a registered connection test returns."""
    return {"ok": bool(ok), "error_class": error_class, "what": what, "http_status": http_status,
            "latency_ms": latency_ms, "observed": observed or {}}


def _clean(raw: Any, extra=()) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("ok"), bool) or not isinstance(raw.get("what"), str):
        return result(False, "The connection test returned no usable result.",
                      error_class="integration_unexpected_response")
    http_status = raw.get("http_status")
    latency_ms = raw.get("latency_ms")
    error_class = raw.get("error_class") or ""
    if not raw["ok"] and not error_class:
        error_class = "integration_failed"
    return {
        "ok": raw["ok"],
        "error_class": redact(str(error_class), extra)[:64],
        "what": redact(raw["what"], extra)[:500],
        "http_status": http_status if isinstance(http_status, int) and 0 <= http_status < 1000 else None,
        "latency_ms": latency_ms if isinstance(latency_ms, int) and latency_ms >= 0 else None,
        "observed": sanitize_observed(raw.get("observed"), extra),
    }


def run_check(target: str, *, user=None) -> dict[str, Any]:
    """Run one connection test, persist it, and return the nonsecret result.

    Raises ``KeyError`` when ``target`` is not a runnable test (registered and
    listed by a ``managed`` row). Never raises for a provider failure or a
    crash inside the test.
    """
    tests = integration_registry.connection_tests()
    if target not in integration_registry.available_targets():
        raise KeyError(target)
    started = time.monotonic()
    try:
        cleaned = _clean(tests[target].func())
    except Exception as exc:  # a bug in the test itself must still be explained
        # Deliberately NOT logger.exception: a traceback (and an error
        # tracker's frame locals) would carry the key held by the crashing frame.
        logger.error("[integrations] test %s crashed: %s", target, type(exc).__name__)
        cleaned = _clean(result(False, f"The connection test crashed ({type(exc).__name__}).",
                                error_class="integration_unexpected_response"))
    if cleaned["latency_ms"] is None:
        cleaned["latency_ms"] = int((time.monotonic() - started) * 1000)
    row = IntegrationCheck.objects.create(
        target=target, ok=cleaned["ok"], error_class=cleaned["error_class"], what=cleaned["what"],
        http_status=cleaned["http_status"], latency_ms=cleaned["latency_ms"],
        observed=cleaned["observed"], tested_by=user if getattr(user, "pk", None) else None,
    )
    logger.info("[integrations] test %s ok=%s class=%s by=%s", target, cleaned["ok"],
                cleaned["error_class"] or "-", getattr(user, "pk", None))
    return dict(cleaned, target=target, tested_at=row.created_at.isoformat(), check_id=str(row.pk))


def last_results(targets=None) -> dict[str, IntegrationCheck | None]:
    """The newest persisted server-level check per target (None when never run)."""
    targets = integration_registry.available_targets() if targets is None else targets
    return {
        t: IntegrationCheck.objects.filter(target=t, company__isnull=True)
        .select_related("tested_by").order_by("-created_at").first()
        for t in targets
    }


# -- Connected accounts (docs/crm/phase6-adapters.md 2.7, problem 15) ----------


def account_target(account) -> str:
    return f"{account.provider}:{account.pk}"


def run_account_check(account, *, user=None, transport=None) -> dict[str, Any]:
    """Run ``account``'s adapter ``test_connection`` and persist it for that tenant only.

    The stored sentence and observed values are redacted against the
    server's secrets **and every decrypted secret of the account**, so a
    provider echoing the key back never reaches the row, the page or the
    JSON answer. Never raises for a provider failure or a crash in the test.
    """
    from core.integrations.accounts import account_secret_values, adapter_for

    target = account_target(account)
    extra: list = []
    started = time.monotonic()
    adapter = None
    try:
        extra = account_secret_values(account)
        adapter = adapter_for(account, transport)
        raw = adapter.test_connection()
        extra = sorted(set(extra) | set(adapter.revealed_values()), key=len, reverse=True)
        cleaned = _clean(raw, extra)
    except LookupError:
        cleaned = _clean(result(False, "This provider's adapter is not installed on this server.",
                                error_class="integration_adapter_missing"))
    except Exception as exc:  # AdapterError included: its text is the adapter's, redact anyway
        logger.error("[integrations] account test %s crashed: %s", target, type(exc).__name__)
        from core.plugins.payroll import AdapterError

        what = (str(exc) if isinstance(exc, AdapterError)
                else f"The connection test crashed ({type(exc).__name__}).")
        if adapter is not None:
            extra = sorted(set(extra) | set(adapter.revealed_values()), key=len, reverse=True)
        cleaned = _clean(result(False, what, error_class="integration_unexpected_response"), extra)
    if cleaned["latency_ms"] is None:
        cleaned["latency_ms"] = int((time.monotonic() - started) * 1000)
    row = IntegrationCheck.objects.create(
        target=target, company_id=account.company_id, ok=cleaned["ok"],
        error_class=cleaned["error_class"], what=cleaned["what"], http_status=cleaned["http_status"],
        latency_ms=cleaned["latency_ms"], observed=cleaned["observed"],
        tested_by=user if getattr(user, "pk", None) else None,
    )
    logger.info("[integrations] account test %s ok=%s class=%s by=%s", target, cleaned["ok"],
                cleaned["error_class"] or "-", getattr(user, "pk", None))
    return dict(cleaned, target=target, tested_at=row.created_at.isoformat(), check_id=str(row.pk))


def last_account_results(accounts) -> dict[str, IntegrationCheck | None]:
    """The newest check per account, read within each account's own company."""
    return {
        str(a.pk): IntegrationCheck.objects.filter(target=account_target(a), company_id=a.company_id)
        .select_related("tested_by").order_by("-created_at").first()
        for a in accounts
    }
