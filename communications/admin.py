from django.contrib import admin

from .models import EmailLog


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = [
        "document_type", "document_number", "recipient_email",
        "status", "triggered_by", "sent_by", "created_at",
    ]
    list_filter = ["status", "document_type", "triggered_by"]
    search_fields = ["document_number", "recipient_email", "anymail_message_id"]
    readonly_fields = [
        "id", "document_type", "document_id", "document_number",
        "recipient_email", "subject", "template_name", "status",
        "error_message", "triggered_by", "sent_by", "anymail_message_id",
        "created_at", "updated_at",
    ]
    ordering = ["-created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# --- CRM Phase 3: read-only admin (docs/crm/phase3-engagement.md step 4) ---
# Writes go through communications.ingest.record and the EmailLog link; the
# admin only inspects. The raw sidecar needs view_raw_communication.

from .models import Communication, CommunicationParticipant, CommunicationRaw  # noqa: E402


class _ReadOnlyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class CommunicationParticipantInline(admin.TabularInline):
    model = CommunicationParticipant
    extra = 0
    fields = ["role", "party", "address_raw", "address_normalized", "display_name"]
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Communication)
class CommunicationAdmin(_ReadOnlyAdmin):
    list_display = ["occurred_at", "kind", "direction", "status", "subject", "party",
                    "engagement", "provider", "company"]
    list_filter = ["kind", "direction", "status", "provider"]
    search_fields = ["subject", "thread_key"]
    list_select_related = ["party", "engagement", "company"]
    raw_id_fields = ["engagement", "party", "performed_by", "email_log"]
    inlines = [CommunicationParticipantInline]
    ordering = ["-occurred_at"]


@admin.register(CommunicationParticipant)
class CommunicationParticipantAdmin(_ReadOnlyAdmin):
    list_display = ["communication", "role", "party", "address_normalized", "company"]
    list_filter = ["role"]
    search_fields = ["address_normalized", "display_name"]
    raw_id_fields = ["communication", "party", "party_email", "party_phone"]


@admin.register(CommunicationRaw)
class CommunicationRawAdmin(_ReadOnlyAdmin):
    list_display = ["communication", "provider", "schema_version", "fetched_at", "company"]
    list_filter = ["provider"]
    raw_id_fields = ["communication"]

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm("communications.view_raw_communication")

    def has_module_permission(self, request):
        return self.has_view_permission(request)
