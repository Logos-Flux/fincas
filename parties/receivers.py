"""Receivers ``PartiesConfig.ready()`` connects (phase2-party.md 2.4, 2.5)."""

from __future__ import annotations


def company_saved(sender, instance, raw=False, created=False, **kwargs):
    """2.4: the tenant's ``is_self`` party follows a Company rename."""
    if raw or created:
        return
    from .sync import rename_self_party

    rename_self_party(instance)


def customer_address_deleted(sender, instance, **kwargs):
    """A deleted CustomerAddress takes its PartyAddress mirror with it."""
    from .sync import delete_orphan_party_address

    delete_orphan_party_address(instance.party_address_id)
