"""The Klaviyo cases' extra seed (``case.json`` "klaviyo"), passed to the contract runner as ``seed_hook``.

Keys, all optional::

    "klaviyo": {
      "first_poll_pending": false,                             # default: the first poll has run
      "marketable": ["ada"],                                   # Party.marketable
      "consents": [{"party": "ada", "state": "granted", "occurred_at": "...", "address": ""}],
      "suppressions": [{"address": "ada@example.com", "reason": "opt_out"}],
      "profiles": [{"party": "ada", "source_id": "01FIX...", "state": "subscribed", "list": "XFIX01",
                    "address": "ada@example.com", "suppressed": false, "lists": []}],
      "saved_filters": [{"id": "<uuid>", "owner": "rep", "params": {"q": "Ada"}}]
    }

Everything is invented (reserved domains, ``01FIXTURE…`` ids).
"""

from __future__ import annotations

from datetime import datetime


def _when(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def seed(tenant, case) -> None:
    from django.contrib.contenttypes.models import ContentType

    from core.models import ExternalRef, SavedFilter, UserProfile
    from parties.consent import address_hash, normalize_address
    from parties.models import ConsentRecord, Party, Suppression

    spec = case.get("klaviyo") or {}
    company = tenant.company
    # Pushes wait for the first poll (Klaviyo's opt-outs first); a case is past
    # it unless it says otherwise.
    if not spec.get("first_poll_pending"):
        type(tenant.account)._base_manager.filter(pk=tenant.account.pk).update(
            sync_state={"initial_sync_done": True})
        tenant.account.sync_state = {"initial_sync_done": True}

    def obj(key):
        pk = next(k for k, v in tenant.keys.by_id.items() if v == key)
        return pk

    for key in spec.get("marketable", []):
        Party.objects.filter(pk=obj(f"party:{key}")).update(marketable=True)
    for row in spec.get("consents", []):
        ConsentRecord.objects.create(
            party_id=obj(f"party:{row['party']}"), company=company, channel="email", purpose="marketing",
            state=row["state"], address_normalized=normalize_address("email", row.get("address", "")),
            occurred_at=_when(row["occurred_at"]), source=row.get("source", "portal"),
            source_detail=row.get("source_detail", "fixture"),
        )
    for row in spec.get("suppressions", []):
        Suppression.objects.create(
            company=company, channel="email",
            address_hash=address_hash(normalize_address("email", row["address"])),
            reason=row["reason"], source=row.get("source", "erp"), source_detail=row.get("source_detail", ""),
        )
    party_ct = ContentType.objects.get_for_model(Party)
    for row in spec.get("profiles", []):
        meta = {k: row[k] for k in ("state", "list", "suppressed", "lists", "fp") if k in row}
        if row.get("address"):
            meta["address"] = address_hash(normalize_address("email", row["address"]))
        if row.get("erased"):
            meta = {"erased": True}
        ExternalRef.objects.create(
            company=company, source="klaviyo", object_type="profile", source_id=row["source_id"],
            content_type=party_ct, object_id=obj(f"party:{row['party']}"), metadata=meta,
        )
    for row in spec.get("saved_filters", []):
        owner = UserProfile.objects.get(pk=obj(f"staff:{row['owner']}")).user
        SavedFilter.objects.create(
            id=row["id"], company=company, owner=owner, list_id="parties.party", name=row.get("name", "Segment"),
            params=row.get("params", {}), visibility="tenant",
        )
