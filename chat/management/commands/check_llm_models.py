"""Report LLM routing drift — gateway lanes, credential sources, embeddings,
and (optionally) the Anthropic catalogue.

Model choices are pinned on purpose: registry ids never float to "latest",
and gateway lanes are logical names whose model is chosen in the gateway's
admin UI. That is what we want, but it means nothing *tells* you when the
thing a route names has disappeared. This command is that signal.

Sections, each its own status — never one conflated verdict:

1. **Gateway** (gateway mode): ``GET /v1/models`` with this deployment's key,
   compared with every route it sends traffic to (purpose lanes + enabled
   chat-model rows). Unreachable = an outage; a missing route = drift.
2. **Credential sources**: which source (db / env / default) supplies the key
   each purpose uses — env fallback is flagged for retirement.
3. **Embedding probe**: one embedding through ``LLMHUB_EMBED_PROBE_ROUTE``,
   asserting ``LLMHUB_EMBED_PROBE_DIMENSIONS``. Skipped (with a warning) when
   ``LLMHUB_EMBED_PROBE_API_KEY`` is unset.
4. **Anthropic catalogue** (only when ``ANTHROPIC_API_KEY`` is set): the
   direct-mode registry's Claude entries vs Anthropic's ``/v1/models``.

Contract change, stated: this command used to be read-only and free. The
embedding probe (3) makes **one paid embedding call per run**; pass
``--no-embed-probe`` to keep it free.

Exit status:

    0  healthy (or drift found without --fail-on-drift)
    2  gateway down / unauthorised — an outage, always non-zero
    3  drift (only with --fail-on-drift)
    4  embedding dimension mismatch — always non-zero (it corrupts vectors)

    python manage.py check_llm_models
    python manage.py check_llm_models --fail-on-drift
"""

from __future__ import annotations

from typing import Any

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from chat.llm.anthropic import ANTHROPIC_BASE_URL, ANTHROPIC_VERSION
from chat.llm.registry import build_code_models
from llmhub import drift


