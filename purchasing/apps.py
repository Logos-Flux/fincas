from django.apps import AppConfig


class PurchasingConfig(AppConfig):
    name = "purchasing"

    def ready(self):
        import purchasing.signals  # noqa: F401
