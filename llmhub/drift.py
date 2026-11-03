"""Drift between what this deployment routes to and what the gateway offers.

Lanes live in the gateway's database, not in git, so the failure this
catches is "someone renamed or deleted a lane in the gateway UI" — found here
on a schedule rather than by a user's chat turn.

Three separate statuses, never one conflated verdict:

- ``gateway``     ok | down | n/a  — can we list models at all? (down = outage)
- ``drift``       expected routes the gateway does not offer (informational
                  unless the caller chooses to fail on it)
- ``embed``       ok | skipped | mismatch | error — the dimension probe

The embedding probe is a *paid* call (one embedding per run). It needs its own
credential (``LLMHUB_EMBED_PROBE_API_KEY``) because the ERP's key is scoped to
the ERP's own lanes; without it the probe is skipped with a warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.conf import settings

from . import client, conf
from .purposes import load_purposes


@dataclass
class DriftReport:
    mode: str
    gateway: str = "n/a"
    gateway_error: str = ""
    available: list[str] = field(default_factory=list)
    expected: dict[str, list[str]] = field(default_factory=dict)  # route → who routes to it
    missing: list[str] = field(default_factory=list)
    credentials: dict[str, str] = field(default_factory=dict)  # purpose → key source
    embed: str = "skipped"
    embed_message: str = ""

    @property
    def drift(self) -> bool:
        return bool(self.missing)


def expected_routes(snap: conf.Snapshot) -> dict[str, list[str]]:
    from .models import LLMModel

    expected: dict[str, list[str]] = {}
    for name in load_purposes():
        route = conf.route_for(name, snap).value
        if route:
            expected.setdefault(route, []).append(f"purpose:{name}")
    if snap.gateway:
        for row in LLMModel.objects.filter(enabled=True):
            expected.setdefault(row.route, []).append(f"chat:{row.key}")
    return expected


def run(*, embed_probe: bool = True, timeout: float = 30.0) -> DriftReport:
    snap = conf.snapshot(fresh=True)
    report = DriftReport(mode=snap.mode)
    report.expected = expected_routes(snap)
    for name in load_purposes():
        report.credentials[name] = snap.api_key.source

    if snap.gateway:
        listed = client.available_models(timeout=timeout, snap=snap)
        if listed.ok:
            report.gateway = "ok"
            report.available = listed.ids
            report.missing = sorted(r for r in report.expected if r not in listed.ids)
        else:
            report.gateway = "down"
            report.gateway_error = f"{listed.error_category}: {listed.error_message}"

    if embed_probe:
        key = (getattr(settings, "LLMHUB_EMBED_PROBE_API_KEY", "") or "").strip()
        route = (getattr(settings, "LLMHUB_EMBED_PROBE_ROUTE", "") or "").strip()
        dims = getattr(settings, "LLMHUB_EMBED_PROBE_DIMENSIONS", None)
        # Env only, never the DB/snapshot base URL: this key is not the ERP's,
        # and a page edit must not be able to redirect it.
        base = (getattr(settings, "LLMHUB_EMBED_PROBE_BASE_URL", "") or "").strip()
        if not (key and route and base):
            report.embed = "skipped"
            report.embed_message = "LLMHUB_EMBED_PROBE_API_KEY / _ROUTE / _BASE_URL unset — dimension probe skipped."
        else:
            try:
                vectors = client.embed(
                    ["llmhub drift probe"], route=route, base_url=base, api_key=key,
                    key_source="env", dimensions=int(dims) if dims else None, timeout=timeout,
                )
                report.embed = "ok"
                report.embed_message = f"{route}: {len(vectors[0])}-dim"
            except client.DimensionMismatch as exc:
                report.embed = "mismatch"
                report.embed_message = str(exc)
            except client.LLMHubError as exc:
                report.embed = "error"
                report.embed_message = str(exc)
    return report
