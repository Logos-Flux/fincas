"""The fixture adapters (see the package docstring). Copy this file's shape, not its provider."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from datetime import timezone as dt_timezone

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from communications.ingest import record
from communications.participants import Participant, channel_of, match, normalize_address
from core.integrations.ratelimit import RateLimit
from core.models import ExternalRef
from core.plugins.crm_adapter import (
    AdapterError,
    BaseCrmAdapter,
    Capability,
    IngestOutcome,
    PollPage,
    Transient,
    WebhookCheck,
    account_handler,
    envelope,
)
from core.provenance import is_erased_source
from parties.erasure import erased_address

from . import mapping, values

OBJECT_MESSAGE = "message"
OBJECT_CONTACT = "contact"


class _MessageIngest:
    """Inbound messages for both fixture providers: one ``ingest``, any inbound path (rule 3)."""

    def ingest(self, item: dict) -> list[IngestOutcome]:
        payload = self.read_envelope(item)  # rule 2: newer envelopes are refused here
        fields = mapping.message_fields(payload)
        if fields is None:
            return [IngestOutcome(changed=False, skip=True, reason="unsupported_type")]
        company = self.account.company
        external_id = fields["external_id"]
        # Matching (2.4, A7): tombstones first, then exact match-1 only.
        if is_erased_source(company.pk, self.provider_id, OBJECT_MESSAGE, external_id):
            return [IngestOutcome(changed=False, skip=True, reason="erased_source", external_id=external_id)]
        known = False
        for _role, address in fields["participants"]:
            if channel_of(address) == "email" and erased_address(company.pk, address):
                return [IngestOutcome(changed=False, skip=True, reason="erased_address",
                                      external_id=external_id)]
            channel, normalized = normalize_address(address)
            if normalized and match(company.pk, channel, normalized)[0] is not None:
                known = True
        if not known:  # no known party: write nothing, raw included (the Gmail rule)
            return [IngestOutcome(changed=False, skip=True, reason="no_known_party", external_id=external_id)]
        result = record(
            company, "email", direction=fields["direction"], occurred_at=fields["occurred_at"],
            participants=[Participant(role, address) for role, address in fields["participants"]],
            provider=self.provider_id, external_id=external_id, envelope=self.raw_envelope(item),
            known_max_version=self.schema_version, subject=fields["subject"],
            body_text=fields["body_text"], thread_key=fields["thread_key"],
        )
        return [IngestOutcome(changed=result.changed, created=result.created, external_id=external_id)]

    def _page(self, response) -> PollPage:
        body = response.json() or {}
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise AdapterError(f"{self.provider_id}: poll page has no data list — cause: an unexpected "
                               f"response. Fix: check the provider's API version. {values.DOCS}")
        after = body.get("next_after")
        return PollPage(
            items=[envelope(self.provider_id, msg, schema_version=self.schema_version) for msg in data],
            cursor={"after": after} if isinstance(after, str) and after else {},
            more=bool(body.get("has_more")),
        )


class FixtureCrmAdapter(_MessageIngest, BaseCrmAdapter):
    """API key + webhook + poll + an idempotent contact push."""

    provider_id = values.PROVIDER
    schema_version = values.SCHEMA_VERSION
    capabilities = frozenset({Capability.POLL, Capability.WEBHOOK, Capability.PUSH_PROFILES})
    rate_limit = RateLimit(requests=5, per_seconds=1)
    docs_url = values.DOCS
    base_url = values.BASE_URL
    settings_keys = frozenset({"mailbox"})
    credential_kinds = frozenset({"api_key", "webhook_secret"})
    push_dedupe = ("POST /v1/contacts carries Idempotency-Key = golden party id; a create whose answer "
                   "was lost searches GET /v1/contacts?golden_id= before retrying")

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.secret('api_key').get()}"}

    def validate_settings(self, settings: dict) -> list[str]:
        errors = super().validate_settings(settings)
        if isinstance(settings, dict) and "mailbox" in settings and not isinstance(settings["mailbox"], str):
            errors.append("mailbox must be a string — cause: another JSON value. Fix: enter the mailbox id.")
        return errors

    # inbound ----------------------------------------------------------------

    def poll(self, cursor: dict, *, credential=None) -> PollPage:
        params = {"after": cursor.get("after", "")}
        if cursor.get("since"):
            params["since"] = cursor["since"]
        response = self.http("GET", "/v1/messages", headers=self._auth(), params=params)
        return self._page(response)

    def verify_webhook(self, headers, body: bytes) -> WebhookCheck:
        signature = headers.get(values.SIGNATURE_HEADER, "")
        stamp = headers.get(values.TIMESTAMP_HEADER, "")
        if not signature or not stamp.isdigit():
            return WebhookCheck(ok=False, reason="missing_signature")
        secret = self.secret("webhook_secret").get().encode()
        expected = hmac.new(secret, stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return WebhookCheck(ok=False, reason="bad_signature")
        return WebhookCheck(
            ok=True, delivery_id=headers.get(values.DELIVERY_HEADER, ""),
            timestamp=datetime.fromtimestamp(int(stamp), tz=dt_timezone.utc),
        )

    # outbound ---------------------------------------------------------------

    @classmethod
    def outbox_consumers(cls) -> list:
        from core import outbox

        return super().outbox_consumers() + [
            outbox.OutboxConsumer(name="fixture.contact", handle=account_handler(cls, push_contact),
                                  provider=cls.provider_id),
        ]

    def test_connection(self) -> dict:
        from core.integration_checks import result

        response = self.http("GET", "/v1/me", headers=self._auth())
        body = response.json() or {}
        return result(True, "Connected.", http_status=response.status,
                      observed={"workspace": str(body.get("workspace", ""))[:60]})


def push_contact(adapter: FixtureCrmAdapter, event):
    """``fixture.contact``: create or update the party's contact, keyed by ExternalRef (2.5)."""
    from core import outbox
    from core.models import OutboxEvent
    from parties.models import Party

    company = adapter.account.company
    party = Party.objects.filter(company=company, pk=(event.payload or {}).get("object_id")).first()
    if party is None:
        return outbox.Done()  # nothing left to send: the row is gone (or erased away)
    email = party.emails.order_by("created_at").values_list("email", flat=True).first() or ""
    body = mapping.contact_body(party, email)
    content_type = ContentType.objects.get_for_model(Party)
    ref = ExternalRef.objects.filter(company=company, source=adapter.provider_id, object_type=OBJECT_CONTACT,
                                     content_type=content_type, object_id=party.pk).first()
    if ref is not None:
        response = adapter.http("PATCH", f"/v1/contacts/{ref.source_id}", headers=adapter._auth(),
                                json=body, expect=(404,))
        if response.status != 404:
            ExternalRef.objects.filter(pk=ref.pk).update(last_synced_at=timezone.now())
            return outbox.Done()
        ref.delete()  # deleted at the provider: create it again below
    if (event.payload or {}).get("create_unknown"):
        found = adapter.http("GET", "/v1/contacts", headers=adapter._auth(),
                             params={"golden_id": str(party.pk)}).json() or {}
        matches = found.get("data") if isinstance(found, dict) else None
        if matches:
            _write_ref(adapter, party, content_type, matches[0])
            return outbox.Done()
    try:
        response = adapter.http("POST", "/v1/contacts", json=body,
                                headers={**adapter._auth(), "Idempotency-Key": str(party.pk)})
    except Transient:
        # The provider may have written it: the retry searches before creating (rule 4).
        event.payload = {**(event.payload or {}), "create_unknown": True}
        OutboxEvent.objects.filter(pk=event.pk).update(payload=event.payload)
        raise
    _write_ref(adapter, party, content_type, response.json() or {})
    return outbox.Done()


