"""Invented Gmail message resources and a seeded tenant for the behaviour tests."""

from __future__ import annotations

import base64
from datetime import datetime
from datetime import timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from core.integrations import accounts as services
from core.models import Company, IntegrationAccount, UserProfile
from core.plugins.crm_adapter import envelope
from core.testing.integration_fixtures import fixture_secret
from parties.models import Party, PartyEmail

PLUGINS = ["golden_erp.addons.gmail.plugin.GmailPlugin"]


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def message(gid="19b00000000000f1", *, frm="ada@example.com", to=("rep@example.org",), cc=(), bcc=(),
            subject="Hello", mid="<CAF-test-1@mail.example.com>", refs="", irt="", labels=("INBOX",),
            text="Hello there", html=None, at="2026-03-02T14:00:00+00:00", extra=()):
    headers = [{"name": "From", "value": frm}]
    for name, values in (("To", to), ("Cc", cc), ("Bcc", bcc)):
        if values:
            headers.append({"name": name, "value": ", ".join(values)})
    headers.append({"name": "Subject", "value": subject})
    if mid:
        headers.append({"name": "Message-ID", "value": mid})
    if irt:
        headers.append({"name": "In-Reply-To", "value": irt})
    if refs:
        headers.append({"name": "References", "value": refs})
    headers += [{"name": k, "value": v} for k, v in extra]
    parts = []
    if text is not None:
        parts.append({"mimeType": "text/plain", "filename": "", "body": {"size": len(text), "data": b64(text)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "filename": "", "body": {"size": len(html), "data": b64(html)}})
    millis = int(datetime.fromisoformat(at).astimezone(dt_timezone.utc).timestamp() * 1000)
    return {"id": gid, "threadId": gid, "labelIds": list(labels), "internalDate": str(millis), "historyId": "9",
            "payload": {"mimeType": "multipart/alternative", "headers": headers, "parts": parts}}


def item(msg, mailbox="rep@example.org", **extra):
    return envelope("gmail", {"mailbox": mailbox, "message": msg, **extra})


class GmailSeed:
    """A tenant with staff rep/boss, parties Ada and Grace, and an enabled account with rep's grant."""

    @classmethod
    def seed_tenant(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other = Company.objects.create(name="Tenant B")
        cls.rep = cls.staff("rep", "rep@example.org")
        cls.boss = cls.staff("boss", "boss@example.org")
        cls.ada = cls.person("Ada", "Lovelace", "ada@example.com")
        cls.grace = cls.person("Grace", "Hopper", "grace@example.net")
        cls.account = IntegrationAccount.objects.create(company=cls.company, provider="gmail", enabled=True,
                                                        settings={})
        for kind in ("oauth_client", "webhook_secret"):
            services.set_credential(cls.account, kind, fixture_secret(kind))
        cls.grant(cls.account, cls.rep, "rep@example.org")

    @classmethod
    def staff(cls, username, email, company=None):
        user = get_user_model().objects.create_user(username, email=email, password="x", is_staff=True)
        return UserProfile.objects.create(user=user, company=company or cls.company, display_name=username)

    @classmethod
    def person(cls, given, family, email, company=None):
        party = Party.objects.create(company=company or cls.company, kind=Party.Kind.PERSON, given_name=given,
                                     family_name=family, origin=Party.Origin.ERP)
        PartyEmail.objects.create(party=party, email=email, purpose="work")
        return party

    @staticmethod
    def grant(account, owner, subject, *, initial_sync_done=True):
        from datetime import timedelta

        from django.utils import timezone

        from core.models import IntegrationCredential

        services.set_credential(account, "oauth_refresh", fixture_secret("oauth_refresh"), owner=owner,
                                subject=subject)
        services.set_credential(account, "oauth_access", fixture_secret("oauth_access"), owner=owner,
                                subject=subject, expires_at=timezone.now() + timedelta(hours=1))
        IntegrationCredential.objects.filter(account=account, owner=owner).update(
            sync_state={"cursor": {"history_id": "1000"}, "initial_sync_done": initial_sync_done})
        return IntegrationCredential.objects.get(account=account, owner=owner, kind="oauth_refresh",
                                                 revoked_at=None)

    def adapter(self, account=None):
        from core.testing.integration_fixtures import FixtureTransport

        from ..adapter import GmailAdapter

        return GmailAdapter(account or self.account, FixtureTransport())

    def ingest(self, msg, *, mailbox="rep@example.org", adapter=None, **extra):
        from django.db import transaction

        from core import outbox

        adapter = adapter or self.adapter()
        with outbox.origin("gmail"), transaction.atomic():
            return adapter.ingest(item(msg, mailbox, **extra))[0]


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class GmailTestCase(GmailSeed, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.seed_tenant()
