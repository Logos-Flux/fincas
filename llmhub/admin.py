"""Django admin for llmhub — the ops path for rare toggles (fallback_to_env).

The encrypted token is not editable here; rotate it on /erp/llm/. Every save
from this admin is audited by the signal backstop (actor from the request).
"""

from django.contrib import admin

from .models import LaneObservation, LLMModel, LLMSettings, LLMSettingsAudit


@admin.register(LLMSettings)
class LLMSettingsAdmin(admin.ModelAdmin):
    list_display = ("mode", "gateway_base_url", "fallback_to_env", "updated_at")
    fields = ("mode", "gateway_base_url", "lane_map", "fallback_to_env")

    def has_add_permission(self, request):
        return not LLMSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LLMModel)
class LLMModelAdmin(admin.ModelAdmin):
    list_display = ("key", "label", "route", "enabled", "is_default", "sort")
    list_editable = ("enabled", "sort")


@admin.register(LLMSettingsAudit)
class LLMSettingsAuditAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "field", "old_masked", "new_masked")
    list_filter = ("action",)
    readonly_fields = [f.name for f in LLMSettingsAudit._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LaneObservation)
class LaneObservationAdmin(admin.ModelAdmin):
    list_display = ("route", "model", "observed_at")
