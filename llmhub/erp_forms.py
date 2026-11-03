"""Forms for the LLM Settings page."""

from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError

from .models import MODE_CHOICES, LLMModel, validate_base_url

CALLER_TOKEN_HELP = (
    "This is the Caddy caller token, never a LiteLLM virtual key. Write-only: paste a new "
    "value to replace the stored one; it is encrypted at rest and never shown again. "
    "Direct mode ignores it (provider keys come from the environment)."
)


class ConnectionForm(forms.Form):
    mode = forms.ChoiceField(choices=MODE_CHOICES, widget=forms.RadioSelect(attrs={"class": "form-selectgroup-input"}))
    gateway_base_url = forms.CharField(
        required=False,
        max_length=300,
        label="Gateway base URL",
        help_text="Everything before /v1/…. Blank = LLMHUB_GATEWAY_BASE_URL from the environment.",
        validators=[validate_base_url],
    )
    new_api_key = forms.CharField(
        required=False,
        max_length=500,
        label="Replace caller token",
        help_text=CALLER_TOKEN_HELP,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
    )
    clear_api_key = forms.BooleanField(required=False, label="Clear the stored token")

    def __init__(
        self,
        *args,
        env_base_url: str = "",
        env_key_available: bool = False,
        has_db_key: bool = False,
        current_base_url: str = "",
        **kwargs,
    ):
        self.env_base_url = env_base_url
        self.current_base_url = (current_base_url or "").rstrip("/")
        self.env_key_available = env_key_available
        self.has_db_key = has_db_key
        super().__init__(*args, **kwargs)
        self.fields["gateway_base_url"].widget.attrs.update(
            {"class": "form-control", "placeholder": env_base_url or "https://gateway.example.com"}
        )
        self.fields["new_api_key"].widget.attrs.update({"class": "form-control", "placeholder": "paste to replace"})
        self.fields["clear_api_key"].widget.attrs.update({"class": "form-check-input"})

    def clean_new_api_key(self):
        value = (self.cleaned_data.get("new_api_key") or "").strip()
        if value and any(c.isspace() for c in value):
            raise ValidationError("A token cannot contain whitespace — check the paste.")
        return value

    def clean(self):
        data = super().clean()
        if data.get("new_api_key") and data.get("clear_api_key"):
            raise ValidationError("Either paste a new token or clear the stored one, not both.")
        new_base = (data.get("gateway_base_url") or "").strip().rstrip("/")
        if (
            new_base
            and new_base != self.current_base_url
            and self.has_db_key
            and not data.get("new_api_key")
            and not data.get("clear_api_key")
        ):
            # The stored token only ever goes where it was pasted for: moving the
            # URL without re-entering it would hand it to a host its owner never chose.
            self.add_error(
                "new_api_key",
                "Changing the base URL requires re-entering the caller token (or clearing it).",
            )
        if data.get("mode") == "gateway":
            base = (data.get("gateway_base_url") or "").strip() or self.env_base_url
            will_have_db_key = bool(data.get("new_api_key")) or (self.has_db_key and not data.get("clear_api_key"))
            if not base:
                self.add_error("gateway_base_url", "Gateway mode needs a base URL (here or LLMHUB_GATEWAY_BASE_URL).")
            if not (will_have_db_key or self.env_key_available):
                self.add_error(
                    "new_api_key",
                    "Gateway mode needs a caller token: paste one, or keep env fallback on with "
                    "LLMHUB_GATEWAY_API_KEY set.",
                )
        return data


class LLMModelForm(forms.ModelForm):
    """The page-editable columns only. Adapter, credentials, warmup etc. stay code-managed."""

    class Meta:
        model = LLMModel
        fields = ["key", "label", "route", "enabled", "cost_hint", "sort", "max_tokens"]

    def __init__(self, *args, route_choices: list[str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.route_choices = route_choices
        if self.instance.pk:
            # A live key is persisted on chat sessions; renaming it strands them.
            self.fields["key"].disabled = True
        else:
            # The page's add form has no sort input — ModelCreateView appends
            # the new row at the end — so requiring it rejected every add.
            self.fields["sort"].required = False
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            else:
                field.widget.attrs.setdefault("class", "form-control form-control-sm")

    def clean_route(self):
        route = (self.cleaned_data.get("route") or "").strip()
        # Only validated against a known vocabulary: while the gateway is
        # unreachable the list is unknown, and blocking every save then would
        # turn an outage into a lockout. The page flags drift inline instead.
        if self.route_choices is not None and route not in self.route_choices:
            raise ValidationError(
                f"{route!r} is not offered to this key (known routes: {', '.join(self.route_choices) or 'none'})."
            )
        return route