def _write_ref(adapter, party, content_type, contact: dict) -> None:
    contact_id = contact.get("id") if isinstance(contact, dict) else None
    if not isinstance(contact_id, str) or not contact_id:
        raise AdapterError(f"fixture: contact answer has no id — cause: an unexpected response. "
                           f"Fix: check the provider's API version. {values.DOCS}")
    with transaction.atomic():
        ExternalRef.objects.update_or_create(
            company=party.company, source=adapter.provider_id, object_type=OBJECT_CONTACT, source_id=contact_id,
            defaults={"content_type": content_type, "object_id": party.pk, "last_synced_at": timezone.now(),
                      "source_url": f"https://app.fixture.example.com/contacts/{contact_id}"},
        )


class FixtureOAuthAdapter(_MessageIngest, BaseCrmAdapter):
    """Per-user OAuth grants and a poll per grant (Gmail's shape)."""

    provider_id = values.OAUTH_PROVIDER
    schema_version = values.SCHEMA_VERSION
    capabilities = frozenset({Capability.POLL, Capability.OAUTH_USER})
    rate_limit = RateLimit(requests=5, per_seconds=1)
    docs_url = values.DOCS
    base_url = values.BASE_URL
    credential_kinds = frozenset({"oauth_client", "oauth_refresh", "oauth_access"})
    oauth_authorize_url = values.AUTHORIZE_URL
    oauth_token_url = values.TOKEN_URL
    oauth_revoke_url = values.REVOKE_URL
    oauth_scopes = values.SCOPES

    def oauth_subject(self, tokens) -> str:
        response = self.http("GET", "/v1/me", headers={"Authorization": f"Bearer {tokens.access_token}"},
                             pause_on_401=False)  # one user's connect never pauses the account
        email = (response.json() or {}).get("email")
        return email if isinstance(email, str) else ""

    def poll(self, cursor: dict, *, credential=None) -> PollPage:
        if credential is None:
            raise AdapterError(f"fixture_oauth polls per user grant — cause: no grant given. "
                               f"Fix: poll through integrations_poll. {values.DOCS}")
        token = self.access_token(credential.owner)
        params = {"after": cursor.get("after", "")}
        if cursor.get("since"):
            params["since"] = cursor["since"]
        response = self.http("GET", "/v1/mailbox/messages", headers={"Authorization": f"Bearer {token.get()}"},
                             params=params, grant=credential)
        return self._page(response)
