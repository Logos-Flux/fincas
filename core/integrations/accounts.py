"""Services over ``IntegrationAccount`` / ``IntegrationCredential`` (docs/crm/phase6-adapters.md 2.6-2.7).

The views, ``manage.py integration_connect`` and the tests all write through
here, so the rules live once:

- an account's provider must be an installed adapter, its settings must pass
  the adapter's ``validate_settings``, and there is one per provider per
  company;
- a credential is written encrypted, replaces the active one of its kind
  (the old row is revoked, never overwritten, so the history stays), and
  lifts a "credentials rejected" pause;
- every write leaves an ``AuditLog`` row naming the account, the kind and
  the subject: never the secret, its ciphertext or its key.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.models import AuditLog, IntegrationAccount, IntegrationCredential

logger = logging.getLogger("golden_erp.integrations")

__all__ = [
    "adapter_class",
    "adapter_for",
    "account_secret_values",
    "create_account",
    "installed_adapters",
    "revoke_credential",
    "set_credential",
    "update_account",
]


def installed_adapters() -> dict:
    from core.plugins import plugin_registry

    return plugin_registry.get_crm_adapters()


def adapter_class(provider: str):
    return installed_adapters().get(provider)


def adapter_for(account, transport=None):
    """The adapter instance for ``account``; ``LookupError`` when its provider is not installed."""
    from .transport import get_transport

    cls = adapter_class(account.provider)
    if cls is None:
        raise LookupError(account.provider)
    return cls(account, transport if transport is not None else get_transport(account))


def _profile(user):
    return getattr(user, "userprofile", None) if user is not None else None


def _audit(user, action: str, obj, changes: dict) -> None:
    AuditLog.objects.create(
        user=_profile(user), action=action, model_name=f"core.{type(obj).__name__}",
        object_id=obj.pk, changes=changes,
    )


def _validate(account, settings) -> None:
    cls = adapter_class(account.provider)
    if cls is None:
        raise ValidationError({"provider": f"No installed adapter provides {account.provider!r}."})
    errors = cls(account, None).validate_settings(settings)
    if errors:
        raise ValidationError({"settings": errors})


def create_account(company, provider: str, *, settings=None, enabled=False, user=None) -> IntegrationAccount:
    settings = {} if settings is None else settings
    account = IntegrationAccount(company=company, provider=provider, settings=settings, enabled=enabled,
                                 created_by=user if getattr(user, "pk", None) else None)
    _validate(account, settings)
    if IntegrationAccount.objects.filter(company=company, provider=provider).exists():
        raise ValidationError({"provider": f"This company already has a {provider} account (one per provider)."})
    with transaction.atomic():
        account.save()
        _audit(user, "create", account, {"provider": provider, "enabled": enabled,
                                         "settings_keys": sorted(settings)})
    return account


def update_account(account, *, settings=None, enabled=None, resume=False, user=None) -> IntegrationAccount:
    changes = {}
    if settings is not None:
        _validate(account, settings)
        account.settings = settings
        changes["settings_keys"] = sorted(settings)
    if enabled is not None and enabled != account.enabled:
        account.enabled = enabled
        changes["enabled"] = enabled
    if resume and account.paused_until is not None:
        account.paused_until = None
        changes["resumed"] = True
    if changes:
        with transaction.atomic():
            account.save()
            _audit(user, "update", account, changes)
    return account


def set_credential(account, kind: str, plaintext: str, *, owner=None, subject: str = "", scopes=None,
                   expires_at=None, user=None) -> IntegrationCredential:
    """Store ``plaintext`` encrypted as ``account``'s active ``kind`` credential (revoking the previous)."""
    if kind not in IntegrationCredential.Kind.values:
        raise ValidationError({"kind": f"Unknown credential kind {kind!r}."})
    if owner is not None and owner.company_id != account.company_id:
        raise ValidationError({"owner": "A grant belongs to a profile of the account's company."})
    if not isinstance(plaintext, str) or not plaintext.strip():
        raise ValidationError({"secret": "Enter the secret."})
    cred = IntegrationCredential(
        account=account, company_id=account.company_id, owner=owner, kind=kind,
        subject=(subject or "")[:255], scopes=list(scopes or []), expires_at=expires_at,
    )
    cred.set_secret(plaintext.strip())
    now = timezone.now()
    with transaction.atomic():
        IntegrationCredential.objects.filter(
            account=account, owner=owner, kind=kind, revoked_at__isnull=True,
        ).update(revoked_at=now)
        cred.save()
        if owner is None and account.last_error == "credentials_rejected":
            IntegrationAccount.objects.filter(pk=account.pk).update(paused_until=None, last_error="")
            account.paused_until, account.last_error = None, ""
        _audit(user, "set_credential", account, {"credential": str(cred.pk), "kind": kind,
                                                 "per_user": owner is not None, "subject": cred.subject})
    return cred


def revoke_credential(cred, *, user=None) -> None:
    if cred.revoked_at is not None:
        return
    cred.revoked_at = timezone.now()
    with transaction.atomic():
        IntegrationCredential.objects.filter(pk=cred.pk).update(revoked_at=cred.revoked_at)
        _audit(user, "revoke_credential", cred.account, {"credential": str(cred.pk), "kind": cred.kind,
                                                         "per_user": cred.owner_id is not None})


def account_secret_values(account) -> list[str]:
    """Every decryptable secret of ``account`` (all kinds, all owners, revoked too), longest first.

    For redaction only (``core.integration_checks.redact(extra=)``): never render these.
    """
    from core.secrets import InvalidToken

    values = set()
    for cred in IntegrationCredential.objects.filter(account=account):
        try:
            value = cred.reveal().get()
        except InvalidToken:
            continue
        values.add(value)
        # An oauth_client credential is JSON: its parts are secrets too.
        if cred.kind == IntegrationCredential.Kind.OAUTH_CLIENT:
            import json

            try:
                parts = json.loads(value)
            except ValueError:
                parts = {}
            if isinstance(parts, dict) and isinstance(parts.get("client_secret"), str):
                values.add(parts["client_secret"])
    return sorted((v for v in values if len(v) >= 4), key=len, reverse=True)
