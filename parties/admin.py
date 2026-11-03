"""Read-only admin for the party schema (phase2-party.md step 3).

Writes go through the services, sync and API of later steps, which keep the
denormalised columns, mirrored Customer fields and consent ledger honest; the
admin is for inspection only.
"""

from django.contrib import admin

from .models import (
    ConsentRecord,
    Party,
    PartyAddress,
    PartyEmail,
    PartyPhone,
    PartyRelationship,
    PartyRole,
    Suppression,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Party)
class PartyAdmin(ReadOnlyAdmin):
    list_display = ["display_name", "kind", "company", "primary_email_normalized", "origin",
                    "is_active", "updated_at"]
    list_filter = ["kind", "origin", "is_active", "is_self", "marketable"]
    search_fields = ["display_name", "legal_name", "primary_email_normalized", "primary_phone_e164"]
    list_select_related = ["company"]


@admin.register(PartyRole)
class PartyRoleAdmin(ReadOnlyAdmin):
    list_display = ["party", "role", "valid_from", "valid_to", "source"]
    list_filter = ["role"]
    search_fields = ["party__display_name"]
    list_select_related = ["party"]


@admin.register(PartyRelationship)
class PartyRelationshipAdmin(ReadOnlyAdmin):
    list_display = ["from_party", "kind", "to_party", "valid_from", "valid_to", "is_primary"]
    list_filter = ["kind", "is_symmetric"]
    search_fields = ["from_party__display_name", "to_party__display_name"]
    list_select_related = ["from_party", "to_party"]


@admin.register(PartyEmail)
class PartyEmailAdmin(ReadOnlyAdmin):
    list_display = ["email", "party", "purpose", "is_primary", "status"]
    list_filter = ["purpose", "status", "is_primary"]
    search_fields = ["email_normalized", "party__display_name"]
    list_select_related = ["party"]


@admin.register(PartyPhone)
class PartyPhoneAdmin(ReadOnlyAdmin):
    list_display = ["number_raw", "number_e164", "party", "purpose", "line_type", "is_primary"]
    list_filter = ["purpose", "line_type", "is_primary"]
    search_fields = ["number_e164", "number_raw", "party__display_name"]
    list_select_related = ["party"]


@admin.register(PartyAddress)
class PartyAddressAdmin(ReadOnlyAdmin):
    list_display = ["party", "purpose", "address_line_1", "city", "country", "is_primary"]
    list_filter = ["purpose", "country", "is_primary"]
    search_fields = ["party__display_name", "address_line_1", "city", "postal_code"]
    list_select_related = ["party"]


@admin.register(ConsentRecord)
class ConsentRecordAdmin(ReadOnlyAdmin):
    list_display = ["party", "channel", "purpose", "state", "occurred_at", "source"]
    list_filter = ["channel", "purpose", "state", "source"]
    search_fields = ["party__display_name", "address_normalized"]
    list_select_related = ["party"]


@admin.register(Suppression)
class SuppressionAdmin(ReadOnlyAdmin):
    list_display = ["channel", "address_hint", "reason", "company", "created_at", "lifted_at"]
    list_filter = ["channel", "reason"]
    search_fields = ["address_hash", "address_hint"]
    list_select_related = ["company"]
