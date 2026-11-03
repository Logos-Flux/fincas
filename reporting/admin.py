from django.contrib import admin

from .models import ReportDefinition


@admin.register(ReportDefinition)
class ReportDefinitionAdmin(admin.ModelAdmin):
    list_display = ["name", "chart_type", "is_active"]
    list_filter = ["is_active", "chart_type"]
    search_fields = ["name"]
    readonly_fields = ["id", "created_at", "updated_at"]
