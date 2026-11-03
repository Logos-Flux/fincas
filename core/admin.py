from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import Group, User

from .models import ApiKey, AuditLog, Company, ExternalRef, Team, UserIdentity, UserProfile


class GoldenUserAdmin(DjangoUserAdmin):
    """Stock UserAdmin with ``is_staff`` read-only: it is derived from roles.

    Grant back-office access by adding the user to a staff role's group
    (``staff``, or a deployment role with ``staff=True``); core.roles then
    sets ``is_staff``. Superuser stays an editable, independent flag.
    """

    readonly_fields = (*DjangoUserAdmin.readonly_fields, "is_staff")

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        field = form.base_fields.get("groups")
        if field is not None:
            field.help_text = (
                "Roles are groups (GOLDEN_ROLES). Membership of a staff role "
                "(e.g. 'staff') grants back-office access and sets "
                "'Staff status' automatically. " + str(field.help_text or "")
            )
        return form


if admin.site.is_registered(User):
    admin.site.unregister(User)
admin.site.register(User, GoldenUserAdmin)


class GoldenGroupAdmin(DjangoGroupAdmin):
    """Built-in role groups cannot be deleted or renamed from the admin."""

    def _is_builtin(self, obj):
        from .role_registry import BUILTIN_ROLE_NAMES

        return obj is not None and obj.name in BUILTIN_ROLE_NAMES

    def has_delete_permission(self, request, obj=None):
        return not self._is_builtin(obj) and super().has_delete_permission(request, obj)

    def get_readonly_fields(self, request, obj=None):
        readonly = tuple(super().get_readonly_fields(request, obj))
        return (*readonly, "name") if self._is_builtin(obj) else readonly


if admin.site.is_registered(Group):
    admin.site.unregister(Group)
admin.site.register(Group, GoldenGroupAdmin)


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    """One instance per company (core.E070): once a Company exists the admin
    offers no "Add", and the add view answers 403 with the reason."""

    list_display = ["name", "phone", "website_url", "created_at"]
    search_fields = ["name", "erpnext_name"]
    readonly_fields = ["id", "created_at", "updated_at"]

    @staticmethod
    def _existing():
        return list(Company.objects.order_by("name", "pk").values_list("pk", "name"))

    def has_add_permission(self, request):
        return super().has_add_permission(request) and not Company.objects.exists()

    def add_view(self, request, form_url="", extra_context=None):
        existing = self._existing()
        if existing:
            from django.core.exceptions import PermissionDenied

            from .checks import one_company_message

            raise PermissionDenied(one_company_message(existing))
        return super().add_view(request, form_url, extra_context)

    def changelist_view(self, request, extra_context=None):
        existing = self._existing()
        if existing:
            from django.contrib import messages

            from .checks import one_company_message

            messages.info(request, one_company_message(existing))
        return super().changelist_view(request, extra_context)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ["display_name", "user", "company"]
    list_filter = ["company"]
    search_fields = ["display_name", "user__username", "legacy_external_id"]
    readonly_fields = ["id", "legacy_external_id", "created_at", "updated_at"]


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["action", "model_name", "object_id", "user", "created_at"]
    list_filter = ["action", "model_name"]
    search_fields = ["model_name", "object_id"]
    readonly_fields = [
        "id", "user", "action", "model_name", "object_id",
        "changes", "ip_address", "created_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ExternalRef)
class ExternalRefAdmin(admin.ModelAdmin):
    """Read-only: refs are written by importers and the party backfill."""

    list_display = ["source", "object_type", "source_id", "content_type", "object_id",
                    "company", "last_synced_at"]
    list_filter = ["source", "object_type", "content_type"]
    search_fields = ["source_id", "object_id"]
    list_select_related = ["content_type", "company"]

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(UserIdentity)
class UserIdentityAdmin(admin.ModelAdmin):
    """Read-only list of external logins (identity-unification §2.2).

    Links change only through the portal resolver and invite views (step 4)
    or an explicit admin action added later, never by editing rows here.
    """

    list_display = ["user", "provider", "subject", "email", "email_verified",
                    "last_login_at", "created_at"]
    list_filter = ["provider", "email_verified"]
    search_fields = ["subject", "email", "user__username", "user__email"]
    list_select_related = ["user"]
    readonly_fields = ["id", "user", "provider", "subject", "email", "email_verified",
                       "created_at", "last_login_at", "metadata"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    """Per-integration API keys (identity-unification §2.6). Read-only.

    Keys are created by ``manage.py create_api_key`` (the secret is shown once
    there) and revoked here or by ``manage.py revoke_api_key``. The hash is
    never displayed.
    """

    list_display = ["name", "prefix", "user", "scope", "created_at", "expires_at",
                    "revoked_at", "last_used_at"]
    list_filter = ["scope"]
    search_fields = ["name", "prefix", "user__username"]
    list_select_related = ["user"]
    exclude = ["key_hash"]
    actions = ["revoke_selected"]

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields if f.name != "key_hash"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Revoke selected keys (and their bearer tokens)",
                  permissions=["change"])
    def revoke_selected(self, request, queryset):
        from .service_accounts import revoke

        count = revoke(queryset)
        self.message_user(request, f"Revoked {count} key(s).")


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    """Owner teams (phase4-fields.md 2.1). ``key`` is immutable once saved."""

    list_display = ["name", "key", "company", "is_active", "sort_order"]
    list_filter = ["is_active", "company"]
    search_fields = ["name", "key"]

    def get_readonly_fields(self, request, obj=None):
        return ("key", "company") if obj is not None else ()


# -- Connected accounts (docs/crm/phase6-adapters.md 2.6-2.7) ------------------
# The ciphertext is never a field here: not listed, not editable, not
# rendered. Secrets are entered through Connected accounts on
# /erp/integrations/ (core.integrations.accounts.set_credential), which
# encrypts them; the admin only shows that a credential exists.

from .models import IntegrationAccount, IntegrationCredential  # noqa: E402


@admin.register(IntegrationAccount)
class IntegrationAccountAdmin(admin.ModelAdmin):
    list_display = ("provider", "company", "enabled", "paused_until", "last_success_at", "last_error")
    list_filter = ("provider", "enabled")
    readonly_fields = ("sync_state", "last_success_at", "last_error_at", "last_error", "created_by",
                       "created_at", "updated_at")

    def has_delete_permission(self, request, obj=None):
        # Disable instead while golden still holds this provider's records:
        # erasure reaches them through the account (CRM Phase 6 step 8).
        from core.integrations.erasure import account_holds_records

        if obj is not None and account_holds_records(obj):
            return False
        return super().has_delete_permission(request, obj)


@admin.register(IntegrationCredential)
class IntegrationCredentialAdmin(admin.ModelAdmin):
    list_display = ("account", "kind", "owner", "subject", "key_id", "revoked_at", "expires_at")
    list_filter = ("kind",)
    exclude = ("ciphertext",)
    readonly_fields = ("company", "account", "owner", "kind", "key_id", "subject", "scopes",
                       "expires_at", "last_used_at", "sync_state", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False  # no plaintext ever enters through the admin
