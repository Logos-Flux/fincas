"""``UserIdentity``: one external login subject bound to one ``auth.User``."""

import uuid

from django.db import models


class UserIdentity(models.Model):
    """A provider's stable subject (``sub``) linked to a ``User`` (§2.2).

    Password logins write no row: the password lives on ``User``. Rows exist
    only for external subjects, plus inert history rows written by the step-3
    backfill under reserved provider names (``core.identity``).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        "auth.User", on_delete=models.CASCADE, related_name="identities",
    )
    # Key in GOLDEN_IDENTITY_PROVIDERS. Persisted forever.
    provider = models.CharField(max_length=64)
    subject = models.CharField(max_length=255)
    # Last seen from the provider, informational only. Never used to link.
    email = models.EmailField(blank=True)
    email_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    # Allow-listed claims only (name, picture). Never tokens.
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "user identity"
        verbose_name_plural = "user identities"
        ordering = ["provider", "subject"]
        # Read-only through permissions: links change only through the portal
        # resolver and the invite views (code paths, no model permission
        # needed) or a later admin "link identity" action (spec §4), which
        # adds the permission it needs.
        default_permissions = ("view",)
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "subject"],
                name="core_useridentity_provider_subject_uniq",
            ),
            # A user holds at most one subject per provider (§2.2). The
            # resolver checks it under the row locks (attach_identity); this
            # holds it for every other writer (the backfill, a shell, raw
            # SQL). Its index replaces the plain (user, provider) one.
            models.UniqueConstraint(
                fields=["user", "provider"],
                name="core_useridentity_user_provider_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.provider}:{self.subject}"

    def save(self, *args, **kwargs):
        self.email = (self.email or "").lower()
        super().save(*args, **kwargs)
