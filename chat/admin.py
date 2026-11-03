from django.contrib import admin

from .models import ChatMessage, ChatSession


class ChatMessageInline(admin.TabularInline):
    model = ChatMessage
    extra = 0
    readonly_fields = ("role", "content", "tool_calls", "context", "created_at")
    can_delete = False
    fields = ("role", "content", "created_at")
    ordering = ("created_at",)


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "model_id", "updated_at", "message_count")
    list_filter = ("model_id", "created_at")
    search_fields = ("title", "user__username", "user__email")
    readonly_fields = ("id", "created_at", "updated_at", "metadata")
    inlines = [ChatMessageInline]
    ordering = ("-updated_at",)

    @admin.display(description="Messages")
    def message_count(self, obj: ChatSession) -> int:
        return obj.messages.count()


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("session", "role", "created_at", "short_content")
    list_filter = ("role", "created_at")
    search_fields = ("content", "session__title")
    readonly_fields = ("id", "created_at", "updated_at")
    ordering = ("-created_at",)

    @admin.display(description="Content preview")
    def short_content(self, obj: ChatMessage) -> str:
        text = (obj.content or "").strip().replace("\n", " ")
        return text[:100] + ("…" if len(text) > 100 else "")
