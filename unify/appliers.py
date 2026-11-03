"""Applier registry: how an approved staged row reaches the domain (step 6).

``unify.apply`` runs approval runs (chunked, idempotent per row,
cancellable, one undo token per run) without knowing what a row *is*. Each
``ImportBatch.entity_kind`` names an applier that does:

- ``apply(company, staged, *, run, approval_row) -> str``: write the row to
  the domain and return its ``applied_ref``. Raise to fail the row; the
  error is recorded on the row, never propagated.
- ``undo(approval_row) -> int``: reverse one applied row and return how many
  domain objects it reverted. Raise ``ValueError`` to refuse (the row stays
  applied and the reason is reported).

Optional per-applier facts:

- ``permission``: a Django permission a human needs to start, apply or undo
  a run of this kind from a screen (the server-side executor never checks
  it; the approver already did). ``None`` means staff is enough.
- ``undo_newest_first``: undo the run's rows in reverse apply order, for
  appliers whose undo must be LIFO (merges).
- ``undo_state``: where an undone row goes. ``approved`` (products: the row
  is still right, re-apply recreates it) or ``proposed`` (merges: an undo
  usually means "that was wrong", so the decision is cleared and a later
  "apply approved" cannot quietly redo it).
- ``gate(batch, action) -> str``: a refusal for ``action`` (``"apply"``
  opens a run, ``"undo"`` undoes one) started from a generic entry point
  (the batch screen, ``unify_apply --batch``), or ``""`` to allow it. Kinds
  whose batches belong to a larger job use it to send the operator to the
  job's own runner (``crm_import``: step order and source lock).

``product`` (registered here) is the original Steps 3-4 logic, unchanged.
Other apps register their kinds from ``AppConfig.ready`` (``parties``
registers ``party_merge``), so ``unify`` never imports them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction


@dataclass(frozen=True)
class Applier:
    entity_kind: str
    apply: Callable[..., str]
    undo: Callable[..., int]
    permission: str | None = None
    undo_newest_first: bool = False
    undo_state: str = "approved"
    gate: Callable[..., str] | None = None

    def refusal(self, batch, action: str) -> str:
        """Why a generic screen or command may not ``action`` this batch ("" = it may)."""
        return self.gate(batch, action) if self.gate is not None else ""


_REGISTRY: dict[str, Applier] = {}


def register_applier(
    entity_kind: str,
    apply_fn: Callable[..., str],
    undo_fn: Callable[..., int],
    *,
    permission: str | None = None,
    undo_newest_first: bool = False,
    undo_state: str = "approved",
    gate: Callable[..., str] | None = None,
) -> Applier:
    """Register (or re-register, idempotently) the applier for a kind."""
    if undo_state not in ("approved", "proposed"):
        raise ValueError("undo_state is 'approved' or 'proposed'.")
    applier = Applier(entity_kind, apply_fn, undo_fn, permission, undo_newest_first, undo_state, gate)
    existing = _REGISTRY.get(entity_kind)
    if existing is not None and (existing.apply, existing.undo) != (apply_fn, undo_fn):
        raise ValueError(f"An applier for {entity_kind!r} is already registered.")
    _REGISTRY[entity_kind] = applier
    return applier


def get_applier(entity_kind: str) -> Applier:
    try:
        return _REGISTRY[entity_kind]
    except KeyError:
        raise LookupError(
            f"No applier registered for entity kind {entity_kind!r}; "
            f"known: {sorted(_REGISTRY)}."
        ) from None


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)


# --- product (Steps 3-4, unchanged) ------------------------------------------


def _dec(value, default="0"):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal(default)


def apply_product(company, staged, **_context) -> str:
    """Create Product + Variant from mapped values. Returns the ref."""
    from products.models import Product, ProductVariant

    mapped = staged.mapped_values or {}
    title = (mapped.get("title") or "").strip()
    if not title:
        raise ValueError("Row has no title — cannot create a product.")
    sku = (mapped.get("sku") or "").strip()
    try:
        with transaction.atomic():
            product = Product.objects.create(
                title=title[:255],
                part_number=(mapped.get("part_number") or "")[:100],
                description=mapped.get("description") or "",
                company=company,
            )
            if sku:
                ProductVariant.objects.create(
                    product=product,
                    sku=sku,
                    retail_price=_dec(mapped.get("retail_price")),
                    msrp_price=_dec(mapped.get("msrp_price"))
                    if mapped.get("msrp_price") else None,
                )
    except IntegrityError as exc:
        raise ValueError(
            f"Row duplicates an existing record ({exc}). Edit the row or "
            "mark it N/A."
        ) from exc
    return f"product:{product.pk}"


def undo_product(approval_row) -> int:
    """Deactivate the product the row created. Non-destructive by design."""
    from products.models import Product

    ref = approval_row.staged_row.applied_ref
    if ref.startswith("product:"):
        return Product.objects.filter(
            pk=ref.split(":", 1)[1], is_active=True
        ).update(is_active=False)
    return 0


register_applier("product", apply_product, undo_product)
