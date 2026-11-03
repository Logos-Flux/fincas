"""Provenance hooks for ``core.ExternalRef`` (docs/crm/phase2-party.md 1.7).

``ExternalRef`` points at its target generically, so the database cannot
cascade a target's deletion to its refs. ``track(Model)`` closes that gap
for hard deletes: it registers a ``post_delete`` receiver that deletes the
refs of the deleted row. Call it from the owning app's ``ready()`` (or use it
as a class decorator). Rows deleted without signals (raw SQL, a cascade from
outside Django) are what ``manage.py check_external_refs`` reports.

Merge and erasure never hard-delete a party, so refs survive both on
purpose: an erased party's refs let the next import skip the record instead
of resurrecting the person.
"""

from __future__ import annotations

import hashlib

from django.db.models.signals import post_delete

from .sidecar import all_system_keys

_TRACKED: set[str] = set()

ERASED_PREFIX = "erased:"


def _delete_refs(sender, instance, **kwargs) -> None:
    from django.contrib.contenttypes.models import ContentType

    from .models import ExternalRef

    content_type = ContentType.objects.get_for_model(sender)
    ExternalRef.objects.filter(content_type=content_type, object_id=instance.pk).delete()


def track(model):
    """Delete a ``model`` row's ExternalRefs when the row is deleted. Idempotent.

    ``model`` must have a UUID primary key (``ExternalRef.object_id`` is a
    UUID). Returns ``model``, so it also works as a class decorator.
    """
    pk = model._meta.pk
    if pk.get_internal_type() != "UUIDField":
        raise TypeError(
            f"{model._meta.label} has a {pk.get_internal_type()} primary key; "
            "ExternalRef targets need a UUID primary key."
        )
    post_delete.connect(
        _delete_refs, sender=model, weak=False,
        dispatch_uid=f"core.provenance.track:{model._meta.label_lower}",
    )
    _TRACKED.add(model._meta.label_lower)
    return model


def tracked_models() -> frozenset[str]:
    """Labels (``app_label.model``) registered with ``track``."""
    return frozenset(_TRACKED)


# --- erased source records (phase2-party.md 1.7, 7.4) --------------------------


# --- system keys in ``metadata`` sidecars (phase4-fields.md 3.1, 3.3) -----------

# Keys golden code writes into a ``metadata`` sidecar and reads back. The
# registry is ``core.sidecar.SYSTEM_KEYS`` (per sidecar model); this is its
# union plus the keys golden writes on non-sidecar ``metadata`` columns
# (``ExternalRef``: ``erased``, ``erased_at``, ``legacy_column``, ``backfill``;
# imported invoices: ``bc_import``). A client write must never set, change or
# delete one, and erasure keeps them (when they do not name the person) so the
# syncs stay idempotent.
NON_SIDECAR_SYSTEM_KEYS = frozenset({
    "bc_import", "erased", "erased_at", "legacy_column",
    # CRM import (phase5-importer.md 2.3, 2.4): what has no column or sidecar,
    # kept on the ExternalRef of an owner (teams, properties), a stage or a
    # ticket (2.8: its facts, and a linked second ticket's properties).
    "source_properties", "source_stage", "source_ticket", "teams",
})
SYSTEM_METADATA_KEYS = all_system_keys() | NON_SIDECAR_SYSTEM_KEYS
# Reserved namespaces: ``_private`` keys and ``provenance`` / ``provenance.*``.
SYSTEM_METADATA_PREFIXES = ("_", "provenance")


def is_system_metadata_key(key) -> bool:
    """Whether ``key`` belongs to golden code, not to API clients."""
    key = str(key)
    return key in SYSTEM_METADATA_KEYS or key.startswith(SYSTEM_METADATA_PREFIXES)


def merge_metadata(current, patch) -> dict:
    """RFC 7396 JSON merge patch of ``patch`` onto ``current``: objects merge
    key by key (recursively), ``null`` removes a key, anything else replaces."""
    result = dict(current) if isinstance(current, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict):
            result[key] = merge_metadata(result.get(key), value)
        else:
            result[key] = value
    return result


def erased_source_id(source_id: str) -> str:
    """The key an erased ref is stored under: ``erased:<sha256(source id)>``.

    A source id is often the person's name or username (ERPNext customer
    names, eBay buyers), so erasure keeps only its hash. Idempotent.
    """
    source_id = source_id or ""
    if source_id.startswith(ERASED_PREFIX):
        return source_id
    return ERASED_PREFIX + hashlib.sha256(source_id.encode("utf-8")).hexdigest()


def is_erased_source(company_id, source: str, object_type: str, source_id) -> bool:
    """Whether this source record belongs to a person golden erased.

    Every importer asks this before creating or updating from a source
    record, and skips the record on ``True``: re-importing it would bring
    the erased person back.
    """
    from .models import ExternalRef

    source_id = str(source_id or "").strip()
    if not source_id:
        return False
    return ExternalRef._base_manager.filter(
        company_id=company_id, source=source, object_type=object_type,
        source_id__in={source_id, erased_source_id(source_id)}, metadata__erased=True,
    ).exists()
