"""llmhub persistence: one settings row, the chat model list, and an audit trail.

``LLMSettings`` is a singleton (pk is always 1). No row is created until an
admin saves the settings page: until then every value resolves from env and
built-in defaults, exactly as a deployment without llmhub behaved (see
``conf.current``).

The one secret stored here — the gateway caller token — is Fernet-encrypted
(``crypto.py``) and never rendered back. What an admin sees is a source-
labelled fingerprint of the *resolved* key (last 4 characters).
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

MODE_DIRECT = "direct"
MODE_GATEWAY = "gateway"
MODE_CHOICES = [
    (MODE_DIRECT, "Direct — call providers with their own keys"),
    (MODE_GATEWAY, "Gateway — call logical lanes on an OpenAI-compatible gateway"),
]


def allowed_base_urls() -> list[str]:
    """Deploy-time allowlist for the DB-stored gateway base URL.

    Empty means unlocked (the golden default: a template cannot know its
    tenants' hosts). A deployment that sets it closes the redirect vector —
    a staff session repointing LLM traffic at a host it controls — because
    the list itself can only change with an env edit and a deploy.
    """
    configured = getattr(settings, "LLMHUB_ALLOWED_BASE_URLS", []) or []
    return [u.strip().rstrip("/") for u in configured if u and u.strip()]


def _is_local_host(host: str) -> bool:
    import ipaddress

    if host in ("localhost",) or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def is_allowed_base_url(value: str) -> bool:
    allowed = allowed_base_urls()
    return not allowed or value.rstrip("/") in allowed


def validate_base_url(value: str) -> None:
    from urllib.parse import urlsplit

    if not value:
        return
    parts = urlsplit(value)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValidationError("Base URL must be an https:// URL (http:// only for a local/private gateway).")
    if parts.username or parts.password:
        raise ValidationError("Base URL must not carry credentials — the caller token goes in its own field.")
    if parts.scheme == "http" and not _is_local_host(parts.hostname):
        raise ValidationError("Plain http:// would send the caller token unencrypted; use https:// for a remote gateway.")
    allowed = allowed_base_urls()
    if allowed and value.rstrip("/") not in allowed:
        raise ValidationError(
            "This base URL is not in LLMHUB_ALLOWED_BASE_URLS (%(allowed)s). The allowlist is "
            "deploy-time config on purpose: it is what stops a settings edit from sending prompts "
            "to an arbitrary host.",
            params={"allowed": ", ".join(allowed)},
        )


class LLMSettings(models.Model):
    """The singleton LLM configuration row. Always pk=1."""

    # Blank = follow LLMHUB_MODE from the environment. Only an explicit save of
    # the mode selector pins it here, so creating the row for any other reason
    # (a test result, a lane edit) never silently freezes the env's choice.
    mode = models.CharField(max_length=10, choices=MODE_CHOICES, blank=True, default="")
    gateway_base_url = models.CharField(
        max_length=300,
        blank=True,
        validators=[validate_base_url],
        help_text="Everything before /v1/…. Blank = LLMHUB_GATEWAY_BASE_URL from the environment.",
    )
    # Fernet ciphertext of the gateway *caller* token. Never rendered.
    gateway_api_key_enc = models.TextField(blank=True, editable=False)
    # purpose name → route (a lane in gateway mode, a model id in direct mode).
    lane_map = models.JSONField(default=dict, blank=True)
    # When the DB holds no usable key, use the environment's. Ops-only toggle
    # (Django admin / shell); every change is audited by the signal backstop.
    fallback_to_env = models.BooleanField(default=True)

    # Last per-purpose test results, for the status header:
    # {purpose: {"status", "category", "message", "resolved_model", "elapsed_ms", "at"}}.
    last_test = models.JSONField(default=dict, blank=True, editable=False)
    # Last successful GET /v1/models, so the page renders when the gateway is down.
    models_cache = models.JSONField(default=list, blank=True, editable=False)
    models_cache_at = models.DateTimeField(null=True, blank=True, editable=False)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "LLM settings"
        verbose_name_plural = "LLM settings"

    def __str__(self) -> str:
        return f"LLM settings ({self.mode or 'env mode'})"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # pragma: no cover — guarded, never called
        raise ValidationError("LLMSettings is a singleton and cannot be deleted.")

    @classmethod
    def load(cls, *, for_update: bool = False) -> "LLMSettings":
        """The saved row, or an unsaved instance carrying env/built-in defaults.

        ``for_update=True`` must be called inside ``transaction.atomic()``; it
        locks the row so two concurrent edits of ``lane_map`` (one JSON blob)
        cannot lose one another's change.
        """
        if not for_update:
            return cls.objects.filter(pk=1).first() or cls(pk=1)
        # get_or_create absorbs the IntegrityError when two first writes race
        # (e.g. "Test all lanes" on a fresh install fires concurrent POSTs).
        cls.objects.get_or_create(pk=1)
        return cls.objects.select_for_update().get(pk=1)

    def clean(self):
        super().clean()
        from .purposes import load_purposes

        unknown = sorted(set(self.lane_map or {}) - set(load_purposes()))
        if unknown:
            raise ValidationError({"lane_map": f"Unknown purpose(s): {', '.join(unknown)}."})


class LLMModel(models.Model):
    """One entry in the chat drawer's model picker (gateway mode).

    ``route`` is deliberately mode-neutral: in gateway mode it names a lane,
    in direct mode a concrete model id. The remaining columns mirror the
    code registry's ``ModelConfig`` so an entry never loses the fields that
    make it work (a scale-to-zero endpoint's warmup budget, its credential
    setting names, Anthropic reasoning controls).
    """

    ADAPTER_CHOICES = [("openai_compat", "OpenAI-compatible"), ("anthropic", "Anthropic")]
    TOOLS_CHOICES = [("full", "Full tool catalog"), ("none", "No tools")]

    key = models.SlugField(
        max_length=60,
        unique=True,
        help_text="Stable identifier persisted on chat sessions. Never rename a live key.",
    )
    label = models.CharField(max_length=80)
    route = models.CharField(max_length=120, help_text="Gateway lane (gateway mode) or model id (direct mode).")
    enabled = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    cost_hint = models.CharField(max_length=120, blank=True)
    sort = models.PositiveIntegerField(default=0)
    max_tokens = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Output ceiling. Reasoning models bill hidden thinking against it — leave headroom.",
    )

    # Code-managed columns (not editable on the settings page).
    adapter = models.CharField(max_length=20, choices=ADAPTER_CHOICES, default="openai_compat")
    model_id = models.CharField(max_length=120, blank=True, help_text="Direct mode only; blank = use route.")
    use_streaming = models.BooleanField(default=True)
    tools_mode = models.CharField(max_length=10, choices=TOOLS_CHOICES, default="full")
    warmup_max_wait_s = models.FloatField(default=0.0)
    base_url_setting = models.CharField(max_length=80, blank=True)
    api_key_setting = models.CharField(max_length=80, blank=True)
    effort = models.CharField(max_length=10, null=True, blank=True)
    thinking = models.CharField(max_length=10, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort", "id"]
        verbose_name = "LLM chat model"

    def __str__(self) -> str:
        return f"{self.label} ({self.route})"

    def clean(self):
        super().clean()
        # A named credential setting that resolves to nothing is an admin
        # typo; catch it here instead of as an opaque upstream 401 later.
        errors = {}
        for field in ("base_url_setting", "api_key_setting"):
            name = getattr(self, field)
            if name and not (getattr(settings, name, "") or ""):
                errors[field] = f"settings.{name} is not set (or empty) in this deployment."
        if errors:
            raise ValidationError(errors)


class LLMSettingsAudit(models.Model):
    """Append-only trail of every LLM settings change. Values are masked."""

    KEY_ROTATED = "key_rotated"
    KEY_CLEARED = "key_cleared"
    KEY_UNDECRYPTABLE = "key_undecryptable"
    BASE_URL_CHANGED = "base_url_changed"
    FALLBACK_TOGGLED = "fallback_toggled"
    LANE_CHANGED = "lane_changed"
    MODE_CHANGED = "mode_changed"
    MODEL_ADDED = "model_added"
    MODEL_CHANGED = "model_changed"
    MODEL_DELETED = "model_deleted"
    MODEL_TOGGLED = "model_toggled"
    MODEL_REORDERED = "model_reordered"
    MODEL_DEFAULT_CHANGED = "model_default_changed"
    FIELD_CHANGED = "field_changed"  # signal backstop: an out-of-band write

    ACTION_CHOICES = [
        (KEY_ROTATED, "Key rotated"),
        (KEY_CLEARED, "Key cleared"),
        (KEY_UNDECRYPTABLE, "Stored key undecryptable"),
        (BASE_URL_CHANGED, "Base URL changed"),
        (FALLBACK_TOGGLED, "Env fallback toggled"),
        (LANE_CHANGED, "Lane changed"),
        (MODE_CHANGED, "Mode changed"),
        (MODEL_ADDED, "Model added"),
        (MODEL_CHANGED, "Model changed"),
        (MODEL_DELETED, "Model deleted"),
        (MODEL_TOGGLED, "Model enabled/disabled"),
        (MODEL_REORDERED, "Models reordered"),
        (MODEL_DEFAULT_CHANGED, "Default model changed"),
        (FIELD_CHANGED, "Field changed (outside the settings page)"),
    ]

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Null for shell / fixture / migration writes.",
    )
    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    field = models.CharField(max_length=60, blank=True)
    old_masked = models.CharField(max_length=200, blank=True)
    new_masked = models.CharField(max_length=200, blank=True)
    detail = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "LLM settings audit entry"
        verbose_name_plural = "LLM settings audit"

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.field}"


class LaneObservation(models.Model):
    """The concrete model a route was last observed serving.

    A gateway lane is a logical name; the model behind it is whatever the
    lane points at right now, fallbacks included. The ``x-litellm-model-name``
    response header is the only place that is visible, so each real call
    records it here. DB-backed rather than a process-local cache: with several
    workers on several machines, a per-process cache would show a stale model
    right after a lane re-point — the exact moment an admin looks.
    """

    route = models.CharField(max_length=120, unique=True)
    model = models.CharField(max_length=200)
    observed_at = models.DateTimeField()

    class Meta:
        verbose_name = "LLM lane observation"

    def __str__(self) -> str:
        return f"{self.route} → {self.model}"
