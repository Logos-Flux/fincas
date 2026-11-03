"""The staff-only LLM Settings page at /erp/llm/.

Job, in one sentence (it is also the page's first line): observe and assign
LLM routing and rotate the ERP's caller token — swapping which model a lane
serves happens in the gateway's own admin UI, not here.

Every POST handler is a plain Django view (CSRF-protected, staff-only). Each
mutation locks the settings singleton, saves inside ``audit.view_write()`` so
the signal backstop stays quiet, and records a POST-shaped audit row that is
written only if the transaction commits.

The page never depends on gateway liveness: every gateway read degrades to the
last-known values with a banner, and per-lane tests are separate short POSTs
driven from the browser, never one blocking multi-lane probe.
"""

from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import async_to_sync
from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db import transaction
from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from . import audit, client, conf, crypto
from .erp_forms import ConnectionForm, LLMModelForm
from .models import LLMModel, LLMSettings, LLMSettingsAudit
from .purposes import load_external_purposes, load_purposes

logger = logging.getLogger(__name__)

AUDIT_ROWS = 20
RUNBOOK_403 = (
    "The gateway is not exposing /v1/models to this caller — its edge only allows specific "
    "paths per caller token. Allow /v1/models for this caller at the edge (see the gateway "
    "topology runbook). Lane assignments below are from the database."
)


class StaffOnlyMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Anonymous → login redirect; signed-in non-staff → 403."""

    login_url = "/erp/login/"
    raise_exception = False

    def test_func(self):
        return self.request.user.is_staff

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            self.raise_exception = True
        return super().handle_no_permission()


# ---- page ------------------------------------------------------------------


def _env_base_url() -> str:
    return (getattr(settings, "LLMHUB_GATEWAY_BASE_URL", "") or "").strip()


def _env_key_available(row: LLMSettings) -> bool:
    return bool(row.fallback_to_env and (getattr(settings, "LLMHUB_GATEWAY_API_KEY", "") or "").strip())


def _note_undecryptable(snap: conf.Snapshot, user) -> None:
    """Write one audit row per undecryptable-key episode (not one per page view)."""
    if not snap.key_undecryptable:
        return
    latest = (
        LLMSettingsAudit.objects.filter(action__in=[LLMSettingsAudit.KEY_UNDECRYPTABLE, LLMSettingsAudit.KEY_ROTATED])
        .values_list("action", flat=True)
        .first()
    )
    if latest != LLMSettingsAudit.KEY_UNDECRYPTABLE:
        LLMSettingsAudit.objects.create(
            actor=user if user.is_authenticated else None,
            action=LLMSettingsAudit.KEY_UNDECRYPTABLE,
            field="gateway_api_key_enc",
            detail="Stored token could not be decrypted (Fernet key changed); treated as unset.",
        )


def _session_counts() -> dict[str, int]:
    """model key → number of chat sessions that stored it (for delete warnings)."""
    if not apps.is_installed("chat"):
        return {}
    from django.db.models import Count

    ChatSession = apps.get_model("chat", "ChatSession")
    return dict(ChatSession.objects.values_list("model_id").annotate(n=Count("id")).values_list("model_id", "n"))


def _route_vocabulary(snap: conf.Snapshot, models_list: client.ModelsList | None) -> list[str] | None:
    """What a route may be in the active mode, or None when it is unknown."""
    if snap.gateway:
        return list(models_list.ids) if models_list and models_list.ids else None
    return None  # direct mode: concrete provider ids; no authoritative list to check against


def build_context(request, *, connection_form=None, model_form=None, edit_forms=None) -> dict[str, Any]:
    snap = conf.snapshot(fresh=True)  # the admin view always reads uncached
    row = LLMSettings.objects.filter(pk=1).first() or LLMSettings(pk=1)
    _note_undecryptable(snap, request.user)

    models_list = client.available_models(timeout=4.0, snap=snap) if snap.gateway else None
    vocabulary = _route_vocabulary(snap, models_list)
    observed = client.observations()
    last_test = row.last_test or {}

    purposes = []
    for name, spec in load_purposes().items():
        route = conf.route_for(name, snap)
        purposes.append(
            {
                "name": name,
                "spec": spec,
                "route": route,
                "assigned": (row.lane_map or {}).get(name, ""),
                "missing_on_gateway": bool(
                    snap.gateway and route.value and vocabulary is not None and route.value not in vocabulary
                ),
                "observed": observed.get(route.value),
                "test": last_test.get(name),
            }
        )

    tests = [t for t in last_test.values() if t.get("at")]
    latest_test = max(tests, key=lambda t: t["at"]) if tests else None
    chat_rows = list(LLMModel.objects.order_by("sort", "id"))
    counts = _session_counts()
    for m in chat_rows:
        m.session_count = counts.get(m.key, 0)
        m.observed = observed.get(m.route)
        m.missing_on_gateway = bool(snap.gateway and vocabulary is not None and m.route not in vocabulary)
        m.edit_errors = (edit_forms or {}).get(m.pk).errors if m.pk in (edit_forms or {}) else {}

    audit_qs = LLMSettingsAudit.objects.select_related("actor")
    return {
        "current_section": "llm",
        "page_title": "LLM Settings",
        "snap": snap,
        "row": row,
        "is_gateway": snap.gateway,
        "key_fingerprint": conf.fingerprint(snap.api_key.value),
        "key_source": snap.api_key.source,
        "key_derived": crypto.key_is_derived(),
        "has_db_key": bool(row.gateway_api_key_enc),
        "env_base_url": _env_base_url(),
        "allowed_base_urls": [u for u in (getattr(settings, "LLMHUB_ALLOWED_BASE_URLS", []) or [])],
        "not_configured": (not snap.saved and not snap.api_key and not snap.gateway),
        "models_list": models_list,
        "models_403": bool(models_list and models_list.error_category == "edge_forbidden"),
        "runbook_403": RUNBOOK_403,
        "vocabulary": vocabulary,
        "purposes": purposes,
        "external_purposes": load_external_purposes(),
        "latest_test": latest_test,
        "chat_rows": chat_rows,
        "lane_map_count": len(row.lane_map or {}),
        "audit_rows": list(audit_qs[:AUDIT_ROWS]),
        "audit_total": audit_qs.count(),
        "test_timeout_s": conf.test_timeout(),
        "connection_form": connection_form
        or ConnectionForm(
            initial={"mode": snap.mode, "gateway_base_url": row.gateway_base_url},
            env_base_url=_env_base_url(),
            env_key_available=_env_key_available(row),
            has_db_key=bool(row.gateway_api_key_enc),
            current_base_url=row.gateway_base_url,
        ),
        "model_form": model_form or LLMModelForm(prefix="new", initial={"enabled": True}),
        "edit_forms": edit_forms or {},
    }


class LLMSettingsView(StaffOnlyMixin, View):
    template_name = "llmhub/settings.html"

    def get(self, request):
        return render(request, self.template_name, build_context(request))


# ---- connection --------------------------------------------------------------


class ConnectionUpdateView(StaffOnlyMixin, View):
    def post(self, request):
        current = LLMSettings.objects.filter(pk=1).first() or LLMSettings(pk=1)
        form = ConnectionForm(
            request.POST,
            env_base_url=_env_base_url(),
            env_key_available=_env_key_available(current),
            has_db_key=bool(current.gateway_api_key_enc),
            current_base_url=current.gateway_base_url,
        )
        if not form.is_valid():
            return render(request, LLMSettingsView.template_name, build_context(request, connection_form=form), status=400)

        data = form.cleaned_data
        changed = []
        with transaction.atomic(), audit.view_write():
            row = LLMSettings.load(for_update=True)
            old_mode = conf.resolve_mode(row, True)[0]
            # Pin the mode only when the admin changes it: saving the form to
            # rotate a token must not freeze whatever LLMHUB_MODE says today.
            if data["mode"] != old_mode:
                detail = ""
                if row.lane_map:
                    detail = (
                        f"{len(row.lane_map)} lane assignment(s) were made as {old_mode} routes and "
                        f"will not resolve in {data['mode']} mode until reassigned (kept, not deleted)."
                    )
                audit.record(
                    LLMSettingsAudit.MODE_CHANGED, actor=request.user, field="mode",
                    old=old_mode, new=data["mode"], detail=detail,
                )
                row.mode = data["mode"]
                changed.append("mode")
            new_base = (data.get("gateway_base_url") or "").strip().rstrip("/")
            if new_base != row.gateway_base_url:
                audit.record(
                    LLMSettingsAudit.BASE_URL_CHANGED, actor=request.user, field="gateway_base_url",
                    old=row.gateway_base_url, new=new_base,
                )
                row.gateway_base_url = new_base
                changed.append("base URL")
            if data.get("new_api_key"):
                old_fp = self._old_fingerprint(row)
                row.gateway_api_key_enc = crypto.encrypt(data["new_api_key"])
                audit.record(
                    LLMSettingsAudit.KEY_ROTATED, actor=request.user, field="gateway_api_key",
                    old_masked=old_fp or "(empty)", new_masked=conf.fingerprint(data["new_api_key"]),
                    detail="caller token replaced from the settings page",
                )
                changed.append("caller token")
            elif data.get("clear_api_key") and row.gateway_api_key_enc:
                audit.record(
                    LLMSettingsAudit.KEY_CLEARED, actor=request.user, field="gateway_api_key",
                    old_masked=self._old_fingerprint(row) or "(set)", new_masked="(empty)",
                )
                row.gateway_api_key_enc = ""
                changed.append("caller token cleared")
            row.full_clean()
            row.save()
        conf.invalidate()
        if changed:
            messages.success(request, "Saved: " + ", ".join(changed) + ".")
        else:
            messages.info(request, "No changes.")
        return redirect("erp-llm-settings")

    @staticmethod
    def _old_fingerprint(row: LLMSettings) -> str:
        try:
            return conf.fingerprint(crypto.decrypt(row.gateway_api_key_enc))
        except crypto.InvalidToken:
            return "(undecryptable)"


# ---- lane assignment -----------------------------------------------------------


class LaneUpdateView(StaffOnlyMixin, View):
    def post(self, request):
        snap = conf.snapshot(fresh=True)
        purposes = load_purposes()
        vocabulary = None
        if snap.gateway:
            listed = client.available_models(timeout=4.0, snap=snap)
            vocabulary = listed.ids or None
        errors = []
        updates: dict[str, str] = {}
        for name in purposes:
            field = f"lane_{name}"
            if field not in request.POST:
                continue
            value = request.POST[field].strip()
            if value and vocabulary is not None and value not in vocabulary:
                errors.append(f"{name}: {value!r} is not offered to this key by the gateway.")
                continue
            updates[name] = value
        if errors:
            for e in errors:
                messages.error(request, e)
            return redirect("erp-llm-settings")

        with transaction.atomic(), audit.view_write():
            row = LLMSettings.load(for_update=True)
            lane_map = dict(row.lane_map or {})
            for name, value in updates.items():
                old = lane_map.get(name, "")
                if old == value:
                    continue
                if value:
                    lane_map[name] = value
                else:
                    lane_map.pop(name, None)
                audit.record(
                    LLMSettingsAudit.LANE_CHANGED, actor=request.user, field=f"lane_map.{name}",
                    old=old or "(default)", new=value or "(default)",
                )
            row.lane_map = lane_map
            row.full_clean()
            row.save()
        conf.invalidate()
        messages.success(request, "Lane assignments saved.")
        return redirect("erp-llm-settings")


# ---- chat model list --------------------------------------------------------------


def _vocabulary_for_forms() -> list[str] | None:
    snap = conf.snapshot(fresh=True)
    if not snap.gateway:
        return None
    listed = client.available_models(timeout=4.0, snap=snap)
    return listed.ids or None


class ModelCreateView(StaffOnlyMixin, View):
    def post(self, request):
        form = LLMModelForm(request.POST, prefix="new", route_choices=_vocabulary_for_forms())
        if not form.is_valid():
            return render(request, LLMSettingsView.template_name, build_context(request, model_form=form), status=400)
        with transaction.atomic(), audit.view_write():
            obj = form.save(commit=False)
            if not LLMModel.objects.exists():
                obj.is_default = True  # the first row is the default; an unset default reads as a bug
            obj.sort = (LLMModel.objects.aggregate(m=Max("sort"))["m"] or 0) + 1
            obj.full_clean()
            obj.save()
            audit.record(LLMSettingsAudit.MODEL_ADDED, actor=request.user, field=f"LLMModel[{obj.key}]", new=obj.route)
        messages.success(request, f"Added chat model {obj.label}.")
        return redirect("erp-llm-settings")


class ModelUpdateView(StaffOnlyMixin, View):
    def post(self, request, pk):
        obj = get_object_or_404(LLMModel, pk=pk)
        before = {f: getattr(obj, f) for f in ("label", "route", "enabled", "cost_hint", "sort", "max_tokens")}
        form = LLMModelForm(request.POST, instance=obj, prefix=f"m{pk}", route_choices=_vocabulary_for_forms())
        if not form.is_valid():
            return render(
                request, LLMSettingsView.template_name, build_context(request, edit_forms={pk: form}), status=400
            )
        with transaction.atomic(), audit.view_write():
            if obj.is_default and not form.cleaned_data["enabled"]:
                messages.error(request, "Make another model the default before disabling this one.")
                return redirect("erp-llm-settings")
            obj = form.save()
            for field, old in before.items():
                new = getattr(obj, field)
                if new != old:
                    action = LLMSettingsAudit.MODEL_TOGGLED if field == "enabled" else LLMSettingsAudit.MODEL_CHANGED
                    audit.record(action, actor=request.user, field=f"LLMModel[{obj.key}].{field}", old=old, new=new)
        messages.success(request, f"Saved {obj.label}.")
        return redirect("erp-llm-settings")


class ModelActionView(StaffOnlyMixin, View):
    """toggle / default / up / down / delete — one small POST each."""

    def post(self, request, pk, action):
        obj = get_object_or_404(LLMModel, pk=pk)
        with transaction.atomic(), audit.view_write():
            if action == "toggle":
                if obj.is_default and obj.enabled:
                    messages.error(request, "Make another model the default before disabling this one.")
                    return redirect("erp-llm-settings")
                obj.enabled = not obj.enabled
                obj.save(update_fields=["enabled", "updated_at"])
                audit.record(
                    LLMSettingsAudit.MODEL_TOGGLED, actor=request.user, field=f"LLMModel[{obj.key}].enabled",
                    old=not obj.enabled, new=obj.enabled,
                )
            elif action == "default":
                previous = list(LLMModel.objects.select_for_update().filter(is_default=True).exclude(pk=obj.pk))
                # One UPDATE demotes every other default: the chat picker would
                # otherwise resolve duplicates to iteration order, silently.
                LLMModel.objects.filter(is_default=True).exclude(pk=obj.pk).update(is_default=False)
                obj.is_default = True
                obj.enabled = True
                obj.save(update_fields=["is_default", "enabled", "updated_at"])
                audit.record(
                    LLMSettingsAudit.MODEL_DEFAULT_CHANGED, actor=request.user, field="LLMModel.is_default",
                    old=", ".join(p.key for p in previous) or "(none)", new=obj.key,
                )
            elif action in ("up", "down"):
                rows = list(LLMModel.objects.select_for_update().order_by("sort", "id"))
                i = next(i for i, r in enumerate(rows) if r.pk == obj.pk)
                j = i - 1 if action == "up" else i + 1
                if 0 <= j < len(rows):
                    rows[i], rows[j] = rows[j], rows[i]
                    for n, r in enumerate(rows, start=1):
                        if r.sort != n:
                            r.sort = n
                            r.save(update_fields=["sort", "updated_at"])
                    audit.record(
                        LLMSettingsAudit.MODEL_REORDERED, actor=request.user, field="LLMModel.sort",
                        new=" > ".join(r.key for r in rows),
                    )
            elif action == "delete":
                if obj.is_default:
                    messages.error(request, "Make another model the default before deleting this one.")
                    return redirect("erp-llm-settings")
                sessions = _session_counts().get(obj.key, 0)
                detail = f"{sessions} chat session(s) still reference this key; they fall back to the default." if sessions else ""
                audit.record(
                    LLMSettingsAudit.MODEL_DELETED, actor=request.user, field=f"LLMModel[{obj.key}]",
                    old=obj.route, detail=detail,
                )
                obj.delete()
            else:
                return JsonResponse({"error": "unknown action"}, status=400)
        conf.invalidate()
        return redirect("erp-llm-settings")


# ---- live checks (JSON, driven per lane from the browser) ------------------------


class PurposeTestView(StaffOnlyMixin, View):
    """POST /erp/llm/test/<purpose>/ → one live call, classified. Hard-capped by LLMHUB_TEST_TIMEOUT_S."""

    def post(self, request, purpose):
        if purpose not in load_purposes():
            return JsonResponse({"error": f"unknown purpose {purpose!r}"}, status=404)
        result = async_to_sync(client.test_purpose)(purpose)
        client.save_test_result(result)
        return JsonResponse(result.as_dict())


class ModelsRefreshView(StaffOnlyMixin, View):
    def post(self, request):
        listed = client.available_models(timeout=5.0)
        return JsonResponse(
            {
                "ids": listed.ids,
                "as_of": listed.as_of.isoformat() if listed.as_of else None,
                "from_cache": listed.from_cache,
                "error_category": listed.error_category,
                "error_message": listed.error_message,
                "runbook": RUNBOOK_403 if listed.error_category == "edge_forbidden" else "",
            }
        )


__all__ = [
    "ConnectionUpdateView",
    "LLMSettingsView",
    "LaneUpdateView",
    "ModelActionView",
    "ModelCreateView",
    "ModelUpdateView",
    "ModelsRefreshView",
    "PurposeTestView",
]
