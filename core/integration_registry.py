"""External-service inventory registry: the loader behind the Integrations page.

Same config-loader shape as ``theme_registry.py`` / ``nav_registry.py``
(golden §3.7 pattern): ``settings.ERP_INTEGRATIONS`` is a list of service dicts
supplied by the instance (``golden_erp/integrations.py`` by default) and validated at
import. Bad config raises ``ImproperlyConfigured`` instead of dropping the row:
a service that silently vanishes from the inventory is indistinguishable from
one nobody uses.

The vocabulary an instance may use is config too:

- ``ERP_INTEGRATION_APPS``: the deployable apps that consume services
  (the ERP itself, plus any storefront or worker the instance runs).
- ``ERP_INTEGRATION_LOCAL_APP``: which of those is THIS server, the only app
  whose credential presence can be inspected here.
- ``ERP_INTEGRATION_OWNERS``: who may own a row; must include ``unassigned``.

Connection tests are code, not config. An app (or plugin) registers one with
``register_connection_test("name", func)``; a ``managed`` row lists the test
names it runs. Because plugins register from their own ``AppConfig.ready()``,
references are checked after every app is ready, by a system check
(``core.E030``) and by ``check_connection_tests()``, which raises
``ImproperlyConfigured``. ``core.integration_checks.run_check`` runs a test and
persists the result as a ``core.IntegrationCheck`` row.

Secrets: this module handles credential NAMES only. ``credential_presence``
reports whether a name is set on THIS server as a bool; it never returns, logs
or stores a value. Nothing here writes a key.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

from django.conf import settings
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

STATUSES = {
    "managed": "Managed here: configuration shown, connection test runs from this page",
    "inventoried": "Inventoried: tracked with credential presence; no test on this page yet",
    "bootstrap": "Bootstrap: externally managed; status and ownership only, never editable",
    "inactive": "Inactive: referenced in code but not live",
}
CATEGORIES = (
    "llm", "internal", "storage", "database", "auth", "payments", "shipping",
    "marketplace", "email", "search", "scraping", "seo", "content",
    "observability", "hardware",
)
ROLES = ("secret", "config")
ENTRY_FIELDS = frozenset({
    "key", "service", "category", "consumers", "credentials", "owner",
    "owner_basis", "status", "bootstrap", "connection_tests", "notes",
})

KEY_ROTATION = "Key rotation: deployment-managed (nothing on this page writes a key)"

#: A credential reference must be an environment/setting NAME. Anything else
#: (lower case, punctuation, a 40-char token) is rejected so a value pasted into
#: the registry cannot reach the page.
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_APP_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
#: Test names: snake_case, optionally ``family:variant`` (``llm_stage:chat``).
_TEST_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}(:[a-z0-9_-]{1,47})?$")


# -- vocabulary ---------------------------------------------------------------


def _str_tuple(setting: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = getattr(settings, setting, default)
    if not isinstance(value, (list, tuple)) or not value or not all(
        isinstance(v, str) and v for v in value
    ):
        raise ImproperlyConfigured(f"{setting} must be a non-empty list of strings.")
    if len(set(value)) != len(value):
        raise ImproperlyConfigured(f"{setting} lists a value twice.")
    return tuple(value)


def build_vocabulary() -> dict[str, Any]:
    apps = _str_tuple("ERP_INTEGRATION_APPS", ("erp",))
    bad = [a for a in apps if not _APP_RE.match(a)]
    if bad:
        raise ImproperlyConfigured(f"ERP_INTEGRATION_APPS has invalid app name(s): {bad}.")
    local = getattr(settings, "ERP_INTEGRATION_LOCAL_APP", "erp")
    if local not in apps:
        raise ImproperlyConfigured(
            f"ERP_INTEGRATION_LOCAL_APP={local!r} is not one of ERP_INTEGRATION_APPS {apps}."
        )
    owners = _str_tuple("ERP_INTEGRATION_OWNERS", ("engineering", "operations", "unassigned"))
    if "unassigned" not in owners:
        raise ImproperlyConfigured("ERP_INTEGRATION_OWNERS must include 'unassigned'.")
    return {"apps": apps, "local_app": local, "owners": owners}


# -- entries ------------------------------------------------------------------


def _where(i: int, key: Any = None) -> str:
    return f"ERP_INTEGRATIONS[{i}]" + (f" ({key!r})" if key else "")


def _validate_entry(entry: Any, i: int, vocab: dict[str, Any]) -> dict[str, Any]:
    apps, owners = vocab["apps"], vocab["owners"]
    where = _where(i)
    if not isinstance(entry, dict):
        raise ImproperlyConfigured(f"{where} must be a dict, got {type(entry).__name__}.")
    key = entry.get("key")
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise ImproperlyConfigured(f"{where} needs a snake_case 'key', got {key!r}.")
    where = _where(i, key)
    unknown = set(entry) - ENTRY_FIELDS
    if unknown:
        raise ImproperlyConfigured(f"{where} has unknown field(s): {', '.join(sorted(unknown))}.")
    if not isinstance(entry.get("service"), str) or not entry["service"].strip():
        raise ImproperlyConfigured(f"{where} needs a non-empty 'service'.")
    if entry.get("category") not in CATEGORIES:
        raise ImproperlyConfigured(f"{where}['category'] is {entry.get('category')!r}; one of {CATEGORIES}.")
    status = entry.get("status")
    if status not in STATUSES:
        raise ImproperlyConfigured(f"{where}['status'] is {status!r}; one of {tuple(STATUSES)}.")
    owner = entry.get("owner")
    if owner not in owners:
        raise ImproperlyConfigured(f"{where}['owner'] is {owner!r}; one of {owners}.")
    if owner != "unassigned" and not entry.get("owner_basis"):
        raise ImproperlyConfigured(
            f"{where} names owner {owner!r} without an 'owner_basis': say where that "
            "ownership comes from, or mark it 'unassigned'."
        )
    bootstrap = entry.get("bootstrap", False)
    if not isinstance(bootstrap, bool):
        raise ImproperlyConfigured(f"{where}['bootstrap'] must be True or False.")
    if bootstrap != (status == "bootstrap"):
        raise ImproperlyConfigured(
            f"{where}: bootstrap=True and status='bootstrap' must go together "
            "(bootstrap credentials are shown, never managed)."
        )
    consumers = entry.get("consumers")
    if not isinstance(consumers, list) or not consumers:
        raise ImproperlyConfigured(f"{where} needs at least one consumer.")
    for c in consumers:
        if not isinstance(c, dict) or c.get("app") not in apps or not c.get("purpose") or not c.get("source"):
            raise ImproperlyConfigured(
                f"{where} consumer {c!r} needs app (one of {apps}), purpose and source."
            )
    creds = entry.get("credentials")
    if not isinstance(creds, list):
        raise ImproperlyConfigured(f"{where}['credentials'] must be a list (may be empty).")
    seen = set()
    for c in creds:
        if not isinstance(c, dict):
            raise ImproperlyConfigured(f"{where} has a credential that is not a dict.")
        extra = set(c) - {"name", "app", "role", "note"}
        if extra:
            raise ImproperlyConfigured(f"{where} credential has unknown field(s): {sorted(extra)}.")
        name = c.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            # Do not echo the offending value: it may be a secret pasted by mistake.
            raise ImproperlyConfigured(
                f"{where} has a credential reference that is not an UPPER_SNAKE setting "
                "name. Credentials are referenced by NAME only, never by value."
            )
        if c.get("app") not in apps:
            raise ImproperlyConfigured(f"{where} credential {name} needs app (one of {apps}).")
        if c.get("role") not in ROLES:
            raise ImproperlyConfigured(f"{where} credential {name} needs role (one of {ROLES}).")
        if (name, c["app"]) in seen:
            raise ImproperlyConfigured(f"{where} lists credential {name} for {c['app']} twice.")
        seen.add((name, c["app"]))
    tests = entry.get("connection_tests", [])
    if not isinstance(tests, list) or any(not isinstance(t, str) or not _TEST_RE.match(t) for t in tests):
        raise ImproperlyConfigured(
            f"{where}['connection_tests'] must be a list of test names (snake_case, "
            "optionally 'family:variant')."
        )
    if tests and status != "managed":
        raise ImproperlyConfigured(f"{where} has connection tests but status {status!r}; only 'managed' rows test.")
    if status == "managed" and not tests:
        raise ImproperlyConfigured(f"{where} is 'managed' but names no connection test.")
    return dict(entry, bootstrap=bootstrap, connection_tests=list(tests))


def build_integrations(vocab: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    vocab = vocab or build_vocabulary()
    configured = getattr(settings, "ERP_INTEGRATIONS", []) or []
    if not isinstance(configured, (list, tuple)):
        raise ImproperlyConfigured(
            f"ERP_INTEGRATIONS must be a list of service dicts, got {type(configured).__name__}."
        )
    out = [_validate_entry(e, i, vocab) for i, e in enumerate(configured)]
    dupes = [k for k, n in Counter(e["key"] for e in out).items() if n > 1]
    if dupes:
        raise ImproperlyConfigured(f"ERP_INTEGRATIONS has duplicate key(s): {', '.join(sorted(dupes))}.")
    tested = Counter(t for e in out for t in e["connection_tests"])
    doubled = [t for t, n in tested.items() if n > 1]
    if doubled:
        raise ImproperlyConfigured(f"Connection test(s) claimed by two rows: {', '.join(sorted(doubled))}.")
    return out


# Mutated in place on reload so `from … import INTEGRATIONS` sees updates.
VOCABULARY: dict[str, Any] = build_vocabulary()
INTEGRATIONS: list[dict[str, Any]] = build_integrations(VOCABULARY)

_RELOAD_ON = frozenset({
    "ERP_INTEGRATIONS", "ERP_INTEGRATION_APPS", "ERP_INTEGRATION_LOCAL_APP", "ERP_INTEGRATION_OWNERS",
})


@receiver(setting_changed)
def _reload(sender, setting, **kwargs) -> None:
    if setting not in _RELOAD_ON:
        return
    vocab = build_vocabulary()
    rebuilt = build_integrations(vocab)
    VOCABULARY.clear()
    VOCABULARY.update(vocab)
    INTEGRATIONS[:] = rebuilt


# -- connection tests ---------------------------------------------------------


@dataclass(frozen=True)
class ConnectionTest:
    """One registered connection test.

    ``func()`` takes no arguments and returns ``core.integration_checks.result(...)``.
    It must call the provider's cheapest non-billable endpoint, never a
    generation, and must not put a credential in a URL.
    """

    name: str
    func: Callable[[], dict[str, Any]]
    label: str = ""


_TESTS: dict[str, ConnectionTest] = {}


def register_connection_test(name: str, func: Callable[[], dict[str, Any]] | None = None, *, label: str = ""):
    """Register ``func`` as connection test ``name``. Usable as a decorator.

    Registering a second, different function under a taken name raises
    ``ImproperlyConfigured``; re-registering the same function is a no-op, so
    an ``AppConfig.ready()`` that runs twice is harmless.
    """

    def _register(f: Callable[[], dict[str, Any]]):
        if not isinstance(name, str) or not _TEST_RE.match(name):
            raise ImproperlyConfigured(
                f"Connection test name {name!r} must be snake_case, optionally 'family:variant'."
            )
        if not callable(f):
            raise ImproperlyConfigured(f"Connection test {name!r} must be callable.")
        existing = _TESTS.get(name)
        if existing is not None and existing.func is not f:
            raise ImproperlyConfigured(f"Connection test {name!r} is already registered.")
        _TESTS[name] = ConnectionTest(name=name, func=f, label=label or name)
        return f

    return _register(func) if func is not None else _register


def unregister_connection_test(name: str) -> None:
    """Remove a registered test (tests and plugin teardown)."""
    _TESTS.pop(name, None)


def connection_tests() -> dict[str, ConnectionTest]:
    """Registered tests by name (a copy)."""
    return dict(_TESTS)


def available_targets() -> list[str]:
    """Tests a ``managed`` row lists AND that are registered: what the page may run."""
    return [t for e in INTEGRATIONS for t in e["connection_tests"] if t in _TESTS]


def check_connection_tests() -> None:
    """Raise ``ImproperlyConfigured`` if a row names a test nobody registered."""
    missing = sorted({t for e in INTEGRATIONS for t in e["connection_tests"]} - set(_TESTS))
    if missing:
        raise ImproperlyConfigured(
            f"ERP_INTEGRATIONS names connection test(s) that are not registered: "
            f"{', '.join(missing)}. Register them with "
            "core.integration_registry.register_connection_test()."
        )


@checks.register()
def _connection_tests_check(app_configs=None, **kwargs):
    try:
        check_connection_tests()
    except ImproperlyConfigured as exc:
        return [checks.Error(str(exc), id="core.E030")]
    return []


# -- render helpers -----------------------------------------------------------


def credential_presence(name: str, app: str) -> bool | None:
    """Whether ``name`` is set on THIS server, or None when it is another app's.

    Returns a bool only; the value is never returned, logged or stored.
    """
    if app != VOCABULARY["local_app"]:
        return None
    if hasattr(settings, name):
        return bool(getattr(settings, name))
    return bool(os.environ.get(name))


def local_secret_values() -> list[str]:
    """Configured values of every LOCAL secret the registry names, longest first.

    For redaction only (``core.integration_checks.redact``); never render these.
    """
    values = set()
    for entry in INTEGRATIONS:
        for c in entry["credentials"]:
            if c["app"] != VOCABULARY["local_app"] or c["role"] != "secret":
                continue
            raw = getattr(settings, c["name"], None)
            if isinstance(raw, (list, tuple)):  # a key list (GOLDEN_SECRET_KEYS): each entry
                values.update(v for v in raw if isinstance(v, str) and len(v) >= 6)
            if not isinstance(raw, str) or not raw:
                raw = os.environ.get(c["name"], "")
            if isinstance(raw, str) and len(raw) >= 6:
                values.add(raw)
                values.update(v.strip() for v in raw.split(",") if len(v.strip()) >= 6)
    return sorted(values, key=len, reverse=True)


def rows() -> list[dict[str, Any]]:
    """Render-ready rows: the registry plus per-credential presence (bools)."""
    out = []
    for entry in INTEGRATIONS:
        creds = [
            {"name": c["name"], "app": c["app"], "role": c["role"], "note": c.get("note", ""),
             "present": credential_presence(c["name"], c["app"])}
            for c in entry["credentials"]
        ]
        out.append({**entry, "credentials": creds,
                    "apps": sorted({c["app"] for c in entry["consumers"]}),
                    "status_label": STATUSES[entry["status"]]})
    return out


def summary() -> dict[str, Any]:
    """Counts by status and by owner, plus the unassigned list."""
    owners = VOCABULARY["owners"]
    return {
        "total": len(INTEGRATIONS),
        "by_status": {s: sum(1 for e in INTEGRATIONS if e["status"] == s) for s in STATUSES},
        "by_owner": {o: sum(1 for e in INTEGRATIONS if e["owner"] == o) for o in owners},
        "unassigned": [e["service"] for e in INTEGRATIONS if e["owner"] == "unassigned"],
    }


def credential_names() -> set[tuple[str, str]]:
    """Every (name, app) the registry references, for inventory-coverage tests."""
    return {(c["name"], c["app"]) for e in INTEGRATIONS for c in e["credentials"]}
