from django.apps import AppConfig


class CrmImportConfig(AppConfig):
    name = "crm_import"
    verbose_name = "CRM import"

    def ready(self):
        # The appliers (spec 1.5): crm_<type> in unify's registry.
        from .appliers import register

        register()
