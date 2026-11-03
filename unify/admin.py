from django.contrib import admin

from .models import (
    ApprovalRow,
    ApprovalRun,
    ColumnMapTemplate,
    ImportBatch,
    StagedRow,
)


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = ("source_name", "company", "status", "created_at")
    list_filter = ("status", "source_kind")
    readonly_fields = ("llm_calls_used",)


@admin.register(StagedRow)
class StagedRowAdmin(admin.ModelAdmin):
    list_display = ("batch", "row_number", "mapping_state", "mapping_confidence")
    list_filter = ("mapping_state",)


@admin.register(ApprovalRun)
class ApprovalRunAdmin(admin.ModelAdmin):
    list_display = ("batch", "kind", "status", "actor", "created_at")
    list_filter = ("kind", "status")


@admin.register(ApprovalRow)
class ApprovalRowAdmin(admin.ModelAdmin):
    list_display = ("run", "staged_row", "state")
    list_filter = ("state",)


@admin.register(ColumnMapTemplate)
class ColumnMapTemplateAdmin(admin.ModelAdmin):
    list_display = ("source_kind", "name")
