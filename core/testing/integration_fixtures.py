"""Fixture replay for live CRM adapters (docs/crm/phase6-adapters.md 2.3, 5).

Three pieces, shared by every adapter's contract test:

- :class:`FixtureTransport` answers each request with the next matching
  recorded response envelope and records the request, with every secret the
  adapter revealed replaced by ``<redacted:kind>``. A request with no
  recorded response raises :class:`FixtureMissing` naming it ("no recorded
  response for GET /v1/contacts"), never a ``LiveSocketError`` deep in httpx.
- :func:`snapshot` dumps what a tenant holds after a case, canonically:
  golden uuids become fixture keys, rows are sorted, JSON has sorted keys.
- :func:`run_case` builds a tenant from a case directory, runs its steps
  (ingest, webhook, poll, enqueue, drain), and returns the recorded requests
  and the snapshot. ``core.plugins.crm_adapter.assert_crm_adapter_contract``
  compares them with ``requests.expected.json`` / ``db.expected.json``, runs
  the case again and asserts nothing changed.

Case layout (``golden_erp/addons/<provider>/fixtures/<case>/``)::

    case.json              {"proves": "...", "now": "2026-03-02T15:00:00Z",
                            "seed": {"parties": [...], "staff": [...], "refs": [...]},
                            "steps": [...], "expect": "ok" | "refused",
                            "second_run_requests": []}
    account.json           {"settings": {...}, "enabled": true}  (no secrets)
    responses/*.json       {"schema_version", "provider", "request": {"method", "path", "query"?},
                            "response": {"status", "headers"}?, "payload", "repeatable"?}
    inbound/*.json         {"schema_version", "provider", "request"?: {"headers"}, "payload"}
    requests.expected.json
    db.expected.json

Steps: ``{"ingest": "inbound/a.json"}``, ``{"webhook": "inbound/a.json",
"status": 200}``, ``{"poll": true}``, ``{"enqueue": {"consumer": ..., "key":
..., "object": "party:ada", "payload": {...}}}``, ``{"drain": true}``. A
step may carry ``"raises": "AdapterError"``.

Secrets are invented by the runner, never stored in a fixture:
:func:`fixture_secret` gives each credential kind a fixed value, so a
webhook fixture can carry a precomputed signature.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

from core.integrations.transport import Response

__all__ = [
    "CaseResult",
    "FixtureMissing",
    "FixtureTransport",
    "canonical_json",
    "fixture_secret",
    "load_json",
    "run_case",
    "snapshot",
]

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class FixtureMissing(AssertionError):
    """The adapter made a request the case has no recorded response for."""


def load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, default=str) + "\n"


def fixture_secret(kind: str) -> str:
    """The invented secret the runner stores for ``kind`` (never a real credential)."""
    if kind == "oauth_client":
        return json.dumps({"client_id": "fixture-client-id", "client_secret": "fixture-client-secret-1"},
                          sort_keys=True)
    return f"fixture-{kind.replace('_', '-')}-1"


# -- the transport -------------------------------------------------------------


class FixtureTransport:
    """A ``Transport`` that replays recorded envelopes and records every request."""

    def __init__(self, case_dir: Path | None = None, responses: list[dict] | None = None):
        self.responses: list[dict] = list(responses or [])
        if case_dir is not None:
            directory = Path(case_dir) / "responses"
            if directory.is_dir():
                self.responses += [load_json(p) for p in sorted(directory.glob("*.json"))]
        self._used: set[int] = set()
        self.recorded: list[dict] = []
        self._secrets: dict[str, str] = {}

    # BaseCrmAdapter.secret() calls this for every value it decrypts.
    def register_secret(self, value: str, kind: str) -> None:
        if value:
            self._secrets[value] = kind

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in sorted(self._secrets, key=len, reverse=True):
                value = value.replace(secret, f"<redacted:{self._secrets[secret]}>")
            return value
        if isinstance(value, dict):
            return {self._redact(k): self._redact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact(v) for v in value]
        return value

    def request(self, method, url, *, headers, params=None, json=None, data=None, timeout=10.0):
        in_url = url + "?" + "&".join(f"{k}={v}" for k, v in (params or {}).items())
        for secret, kind in self._secrets.items():
            if secret in in_url:
                # httpx logs every URL at INFO: a secret in a URL or query reaches the logs.
                raise AssertionError(f"a {kind} credential was put in the URL; send it in a header or body")
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        query.update({str(k): str(v) for k, v in (params or {}).items()})
        entry = self._redact({
            "method": method.upper(), "host": parts.netloc, "path": parts.path, "query": query,
            "headers": {k.lower(): v for k, v in (headers or {}).items()},
            "json": json, "form": dict(data) if isinstance(data, dict) else None,
        })
        self.recorded.append(entry)
        for index, response in enumerate(self.responses):
            if index in self._used and not response.get("repeatable"):
                continue
            req = response.get("request") or {}
            if str(req.get("method", "GET")).upper() != entry["method"] or req.get("path") != entry["path"]:
                continue
            want = req.get("query")
            if want is not None and {str(k): str(v) for k, v in want.items()} != entry["query"]:
                continue
            self._used.add(index)
            meta = response.get("response") or {}
            payload = response.get("payload")
            body = b"" if payload is None else _json_bytes(payload)
            return Response(status=int(meta.get("status", 200)),
                            headers={k.lower(): str(v) for k, v in (meta.get("headers") or {}).items()},
                            body=body)
        raise FixtureMissing(
            f"no recorded response for {entry['method']} {entry['path']}"
            + (f"?{'&'.join(f'{k}={v}' for k, v in sorted(entry['query'].items()))}" if entry["query"] else "")
            + " — add one under responses/ or fix the adapter's request."
        )

    def comparable(self) -> list[dict]:
        """The recorded requests in the expected-file shape (host dropped: it is the adapter's base URL)."""
        return [{k: v for k, v in r.items() if k != "host"} for r in self.recorded]


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def webhook_body(item: dict) -> bytes:
    """The bytes a webhook fixture is posted as: its payload, canonical compact JSON."""
    return _json_bytes(item["payload"])


# -- the snapshot --------------------------------------------------------------


class Keys:
    """Golden uuid -> stable fixture key (``party:ada``)."""

    def __init__(self):
        self.by_id: dict[str, str] = {}

    def add(self, key: str, obj) -> None:
        self.by_id[str(obj.pk)] = key

    def of(self, value) -> str | None:
        if value is None:
            return None
        return self.by_id.get(str(value), "<new>")

    def scrub(self, text: Any) -> Any:
        if not isinstance(text, str):
            return text
        return UUID_RE.sub(lambda m: self.by_id.get(m.group(0), "<uuid>"), text)

    def scrub_all(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {self.scrub(k): self.scrub_all(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub_all(v) for v in value]
        return self.scrub(value)


def _iso(value) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def snapshot(company, keys: Keys | None = None) -> dict:
    """What ``company`` holds that an adapter can write, canonical and uuid-free (5.2)."""
    from communications.models import Communication
    from core.models import ExternalRef, OutboxEvent
    from crm.models import Task, Touchpoint
    from parties.models import ConsentRecord, Suppression

    keys = keys or Keys()
    comms = []
    for comm in Communication.objects.filter(company=company).prefetch_related("participants"):
        comms.append({
            "kind": comm.kind, "direction": comm.direction, "status": comm.status, "subject": comm.subject,
            "thread_key": keys.scrub(comm.thread_key), "provider": comm.provider,
            "occurred_at": _iso(comm.occurred_at), "party": keys.of(comm.party_id),
            "engagement": keys.of(comm.engagement_id),
            "participants": sorted(
                ({"role": p.role, "address": p.address_normalized, "party": keys.of(p.party_id)}
                 for p in comm.participants.all()),
                key=lambda p: (p["role"], p["address"])),
        })
    tasks = [{
        "title": t.title, "status": t.status, "due_at": _iso(t.due_at), "all_day": t.all_day,
        "assignee": keys.of(t.assignee_id), "party": keys.of(t.party_id), "source": t.source,
    } for t in Task.objects.filter(company=company)]
    consents = [{
        "party": keys.of(c.party_id), "channel": c.channel, "purpose": c.purpose, "state": c.state,
        "source": c.source, "occurred_at": _iso(c.occurred_at),
    } for c in ConsentRecord.objects.filter(company=company)]
    suppressions = [{
        "channel": s.channel, "reason": s.reason, "hint": s.address_hint, "source": s.source,
        "lifted": s.lifted_at is not None,
    } for s in Suppression.objects.filter(company=company)]
    touchpoints = [{
        "party": keys.of(t.party_id), "occurred_at": _iso(t.occurred_at), "channel": t.channel,
        "utm_source": t.utm_source, "utm_campaign": t.utm_campaign,
    } for t in Touchpoint.objects.filter(company=company)]
    refs = [{
        "source": r.source, "object_type": r.object_type, "source_id": r.source_id,
        "target": f"{r.content_type.app_label}.{r.content_type.model}:{keys.of(r.object_id)}",
        "source_url": r.source_url, "metadata": r.metadata,
    } for r in ExternalRef._base_manager.filter(company=company).select_related("content_type")]
    outbox_rows = [{
        "consumer": o.consumer, "event": o.event, "idempotency_key": keys.scrub(o.idempotency_key),
        "state": o.state, "object": keys.of(o.object_id),
    } for o in OutboxEvent.objects.filter(company=company)]

    def ordered(rows):
        return sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=str))

    return {
        "communications": ordered(comms), "tasks": ordered(tasks), "consents": ordered(consents),
        "suppressions": ordered(suppressions), "touchpoints": ordered(touchpoints),
        "external_refs": ordered(refs), "outbox": ordered(outbox_rows),
    }


# -- the case runner -----------------------------------------------------------


@dataclass
class CaseResult:
    name: str
    requests: list = field(default_factory=list)
    db: dict = field(default_factory=dict)
    second_requests: list = field(default_factory=list)
    second_db: dict = field(default_factory=dict)
    outcomes: list = field(default_factory=list)
    errors: list = field(default_factory=list)  # (step index, exception) for refused cases
    other_tenant_empty: bool = True
    second_outcomes: list = field(default_factory=list)
    second_errors: list = field(default_factory=list)


@dataclass
class Tenant:
    company: Any
    other_company: Any
    account: Any
    keys: Keys


def _parse_now(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@contextlib.contextmanager
def unlimited_buckets():
    """Replays are not paced: each bucket read sees an hour pass (rate limits have their own tests)."""
    from core.integrations import ratelimit

    ticks = iter(range(1, 10**9))
    with mock.patch.object(ratelimit, "_monotonic", lambda: next(ticks) * 3600.0):
        yield


class PinnedClock:
    """``timezone.now`` for a case: fixed at ``case.json`` "now", moved only by an ``advance`` step."""

    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


@contextlib.contextmanager
def pinned_now(value):
    """Pin ``django.utils.timezone.now`` for the case (``case.json`` "now")."""
    start = _parse_now(value)
    if start is None:
        yield None
        return
    clock = PinnedClock(start)
    with mock.patch("django.utils.timezone.now", side_effect=clock):
        yield clock


def build_tenant(adapter_cls, case_dir: Path, case: dict,
                 seed_hook: Callable[[Tenant, dict], None] | None = None) -> Tenant:
    from django.contrib.auth import get_user_model

    from core.integrations import accounts as services
    from core.models import Company, ExternalRef, IntegrationAccount, UserProfile
    from parties.models import Party, PartyEmail, PartyPhone

    keys = Keys()
    company = Company.objects.create(name="Fixture tenant")
    other = Company.objects.create(name="Other tenant")
    keys.add("company", company)
    seed = case.get("seed") or {}
    for spec in seed.get("parties", []):
        party = Party.objects.create(
            company=company, kind=Party.Kind.PERSON, given_name=spec.get("given", ""),
            family_name=spec.get("family", ""), origin=Party.Origin.ERP,
        )
        keys.add(f"party:{spec['key']}", party)
        for address in ([spec["email"]] if spec.get("email") else []) + spec.get("emails", []):
            PartyEmail.objects.create(party=party, email=address, purpose="work")
        if spec.get("phone"):
            PartyPhone.objects.create(party=party, number_raw=spec["phone"], purpose="work")
    for spec in seed.get("staff", []):
        user = get_user_model().objects.create_user(spec["key"], email=spec.get("email", ""), password="x",
                                                     is_staff=True)
        profile = UserProfile.objects.create(user=user, company=company, display_name=spec["key"])
        keys.add(f"staff:{spec['key']}", profile)
    account_spec = load_json(case_dir / "account.json") if (case_dir / "account.json").exists() else {}
    account = IntegrationAccount.objects.create(
        company=company, provider=adapter_cls.provider_id, settings=account_spec.get("settings", {}),
        enabled=account_spec.get("enabled", True),
    )
    keys.add("account", account)
    for kind in sorted(adapter_cls.credential_kinds):
        if kind in ("oauth_refresh", "oauth_access"):
            continue
        services.set_credential(account, kind, fixture_secret(kind))
    from django.contrib.contenttypes.models import ContentType

    for spec in seed.get("refs", []):
        target_key = spec["target"]
        target_id = next(k for k, v in keys.by_id.items() if v == target_key)
        model = Party if target_key.startswith("party:") else UserProfile
        meta = {"erased": True} if spec.get("erased") else {}
        ExternalRef.objects.create(
            company=company, source=spec.get("source", adapter_cls.provider_id),
            object_type=spec["object_type"], source_id=spec["source_id"],
            content_type=ContentType.objects.get_for_model(model), object_id=target_id, metadata=meta,
        )
    for spec in seed.get("grants", []):
        from datetime import timedelta

        from django.utils import timezone

        from core.models import IntegrationCredential

        owner = UserProfile.objects.get(pk=next(k for k, v in keys.by_id.items() if v == spec["owner"]))
        for kind, value in (("oauth_refresh", fixture_secret("oauth_refresh")),
                            ("oauth_access", fixture_secret("oauth_access"))):
            expires = spec.get("access_expires_in", 3600) if kind == "oauth_access" else None
            services.set_credential(
                account, kind, value, owner=owner, subject=spec.get("subject", ""),
                expires_at=timezone.now() + timedelta(seconds=expires) if expires is not None else None,
            )
        IntegrationCredential.objects.filter(account=account, owner=owner).update(
            sync_state=spec.get("sync_state", {}))
    tenant = Tenant(company=company, other_company=other, account=account, keys=keys)
    if seed_hook is not None:
        seed_hook(tenant, case)
    return tenant


def _run_steps(adapter_cls, case_dir: Path, case: dict, tenant: Tenant, transport: FixtureTransport,
               result: CaseResult, clock=None) -> None:
    from django.db import transaction
    from django.test import Client
    from django.urls import reverse

    from core import outbox
    from core.integrations.poll import poll_accounts
    from core.integrations.transport import use_transport
    from core.outbox.testing import drain
    from core.plugins.payroll import AdapterError

    with use_transport(transport):
        for index, step in enumerate(case.get("steps", [])):
            raises = step.get("raises")
            try:
                if "ingest" in step:
                    item = load_json(case_dir / step["ingest"])
                    adapter = adapter_cls(tenant.account, transport)
                    with outbox.origin(adapter_cls.provider_id), transaction.atomic():
                        result.outcomes.append(adapter.ingest(item))
                elif "webhook" in step:
                    item = load_json(case_dir / step["webhook"])
                    headers = {f"HTTP_{k.upper().replace('-', '_')}": v
                               for k, v in ((item.get("request") or {}).get("headers") or {}).items()}
                    url = reverse("integration-webhook", args=[adapter_cls.provider_id, tenant.account.pk])
                    response = Client().post(url, data=webhook_body(item), content_type="application/json",
                                             **headers)
                    want = step.get("status", 200)
                    if response.status_code != want:
                        raise AssertionError(f"step {index}: webhook answered {response.status_code}, "
                                             f"expected {want}")
                elif step.get("poll"):
                    poll_accounts(account_id=tenant.account.pk, transport=transport)
                elif "enqueue" in step:
                    spec = step["enqueue"]
                    object_id = None
                    if spec.get("object"):
                        object_id = next(k for k, v in tenant.keys.by_id.items() if v == spec["object"])
                    payload = dict(spec.get("payload") or {})
                    if object_id is not None:
                        payload.setdefault("object_id", object_id)
                    outbox.enqueue(spec["consumer"], outbox.EventSpec(
                        idempotency_key=tenant.keys.scrub(spec.get("key") or f"{spec['consumer']}:{index}"),
                        payload=payload, company_id=tenant.company.pk, event=spec.get("event", "push"),
                        object_type=spec.get("object_type", ""), object_id=object_id,
                    ))
                elif step.get("drain"):
                    drain()
                elif "advance" in step:
                    from datetime import timedelta

                    if clock is None:
                        raise AssertionError(f"step {index}: 'advance' needs a pinned 'now' in case.json")
                    clock.now = clock.now + timedelta(seconds=step["advance"])
                else:
                    raise AssertionError(f"step {index}: unknown step {sorted(step)}")
            except AdapterError as exc:
                if raises is None:
                    raise
                names = {cls.__name__ for cls in type(exc).__mro__}
                if raises not in names:
                    raise AssertionError(f"step {index}: expected {raises}, got {type(exc).__name__}") from exc
                result.errors.append((index, exc))
                continue
            if raises is not None:
                raise AssertionError(f"step {index}: expected {raises}, nothing was raised")


def run_case(adapter_cls, case_dir: Path, *, seed_hook=None) -> CaseResult:
    """Build the tenant, run the steps twice, and return both runs' requests and snapshots."""
    from core.integrations import ratelimit

    case_dir = Path(case_dir)
    case = load_json(case_dir / "case.json")
    result = CaseResult(name=case_dir.name)
    ratelimit.reset()
    with pinned_now(case.get("now")) as clock, unlimited_buckets():
        tenant = build_tenant(adapter_cls, case_dir, case, seed_hook)
        transport = FixtureTransport(case_dir)
        _run_steps(adapter_cls, case_dir, case, tenant, transport, result, clock)
        result.requests = [tenant.keys.scrub_all(r) for r in transport.comparable()]
        result.db = snapshot(tenant.company, tenant.keys)
        before = len(transport.recorded)
        first_outcomes = len(result.outcomes)
        first_errors = len(result.errors)
        ratelimit.reset()
        _run_steps(adapter_cls, case_dir, case, tenant, transport, result, clock)
        result.second_requests = [tenant.keys.scrub_all(r) for r in transport.comparable()[before:]]
        result.second_db = snapshot(tenant.company, tenant.keys)
        result.second_outcomes = result.outcomes[first_outcomes:]
        result.outcomes = result.outcomes[:first_outcomes]
        result.second_errors = result.errors[first_errors:]
        result.errors = result.errors[:first_errors]
        other = snapshot(tenant.other_company)
        result.other_tenant_empty = not any(other.values())
    return result
