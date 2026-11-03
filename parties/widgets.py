"""The party picker: a text input the party search enhances.

``PartySearchInput`` renders a plain text input, so without JavaScript a
form still takes what ``crm.forms.resolve_party`` accepts (a party id or an
exact email address). ``erp/_party_search_js.html`` turns every input
carrying ``data-party-search`` into a combobox over ``erp-party-search``:
the visible box shows the picked party's name and email, and a hidden input
under the field's name submits its id. Typing without picking submits the
typed text, so an exact email still works.
"""

from __future__ import annotations

import uuid

from django import forms
from django.urls import reverse

from .constants import ORGANISATION, PERSON


def party_choice_label(party) -> str:
    """``Name · email`` (the email the party is best known by, when it has one)."""
    email = getattr(party, "matched_email", "") or party.primary_email_normalized
    return f"{party.display_name} · {email}" if email else party.display_name


class PartySearchInput(forms.TextInput):
    """``kind``: ``person`` / ``organisation`` narrows the search; ``company_id``
    scopes it (and the label of the current value) to the form's tenant;
    ``exclude_id`` leaves one party out of the results (the party a
    relationship hangs off); ``empty_text`` replaces the no-match hint, which
    otherwise points at a "New" link beside the picker."""

    def __init__(self, attrs=None, *, kind="", company_id=None, exclude_id=None, empty_text=""):
        if kind not in ("", PERSON, ORGANISATION):
            raise ValueError(f"PartySearchInput kind {kind!r}")
        self.kind = kind
        self.company_id = company_id
        self.exclude_id = exclude_id
        self.empty_text = empty_text
        defaults = {"autocomplete": "off", "spellcheck": "false",
                    "placeholder": "Search by email or name"}
        super().__init__({**defaults, **(attrs or {})})

    def current_label(self, value) -> str:
        """The display text for a value that is a live party id of the tenant, else ""."""
        try:
            pk = uuid.UUID(str(value or "").strip())
        except ValueError:
            return ""
        from .models import Party

        qs = Party.objects.filter(pk=pk, merged_into__isnull=True, erased_at__isnull=True)
        if self.company_id is not None:
            qs = qs.filter(company_id=self.company_id)
        party = qs.only("display_name", "primary_email_normalized").first()
        return party_choice_label(party) if party is not None else ""

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        widget_attrs = context["widget"]["attrs"]
        widget_attrs["data-party-search"] = reverse("erp-party-search")
        if self.kind:
            widget_attrs["data-party-kind"] = self.kind
        if self.company_id is not None:
            widget_attrs["data-party-company"] = str(self.company_id)
        if self.exclude_id is not None:
            widget_attrs["data-party-exclude"] = str(self.exclude_id)
        if self.empty_text:
            widget_attrs["data-party-empty"] = self.empty_text
        label = self.current_label(value)
        if label:
            widget_attrs["data-party-label"] = label
        return context
