from django.apps import AppConfig


class WebhooksConfig(AppConfig):
    name = "webhooks"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import outbox
        from .events import versioning_cutoff

        outbox.register()  # the ``webhooks`` and ``webhooks.deliver`` outbox consumers

        versioning_cutoff()  # a malformed WEBHOOKS_VERSIONING_CUTOFF fails startup, not every delivery
