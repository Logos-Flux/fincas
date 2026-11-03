"""Chat session and message models for the embedded ERP assistant.

Chat state lives in Django so history is per-user, resumable across
logins/devices, and available as future fine-tuning data. Messages
record not just the user/assistant text but also the structured
tool-call events and the page context captured at the time of the
message — which matters for diagnosing "why did the assistant pick
the wrong product" long after the page has moved on.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import BaseModel


class ChatSession(BaseModel):
    """One conversation between a staff user and the assistant.

    A session corresponds to what a user would call "a chat" — opened
    via the "New chat" button in the drawer, populated over time, and
    resumable later from the sessions sidebar.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_sessions",
    )
    # Auto-generated from the first user message when that message lands.
    # Stored in the column rather than computed so it's cheap to list.
    title = models.CharField(max_length=200, blank=True, default="")
    # Matches a key in chat/llm/registry.py::MODELS. Preserved per-session
    # so users can compare models on the same question later. Rows written
    # under an older registry keep their retired key; registry.ALIASES maps
    # it forward at read time, so no data migration is needed on a swap.
    model_id = models.CharField(max_length=64, default="claude-sonnet-5")
    # Free-form metadata for future use: total_tokens, cost_estimate,
    # tool_call_count, etc. Populated incrementally by the stream view.
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["user", "-updated_at"]),
        ]

    def __str__(self) -> str:
        label = self.title or f"Untitled chat {self.id}"
        return f"{label} ({self.user})"


class ChatMessage(BaseModel):
    """One message in a chat session.

    Roles follow the Anthropic Messages API model: "user" and "assistant"
    carry natural-language content, "tool" carries tool invocation results
    echoed back to the model on the next turn. We persist all three so
    the transcript can be replayed or exported as training data later.
    """

    ROLE_CHOICES = [
        ("user", "User"),
        ("assistant", "Assistant"),
        ("tool", "Tool"),
        ("system", "System"),
    ]

    session = models.ForeignKey(
        ChatSession,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    # Natural-language content for user/assistant messages. For tool
    # messages this is a plain-text summary of the result; the full
    # structured result lives in `tool_calls`.
    content = models.TextField(blank=True, default="")
    # List of {name, arguments, result} dicts. Assistant messages that
    # invoke tools use this to record what was called; tool-role messages
    # use it to record the structured payload echoed back to the model.
    tool_calls = models.JSONField(default=list, blank=True)
    # Page context captured at the time the user sent the message —
    # pathname, page_type, record IDs. Stored so we can see what the
    # assistant "knew" when it answered.
    context = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["session", "created_at"]),
        ]

    def __str__(self) -> str:
        preview = (self.content or "").strip()[:60]
        return f"[{self.role}] {preview}"
