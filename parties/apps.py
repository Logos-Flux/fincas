from django.apps import AppConfig


class PartiesConfig(AppConfig):
    name = "parties"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Validate GOLDEN_PARTY_ROLES, GOLDEN_RELATIONSHIP_KINDS,
        # GOLDEN_LIFECYCLE_STAGES, GOLDEN_CONTACT_TYPES and
        # GOLDEN_DEFAULT_PHONE_REGION at boot: bad config fails `manage.py
        # check` instead of surfacing at the first write.
        from . import (
            checks,  # noqa: F401  (parties.E001: the portal tenant)
            classification_registry,  # noqa: F401
            relationship_registry,  # noqa: F401
            role_registry,  # noqa: F401
        )
        from .normalize import default_phone_region

        default_phone_region()

        # ExternalRef targets are generic (spec 1.7): delete a hard-deleted
        # party's refs. Merge and erasure keep the row, so they keep the refs.
        from core.provenance import track

        from .models import Party

        track(Party)

        # Step 5 dual-writes Customer / CustomerAddress legacy ids as refs.
        from customers.models import Customer, CustomerAddress

        track(Customer)
        track(CustomerAddress)

        from django.db.models.signals import post_delete, post_save

        from core.models import Company

        from . import receivers

        post_save.connect(receivers.company_saved, sender=Company, dispatch_uid="parties.self_party_rename")
        post_delete.connect(
            receivers.customer_address_deleted, sender=CustomerAddress,
            dispatch_uid="parties.customer_address_deleted",
        )

        # Step 6: the merge queue applies through unify's applier registry.
        from .merge_queue import register

        register()
