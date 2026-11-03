"""Per-company adapter accounts and their encrypted credentials
(docs/crm/phase6-adapters.md 2.6, 2.7).

Registered under the ``core`` app (imported by ``core/models.py``, like
``UserIdentity``). Settings only *name* the adapters (``GOLDEN_PLUGINS``) and
hold the encryption keys (``GOLDEN_SECRET_KEYS``); everything a tenant
configures for a provider lives here.

``IntegrationCredential.ciphertext`` is never serialised, never shown in the
admin, never logged and never put in an error. ``__str__``/``__repr__`` name
the provider and kind only. ``reveal()`` returns a ``core.secrets.Revealed``,
which does not print its value either.
"""

from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.managers import CompanyScopedManager


class IntegrationAccount(models.Model):
    """One provider connection of one company (2.6): **one per provider per company**.

    A second account of one provider in one tenant would need an
    ``ExternalRef`` source per account; it is refused by
    ``integration_account_uniq`` rather than half-supported.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    # An installed adapter's provider_id (BaseCrmAdapter.provider_id).
    provider = models.CharField(max_length=32)
    # Nothing polls, pushes or accepts webhooks until a staff user enables it.
    enabled = models.BooleanField(default=False)
    # Non-secret configuration, validated by the adapter's validate_settings.
    settings = models.JSONField(default=dict, blank=True)
    # Cursors and flags (initial_sync_done) owned by the poller.
    sync_state = models.JSONField(default=dict, blank=True, editable=False)
    # Set by a rate limit or a rejected credential (2.9), or by an operator.
    paused_until = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error_at = models.DateTimeField(null=True, blank=True, editable=False)
    # An error code and a sentence; never a provider response body.
    last_error = models.CharField(max_length=500, blank=True, editable=False)
    created_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    objects = CompanyScopedManager()

    class Meta:
        app_label = "core"
        ordering = ["provider"]
        constraints = [
            models.UniqueConstraint(fields=["company", "provider"], name="integration_account_uniq"),
        ]

    def __str__(self):
        return f"{self.provider} account"

    def __repr__(self):
        return f"<IntegrationAccount {self.provider} {self.pk}>"

    def is_paused(self, now=None) -> bool:
        return self.paused_until is not None and self.paused_until > (now or timezone.now())


class IntegrationCredential(models.Model):
    """One encrypted secret of an account (2.7).

    ``owner`` is null for a company credential (an API key, the deployment's
    OAuth client, a webhook secret) and set for a per-user grant (a Gmail
    mailbox): deleting the profile deletes the grant. At most one *active*
    (``revoked_at IS NULL``) credential per ``(account, owner, kind)``.
    """

    class Kind(models.TextChoices):
        API_KEY = "api_key", "API key"
        OAUTH_CLIENT = "oauth_client", "OAuth client"
        OAUTH_REFRESH = "oauth_refresh", "OAuth refresh token"
        OAUTH_ACCESS = "oauth_access", "OAuth access token"
        WEBHOOK_SECRET = "webhook_secret", "Webhook secret"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, related_name="+")
    account = models.ForeignKey(IntegrationAccount, on_delete=models.CASCADE, related_name="credentials")
    owner = models.ForeignKey(
        "core.UserProfile", on_delete=models.CASCADE, null=True, blank=True, related_name="+",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    # Fernet token (core.secrets). Never serialised, never in the admin, never logged.
    ciphertext = models.TextField(editable=False)
    # Fingerprint of the key that encrypted it (core.secrets.key_id), for rotation.
    key_id = models.CharField(max_length=16, editable=False)
    # What the grant is for, shown to staff (a mailbox address). Not secret.
    subject = models.CharField(max_length=255, blank=True)
    scopes = models.JSONField(default=list, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    # Per-grant pause (2.9): a rate limit or invalid_grant on one user's grant.
    paused_until = models.DateTimeField(null=True, blank=True)
    # Per-grant cursor (a Gmail historyId).
    sync_state = models.JSONField(default=dict, blank=True, editable=False)

    objects = CompanyScopedManager()

    # check_tenancy: a credential's company is its account's and its owner's.
    tenancy_parents = ("account", "owner")

    class Meta:
        app_label = "core"
        ordering = ["account", "kind", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "kind"], condition=Q(owner__isnull=True, revoked_at__isnull=True),
                name="integration_cred_company_uniq",
            ),
            models.UniqueConstraint(
                fields=["account", "owner", "kind"],
                condition=Q(owner__isnull=False, revoked_at__isnull=True),
                name="integration_cred_owner_uniq",
            ),
        ]
        indexes = [models.Index(fields=["key_id"], name="integration_cred_key_idx")]

    def __str__(self):
        who = " (per user)" if self.owner_id else ""
        return f"{self.account.provider if self.account_id else '?'} {self.kind}{who}"

    def __repr__(self):
        return f"<IntegrationCredential {self.kind} {self.pk}>"

    def clean(self):
        super().clean()
        self._check_tenancy()

    def _check_tenancy(self):
        if self.account_id is not None:
            self.company_id = self.account.company_id
        if self.owner_id is not None and self.owner.company_id != self.company_id:
            raise ValidationError({"owner": "A grant belongs to a profile of the account's company."})

    def save(self, *args, **kwargs):
        self._check_tenancy()
        if not self.ciphertext or not self.key_id:
            raise ValidationError("Set the secret with set_secret() before saving.")
        super().save(*args, **kwargs)

    # -- the secret ---------------------------------------------------------

    def set_secret(self, plaintext: str) -> None:
        """Encrypt ``plaintext`` under the newest key (does not save)."""
        from core import secrets

        self.ciphertext, self.key_id = secrets.encrypt(plaintext)

    def reveal(self):
        """The decrypted secret as a ``core.secrets.Revealed`` (``InvalidToken`` if unreadable)."""
        from core import secrets

        return secrets.Revealed(secrets.decrypt(self.ciphertext), self.kind)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def is_paused(self, now=None) -> bool:
        return self.paused_until is not None and self.paused_until > (now or timezone.now())
