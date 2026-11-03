from django.apps import AppConfig


class LlmhubConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "llmhub"
    verbose_name = "LLM settings"

    def ready(self) -> None:
        from . import checks, signals  # noqa: F401 — registers checks + receivers
        from .purposes import load_purposes

        # Validate the purposes registry at startup, like ERP_NAV and
        # CHAT_EXTRA_MODELS: a bad entry must stop the boot, not vanish.
        load_purposes()
