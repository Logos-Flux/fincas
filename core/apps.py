from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Validate ERP_INTEGRATIONS and ERP_HOOKS at boot: a malformed service row
        # or an unknown hook name fails `manage.py check` instead of vanishing.
        # Whether a hook's dotted path imports is only known on first use.
        from . import (
            checks,  # noqa: F401  # Role/is_staff system checks (database tag)
            field_registry,  # noqa: F401  # Validate GOLDEN_FIELD_DEFINITIONS at boot
            hook_registry,  # noqa: F401
            integration_registry,  # noqa: F401
            label_registry,  # noqa: F401  # Validate GOLDEN_LABEL_OVERRIDES at boot
            role_registry,  # noqa: F401  # Validate GOLDEN_ROLES at boot
            roles,  # noqa: F401  # is_staff derivation (m2m_changed on User.groups)
            schema,  # noqa: F401  # Register OpenAPI authentication extensions
            signals,  # noqa: F401  # Session-login profile linking (AD-12)
        )
        from .integrations import checks as integration_key_checks  # noqa: F401  # core.E040/E041
        from .integrations import erasure as integration_erasure  # noqa: F401  # account delete guard (step 8)
        from .plugins import plugin_registry
        from .plugins.registry import check_plugin_classification
        from .storefront import ensure_storefront_identity_after_migrate

        # Fail startup on a misclassified plugin entry, a plugin URL
        # collision or a bad/duplicate token scope, rather than at the first
        # request that loads the URLconf or presents a bearer.
        check_plugin_classification()
        plugin_registry.get_urls()
        plugin_registry.get_token_scopes()
        plugin_registry.initialize_apps()

        # ready() must not touch the database: it runs on every process start,
        # including `manage.py check` against a restore that has not been
        # migrated yet. Database state is converged after migrate instead.
        # dispatch_uid keeps a re-run of ready() idempotent.
        from django.db.models.signals import post_migrate

        # Roles are Groups created after migrate (no migration file): see
        # core/roles.py.
        post_migrate.connect(
            roles.ensure_roles_after_migrate, dispatch_uid="core.roles.ensure_roles"
        )
        # The storefront identity: core.0002 creates it on a fresh install;
        # this repairs it on every migrate (see core/storefront.py).
        post_migrate.connect(
            ensure_storefront_identity_after_migrate,
            dispatch_uid="core.storefront.ensure_storefront_identity",
        )
        # The legacy API_KEY account: core.0009 creates it. This stands in for
        # that migration only where core's migrations are disabled (the test
        # database); never on a request (core/service_accounts.py).
        from .service_accounts import ensure_legacy_api_user_without_migrations

        post_migrate.connect(
            ensure_legacy_api_user_without_migrations,
            dispatch_uid="core.service_accounts.ensure_legacy_api_user",
        )
