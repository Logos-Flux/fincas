"""Headless smoke check: does every purpose actually answer?

    python manage.py llmhub_check            # config + one live call per purpose
    python manage.py llmhub_check --no-live  # config only (no network, no spend)

Expected output on a healthy gateway deployment::

    mode: gateway (from db)   base URL: https://gw.example.com (db)   key: …a1b2 (db)
      ✓ chat_default   erp-assistant        OK 812ms · served by model-x · tool call OK
      ✓ research       erp-research         OK 1204ms · served by model-y
    all purposes OK

Exit status: 0 when every purpose answered, 1 otherwise — usable as a CI or
post-deploy smoke step. Each live check is one small paid completion.
"""

from __future__ import annotations

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand, CommandError

from llmhub import client, conf
from llmhub.purposes import load_purposes


class Command(BaseCommand):
    help = "Smoke-test llmhub: resolution + one live call per purpose (exit 1 on any failure)."

    def add_arguments(self, parser):
        parser.add_argument("--no-live", action="store_true", help="Resolve config only; make no calls.")
        parser.add_argument("--purpose", action="append", help="Limit to these purposes (repeatable).")

    def handle(self, *args, **options):
        snap = conf.snapshot(fresh=True)
        fp = conf.fingerprint(snap.api_key.value) or "(none)"
        self.stdout.write(
            f"mode: {snap.mode} (from {snap.mode_source})   "
            f"base URL: {snap.base_url.value or '(none)'} ({snap.base_url.source})   "
            f"key: {fp} ({snap.api_key.source})"
        )
        if snap.key_undecryptable:
            self.stdout.write(self.style.ERROR("  stored key is undecryptable — re-enter it on /erp/llm/"))
        if snap.gateway and snap.api_key.source == conf.SOURCE_ENV:
            self.stdout.write(self.style.WARNING("  key supplied by env fallback — scheduled for retirement"))

        purposes = load_purposes()
        wanted = options.get("purpose") or list(purposes)
        unknown = [p for p in wanted if p not in purposes]
        if unknown:
            raise CommandError(f"Unknown purpose(s): {', '.join(unknown)}")

        failures = 0
        for name in wanted:
            spec = purposes[name]
            if not snap.gateway and spec.direct_handler:
                self.stdout.write(f"  – {name:<14} direct mode: handled by {spec.direct_handler}")
                continue
            route_value, route_note, configured = self._resolve(name, snap)
            head = f"  {name:<14} {route_value or '(no route)':<22}"
            if options["no_live"]:
                failures += 0 if configured else 1
                style = self.style.SUCCESS if configured else self.style.ERROR
                self.stdout.write(style(("✓" if configured else "✗") + head[1:] + f" {route_note}"))
                continue
            result = async_to_sync(client.test_purpose)(name, snap=snap)
            head = f"  {name:<14} {result.route or route_value or '(no route)':<22}"
            if result.status == "ok":
                extra = f" · served by {result.resolved_model}" if result.resolved_model else ""
                extra += " · tool call OK" if result.tool_call == "ok" else ""
                self.stdout.write(self.style.SUCCESS("✓" + head[1:] + f" OK {result.elapsed_ms}ms{extra}"))
            else:
                failures += 1
                self.stdout.write(
                    self.style.ERROR("✗" + head[1:] + f" {result.category}: {result.message} (key from {result.key_source})")
                )

        if failures:
            raise CommandError(f"{failures} purpose(s) failed.", returncode=1)
        self.stdout.write(self.style.SUCCESS("all purposes OK"))

    @staticmethod
    def _resolve(name: str, snap) -> tuple[str, str, bool]:
        """(route, how it resolved, whether anything callable is configured) — no network."""
        from django.apps import apps
        from django.conf import settings

        if name == "chat_default" and apps.is_installed("chat"):
            # The chat drawer's own path: the registry's default entry.
            from chat.llm.registry import default_model

            model = default_model()
            if getattr(model, "credentials", "settings") == "llmhub":
                ok = bool(model.model_id and snap.api_key and snap.base_url)
                return model.model_id, "lane via llmhub (chat registry default)", ok
            key_setting = "ANTHROPIC_API_KEY" if model.adapter == "anthropic" else model.api_key_setting
            ok = bool(getattr(settings, key_setting, ""))
            return model.model_id, f"chat registry default (key: {key_setting}{'' if ok else ' — EMPTY'})", ok
        route = conf.route_for(name, snap)
        ok = bool(route) and bool(snap.api_key) and bool(snap.base_url)
        return route.value, f"route from {route.source}", ok