class Command(BaseCommand):
    help = "Report LLM routing drift: gateway lanes, credential sources, embeddings, Anthropic catalog."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fail-on-drift",
            action="store_true",
            help="Exit 3 when a routed lane/model is missing upstream or a newer Claude has shipped.",
        )
        parser.add_argument("--no-embed-probe", action="store_true", help="Skip the (paid) embedding probe.")
        parser.add_argument(
            "--all",
            action="store_true",
            help="Anthropic section: also list older models the registry doesn't carry.",
        )
        parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds (default: 30).")

    def handle(self, *args, **options):
        report = drift.run(embed_probe=not options["no_embed_probe"], timeout=options["timeout"])
        drifted = False

        self.stdout.write(self.style.MIGRATE_HEADING(f"Gateway (mode: {report.mode})"))
        if report.gateway == "n/a":
            self.stdout.write("  (direct mode — no gateway to compare against)")
        elif report.gateway == "down":
            self.stdout.write(self.style.ERROR(f"  ✗ gateway unreachable: {report.gateway_error}"))
        else:
            for route, users in sorted(report.expected.items()):
                ok = route not in report.missing
                line = f"  {'✓' if ok else '✗'} {route:<28} used by {', '.join(users)}"
                self.stdout.write(line if ok else self.style.ERROR(line + " — NOT offered to this key"))
            drifted = drifted or report.drift

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Credential sources"))
        for purpose, source in report.credentials.items():
            line = f"  {purpose:<16} key from {source}"
            if source == "env" and report.mode == "gateway":
                line += "  (env fallback — scheduled for retirement)"
                self.stdout.write(self.style.WARNING(line))
            elif source in ("unset", "default"):
                self.stdout.write(self.style.ERROR(line))
            else:
                self.stdout.write(line)

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Embedding probe"))
        embed_line = f"  {report.embed}: {report.embed_message}"
        style = {"ok": self.style.SUCCESS, "skipped": self.style.WARNING}.get(report.embed, self.style.ERROR)
        self.stdout.write(style(embed_line))

        api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
        anthropic = {m.model_id: m for m in build_code_models().values() if m.adapter == "anthropic"}
        if api_key and anthropic:
            self.stdout.write("")
            drifted = self._anthropic_section(api_key, anthropic, options) or drifted

        self.stdout.write("")
        if report.embed == "mismatch":
            raise CommandError("Embedding dimension mismatch (see above).", returncode=4)
        if report.gateway == "down":
            raise CommandError("Gateway unreachable (see above).", returncode=2)
        if drifted and options["fail_on_drift"]:
            raise CommandError("Model drift detected (see above).", returncode=3)
        self.stdout.write(self.style.SUCCESS("Drift check complete." if drifted else "No drift."))

    def _anthropic_section(self, api_key: str, registered: dict[str, Any], options) -> bool:
        """Direct-mode registry's Claude entries vs Anthropic's catalogue. Returns True on drift."""
        self.stdout.write(self.style.MIGRATE_HEADING("Anthropic catalog (direct-mode registry)"))
        try:
            available = self._fetch_models(api_key, options["timeout"])
        except CommandError as exc:
            self.stdout.write(self.style.WARNING(f"  skipped: {exc}"))
            return False
        by_id = {m["id"]: m for m in available}
        missing: list[str] = []
        newest_registered = ""
        for model_id, cfg in registered.items():
            upstream = self._match(model_id, by_id)
            if upstream is None:
                missing.append(model_id)
                self.stdout.write(self.style.ERROR(f"  ✗ {cfg.key:<20} {model_id} — not listed by the API"))
            else:
                newest_registered = max(newest_registered, upstream.get("created_at", ""))
                self.stdout.write(f"  ✓ {cfg.key:<20} {model_id} ({upstream.get('display_name', '?')})")
        # Only models released after our newest entry are upgrade candidates;
        # older tiers we skipped on purpose would otherwise alert forever.
        matched_ids = {m["id"] for mid in registered if (m := self._match(mid, by_id)) is not None}
        unregistered = [m for m in available if m["id"] not in matched_ids]
        newer = [m for m in unregistered if m.get("created_at", "") > newest_registered]
        for m in newer:
            self.stdout.write(f"  · newer: {m['id']:<28} {m.get('display_name', '')}  {m.get('created_at', '')}")
        if options["all"]:
            for m in unregistered:
                if m not in newer:
                    self.stdout.write(f"  · older: {m['id']:<28} {m.get('display_name', '')}")
        if missing:
            self.stdout.write(self.style.ERROR(f"  {len(missing)} registry model(s) unknown upstream — these will 404."))
        return bool(missing or newer)

    @staticmethod
    def _match(model_id: str, by_id: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        """Find the catalogue entry for a registry model id.

        The catalogue and the registry don't always spell a model the same
        way — one may carry the bare alias (`claude-haiku-4-5`) while the
        other carries a dated snapshot (`claude-haiku-4-5-20251001`). Match
        exactly first, then accept either as a prefix of the other so a
        pinned snapshot isn't reported as a retired model.
        """
        if model_id in by_id:
            return by_id[model_id]
        for candidate_id, model in by_id.items():
            if model_id.startswith(f"{candidate_id}-") or candidate_id.startswith(f"{model_id}-"):
                return model
        return None

    def _fetch_models(self, api_key: str, timeout: float) -> list[dict[str, Any]]:
        """Page through GET /v1/models and return every model object."""
        url = f"{ANTHROPIC_BASE_URL.rstrip('/')}/v1/models"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        params: dict[str, Any] = {"limit": 100}
        models: list[dict[str, Any]] = []
        try:
            with httpx.Client(timeout=timeout) as client:
                while True:
                    response = client.get(url, headers=headers, params=params)
                    if response.status_code >= 400:
                        raise CommandError(
                            f"Anthropic /v1/models returned {response.status_code}: "
                            f"{response.text[:300]}"
                        )
                    payload = response.json()
                    models.extend(payload.get("data") or [])
                    if not payload.get("has_more") or not payload.get("last_id"):
                        return models
                    params["after_id"] = payload["last_id"]
        except httpx.HTTPError as exc:
            raise CommandError(f"Transport error talking to Anthropic: {exc}") from exc
