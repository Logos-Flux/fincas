from django.apps import AppConfig


class CrmConfig(AppConfig):
    name = "crm"

    def ready(self):
        # Validate GOLDEN_LOSS_REASONS, GOLDEN_ENGAGEMENT_SOURCES,
        # GOLDEN_TASK_KINDS and GOLDEN_DEFAULT_PIPELINES at boot: bad config
        # fails `manage.py check` instead of surfacing at the first write.
        # Phase 3 1.2: a new Company starts with its default pipeline(s).
        from django.db.models.signals import post_save

        from core.models import Company

        from . import registries  # noqa: F401
        from .pipelines import company_created

        post_save.connect(company_created, sender=Company, dispatch_uid="crm.seed_default_pipelines")
