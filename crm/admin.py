from django.contrib import admin

from .models import (
    ActivityLog,
    Engagement,
    EngagementContact,
    EngagementStageChange,
    Lead,
    Pipeline,
    Stage,
    Task,
    TaskEvent,
    Touchpoint,
)


class ActivityLogInline(admin.TabularInline):
    """Frozen since CRM Phase 3 step 6 (``ActivityLog.save()`` raises)."""

    model = ActivityLog
    extra = 0
    fields = ["activity_type", "subject", "performed_by", "activity_date"]
    readonly_fields = fields
    fk_name = "lead"
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = [
        "lead_number", "first_name", "last_name", "email",
        "status", "source", "assigned_to", "engagement", "created_at",
    ]
    list_filter = ["status", "source", "request_type"]
    search_fields = ["lead_number", "first_name", "last_name", "email", "company_name"]
    readonly_fields = ["id", "created_at", "updated_at", "engagement"]
    inlines = [ActivityLogInline]

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.engagement_id:
            # Triaged: the work happens on the engagement (spec 1.7).
            fields += [f for f in Lead.FROZEN_WHEN_TRIAGED if f not in fields]
        return fields


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    """Read-only: the rows live on as Communications and Tasks (same ids)."""

    list_display = ["activity_type", "subject", "lead", "customer", "performed_by", "activity_date"]
    list_filter = ["activity_type"]
    search_fields = ["subject"]

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# --- CRM Phase 3: read-only admin (docs/crm/phase3-engagement.md step 3) ---
# Writes go through crm.engagements and crm.tasks, which keep the history
# rows and the denormalised next action honest; the admin only inspects.


class ReadOnlyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False
    show_change_link = True

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class StageInline(ReadOnlyInline):
    model = Stage
    fields = ["sort_order", "key", "name", "default_probability", "outcome_hint", "is_active"]


@admin.register(Pipeline)
class PipelineAdmin(ReadOnlyAdmin):
    list_display = ["name", "key", "company", "is_default", "is_active", "sort_order"]
    list_filter = ["is_default", "is_active"]
    search_fields = ["name", "key"]
    list_select_related = ["company"]
    inlines = [StageInline]


@admin.register(Stage)
class StageAdmin(ReadOnlyAdmin):
    list_display = ["name", "key", "pipeline", "sort_order", "default_probability", "outcome_hint",
                    "is_active"]
    list_filter = ["outcome_hint", "is_active"]
    search_fields = ["name", "key", "pipeline__name"]
    list_select_related = ["pipeline"]


class EngagementContactInline(ReadOnlyInline):
    model = EngagementContact
    fields = ["party", "role", "is_primary"]


class EngagementStageChangeInline(ReadOnlyInline):
    model = EngagementStageChange
    fields = ["changed_at", "from_stage", "to_stage", "from_status", "to_status", "changed_by",
              "source", "note"]


@admin.register(Engagement)
class EngagementAdmin(ReadOnlyAdmin):
    list_display = ["number", "title", "company", "pipeline", "stage", "status", "owner",
                    "expected_value", "next_action_due_at", "last_activity_at"]
    list_filter = ["status", "origin", "pipeline"]
    search_fields = ["number", "title", "party__display_name"]
    list_select_related = ["company", "pipeline", "stage", "owner"]
    inlines = [EngagementContactInline, EngagementStageChangeInline]


@admin.register(EngagementStageChange)
class EngagementStageChangeAdmin(ReadOnlyAdmin):
    list_display = ["engagement", "from_stage", "to_stage", "from_status", "to_status", "changed_at",
                    "source"]
    list_filter = ["source", "to_status"]
    search_fields = ["engagement__number"]
    list_select_related = ["engagement", "from_stage", "to_stage"]


@admin.register(Touchpoint)
class TouchpointAdmin(ReadOnlyAdmin):
    list_display = ["occurred_at", "channel", "party", "engagement", "utm_source", "utm_campaign",
                    "source"]
    list_filter = ["channel", "click_id_kind"]
    search_fields = ["utm_campaign", "utm_source", "party__display_name"]
    list_select_related = ["party", "engagement"]


class TaskEventInline(ReadOnlyInline):
    model = TaskEvent
    fields = ["at", "event", "from_value", "to_value", "by"]


@admin.register(Task)
class TaskAdmin(ReadOnlyAdmin):
    list_display = ["title", "kind", "status", "priority", "assignee", "due_at", "due_tz",
                    "engagement"]
    list_filter = ["status", "priority", "kind", "all_day"]
    search_fields = ["title", "engagement__number"]
    list_select_related = ["assignee", "engagement"]
    inlines = [TaskEventInline]


@admin.register(TaskEvent)
class TaskEventAdmin(ReadOnlyAdmin):
    list_display = ["task", "event", "from_value", "to_value", "at", "by"]
    list_filter = ["event"]
    list_select_related = ["task", "by"]
