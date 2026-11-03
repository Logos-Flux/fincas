"""System checks for Connected-account encryption (docs/crm/phase6-adapters.md 2.7).

- ``core.E040``: stored credentials were encrypted under a key that is not in
  ``GOLDEN_SECRET_KEYS`` (or no key is configured at all), so no key can
  decrypt them. The integration would fail at its next call with no other
  sign; ``manage.py check`` fails instead.
- ``core.E041``: a ``GOLDEN_SECRET_KEYS`` entry is not a valid Fernet key.

Registered without the ``database`` tag on purpose, so plain ``manage.py
check`` runs it: the query is one ``DISTINCT key_id`` over the active rows
(it compares key fingerprints and decrypts nothing). A database that has no
table yet (before ``migrate``) is skipped silently.
"""

from __future__ import annotations

from django.core.checks import Error, register
from django.core.exceptions import ImproperlyConfigured
from django.db.utils import DatabaseError


@register()
def secret_keys_check(app_configs, **kwargs):
    from core import secrets
    from core.models import IntegrationCredential

    try:
        configured = set(secrets.configured_key_ids())
    except ImproperlyConfigured as exc:
        return [Error(str(exc), id="core.E041")]
    try:
        stored = set(
            IntegrationCredential._base_manager.filter(revoked_at__isnull=True)
            .values_list("key_id", flat=True).distinct()
        )
    except DatabaseError:
        return []
    unreadable = sorted(stored - configured)
    if not unreadable:
        return []
    count = IntegrationCredential._base_manager.filter(
        revoked_at__isnull=True, key_id__in=unreadable).count()
    cause = ("GOLDEN_SECRET_KEYS is empty" if not configured
             else "the key that encrypted them is no longer in GOLDEN_SECRET_KEYS")
    return [Error(
        f"{count} stored integration credential(s) cannot be decrypted — cause: {cause}.",
        hint=("Put the old key back in GOLDEN_SECRET_KEYS (after the new one), run "
              "`manage.py rotate_secrets`, then remove it; or re-enter those credentials under "
              "Connected accounts. Key fingerprints: " + ", ".join(unreadable[:5]) + "."),
        id="core.E040",
    )]
