"""Template context processors for the chat app.

Injects the chat model registry into every template that includes the
drawer partial, so the drawer's <select> can render server-side without
an extra round trip on page load.
"""

from __future__ import annotations

from .llm.registry import MODELS, gateway_enabled


def chat_models(request):
    """Adds `chat_models` (registry order) and the gateway-mode state.

    In gateway mode every entry is a lane; `chat_observed` maps each entry's
    key to the concrete model its lane was last observed serving (and when),
    so the drawer can say what is actually answering before the first reply.
    """
    gateway = gateway_enabled()  # also refreshes MODELS in place
    observed: dict[str, dict] = {}
    if gateway and getattr(request, "user", None) is not None and request.user.is_authenticated:
        from llmhub.client import observations

        try:
            by_route = observations()
        except Exception:  # noqa: BLE001 — a readout must never break page render
            by_route = {}
        for m in MODELS.values():
            hit = by_route.get(m.model_id)
            if hit:
                observed[m.key] = hit
    default = next((m for m in MODELS.values() if m.default), None)
    served = observed.get(default.key, {}) if default else {}
    return {
        "chat_models": list(MODELS.values()),
        "chat_gateway": gateway,
        "chat_observed": observed,
        "chat_served_model": served.get("model", ""),
        "chat_served_at": served.get("at"),
    }
