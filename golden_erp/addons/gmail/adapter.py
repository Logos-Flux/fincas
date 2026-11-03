"""The Gmail adapter (docs/crm/phase6-adapters.md 3.3; step 4).

**The rule (A13).** A message is ingested only when at least one participant
in From / To / Cc — other than the mailbox owner and other staff — resolves
to exactly one live party of the account's company. Bcc never qualifies a
message. Everything else (personal mail, newsletters, internal threads) is
never read past its headers and never stored: the poll asks Gmail for the
headers (``format=metadata``) first and fetches the body (``format=full``)
only for a message that passed.

What else drops a message, before anything is written, each counted:

- drafts, spam, trash and chats, always; a label in
  ``settings["exclude_labels"]`` (default promotions); the user's private
  label (``settings["private_label"]``, default ``golden-private``);
- golden's own document email Bcc'd back into a staff mailbox: an
  ``X-Golden-Email-Log`` header naming an ``EmailLog`` this company logged,
  to a recipient the message is addressed to;
- an erased person: any participant address with an erasure suppression
  (``parties.erasure.erased_address``), or an erased ref for the message key
  (``core.provenance.is_erased_source``). These are tombstones: they hold
  across every re-sync;
- a mailbox with no active grant of this account's company (revoked,
  disconnected, or its owner moved company or was deactivated).

**Keys.** ``external_id`` = ``mid:`` + sha256 of the normalised RFC 5322
``Message-ID``: the same email in three staff mailboxes is one
communication. A later copy is the same email only when its Date, From and
Subject equal the stored copy's; a different message reusing the id gets a
key of its own, so a reused Message-ID can never rewrite stored mail.
``thread_key`` = ``mid:`` + sha256 of the thread's root id (first
``References``, else ``In-Reply-To``, else its own). A re-sync of a copy
whose participants are all stored is a no-op; a copy that adds participants
(the sender's carries the Bcc) adds only those
(``communications.ingest.add_participants``). Ingest of one key is
serialised (a Postgres transaction advisory lock), so two mailboxes synced
at once cannot lose each other's Bcc.

**Engagement.** Explicit linkage only (hand-off section 5): ``record`` is
called with ``link_engagement=False``, and with ``engagement=`` when other
communications of the same thread are on exactly one engagement whose party
or contacts include one of this message's known parties. Otherwise the email
is party-only.

**Push.** A Pub/Sub notification is accepted only as a notification
(``webhook_envelope`` keeps the mailbox and history id, nothing else), so a
webhook body can never become mail: its one effect is a poll.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
import weakref
from collections import Counter
from datetime import datetime, timedelta

from django.db import connection, transaction
from django.utils import timezone

from communications.ingest import ERASED_PAYLOAD, add_participants, record
from communications.participants import EMAIL, Participant, match, normalize_address
from core.integrations.ratelimit import RateLimit
from core.plugins.crm_adapter import (
    AdapterError,
    BaseCrmAdapter,
    Capability,
    IngestOutcome,
    PollPage,
    RateLimited,
    Throttled,
    WebhookCheck,
    account_handler,
    envelope,
)
from core.plugins.dispatch import is_suppressed

from . import mapping, pubsub, values

logger = logging.getLogger("golden_erp.integrations.gmail")

OBJECT_MESSAGE = "message"  # communications.ingest.object_type_for("email")

# Drop reasons (IngestOutcome.reason and the per-grant counters).
NO_KNOWN_PARTY = "no_known_party"
EXCLUDED_LABEL = "excluded_label"
GOLDEN_SEND = "golden_send"
ERASED_ADDRESS = "erased_address"
ERASED_SOURCE = "erased_source"
NO_GRANT = "grant_revoked"
DUPLICATE = "duplicate"
UNSUPPORTED = "unsupported"
MALFORMED = "malformed"
SCRUBBED = "erased_communication"

# Postgres advisory-lock namespaces ("GMAI": one message key; "GMAP": one grant's push poll).
LOCK_NAMESPACE = 0x474D4149
POLL_LOCK_NAMESPACE = 0x474D4150

#: Google's JWKS per transport: ``(keys, fetched_at, max_age_s)``.
_JWKS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
JWKS_DEFAULT_MAX_AGE_S = 3600
JWKS_REFETCH_FLOOR_S = 60


class MailboxNotOwned(AdapterError):
    def __init__(self):
        super().__init__(
            "gmail: that mailbox is not yours — cause: the Google account you signed in with is not "
            "your user's email address (or a verified login of yours). Fix: connect with your own "
            f"mailbox; shared mailboxes are not supported. {values.DOCS}"
        )


class GrantOwnerElsewhere(AdapterError):
    code = "grant_owner_elsewhere"

    def __init__(self):
        super().__init__(
            "gmail: this grant's owner is no longer an active user of the account's company — cause: "
            "the user was deactivated or moved. Fix: the user disconnects; a user of this company "
            f"connects their own mailbox. {values.DOCS}"
        )


def _key64(*parts) -> int:
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _lock(company_id, key: str) -> None:
    """Serialise ingest of one message key in one tenant until the transaction ends (Postgres)."""
    if connection.vendor != "postgresql":
        return  # SQLite serialises writers; no second process in tests
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_key64(LOCK_NAMESPACE, company_id, key)])


def _uuid(value: str):
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _scrubbed(comm) -> bool:
    """Erasure replaced this communication's raw payload: never write it again."""
    from communications.models import CommunicationRaw

    payload = CommunicationRaw._base_manager.filter(communication=comm).values_list("payload", flat=True).first()
    return payload == ERASED_PAYLOAD


def _norm(address: str) -> str:
    channel, normalized = normalize_address(address or "")
    return normalized if channel == EMAIL else ""


def _fingerprint(message: dict) -> tuple:
    """What two copies of one email share in every mailbox: Date, From and Subject."""
    senders = sorted(_norm(a) for role, a, _n in mapping.addresses(message) if role == "from")
    return (mapping.header(message, "Date").strip(), tuple(senders), mapping.header(message, "Subject").strip())


class GmailAdapter(BaseCrmAdapter):
    provider_id = values.PROVIDER
    schema_version = values.SCHEMA_VERSION
    capabilities = frozenset({Capability.POLL, Capability.WEBHOOK, Capability.OAUTH_USER, Capability.ERASE})
    # Gmail's per-user quota is 250 units/s (a messages.get costs 5); one
    # bucket per account is shared by every grant, so stay well under it.
    rate_limit = RateLimit(requests=10, per_seconds=1)
    docs_url = values.DOCS
    base_url = values.BASE_URL
    settings_keys = frozenset({"exclude_labels", "private_label", "pubsub_service_account"})
    credential_kinds = frozenset({"oauth_client", "oauth_refresh", "oauth_access", "webhook_secret"})
    oauth_authorize_url = values.AUTHORIZE_URL
    oauth_token_url = values.TOKEN_URL
    oauth_revoke_url = values.REVOKE_URL
    oauth_scopes = values.SCOPES

    def __init__(self, account, transport):
        super().__init__(account, transport)
        self._staff = None
        self._labels: dict = {}  # grant pk -> label ids to skip

    # -- configuration ---------------------------------------------------------

    def validate_settings(self, settings: dict) -> list[str]:
        errors = super().validate_settings(settings)
        if not isinstance(settings, dict):
            return errors
        labels = settings.get("exclude_labels")
        if labels is not None and (not isinstance(labels, list)
                                   or not all(isinstance(x, str) and x.strip() for x in labels)):
            errors.append("exclude_labels is a list of Gmail label ids or names — cause: another JSON "
                          'value. Fix: e.g. ["CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"].')
        private = settings.get("private_label")
        if private is not None and (not isinstance(private, str) or not private.strip()):
            errors.append("private_label is a label name — cause: an empty or non-text value. "
                          f"Fix: enter a name, or remove it (default {values.DEFAULT_PRIVATE_LABEL}).")
        account = settings.get("pubsub_service_account")
        if account is not None and (not isinstance(account, str) or "@" not in account):
            errors.append("pubsub_service_account is the push subscription's service-account email — "
                          "cause: not an address. Fix: enter it, or remove it.")
        return errors

    def _settings(self) -> dict:
        return self.account.settings if isinstance(self.account.settings, dict) else {}

    def exclude_labels(self) -> set[str]:
        """Always drafts, spam, trash and chats; plus the configured list (default: promotions)."""
        labels = self._settings().get("exclude_labels")
        return set(values.ALWAYS_EXCLUDED_LABELS) | set(values.DEFAULT_EXCLUDE_LABELS if labels is None else labels)

    def private_label(self) -> str:
        return (self._settings().get("private_label") or values.DEFAULT_PRIVATE_LABEL).strip()

    # -- grants (OAUTH_USER) ------------------------------------------------------

    def oauth_subject(self, tokens) -> str:
        """The mailbox address of the token (``users.getProfile``)."""
        response = self.http("GET", f"{values.API}/profile", pause_on_401=False,
                             headers={"Authorization": f"Bearer {tokens.access_token}"})
        address = (response.json() or {}).get("emailAddress")
        return address.strip() if isinstance(address, str) else ""

    @staticmethod
    def owner_addresses(owner) -> set[str]:
        """The addresses ``owner`` is known to own: the user's email and verified login emails."""
        from core.identity.models import UserIdentity

        user = owner.user
        found = {_norm(user.email)}
        found |= {_norm(e) for e in UserIdentity.objects.filter(user=user, email_verified=True)
                  .values_list("email", flat=True)}
        found.discard("")
        return found

    def store_grant(self, owner, tokens, *, subject: str):
        """Refuse a mailbox that is not ``owner``'s own, or an owner of another company.

        The code was already exchanged: a refused grant is revoked at Google
        (best effort) so no live token for golden's client is left behind.
        """
        if (owner is None or owner.company_id != self.account.company_id or not subject
                or _norm(subject) not in self.owner_addresses(owner)):
            self._revoke_tokens(tokens)
            raise MailboxNotOwned()
        return super().store_grant(owner, tokens, subject=subject)

    def _revoke_tokens(self, tokens) -> None:
        for token in (getattr(tokens, "refresh_token", ""), getattr(tokens, "access_token", "")):
            if not token:
                continue
            self._register(token, "oauth_refresh")
            try:
                self.http("POST", self.oauth_revoke_url, data={"token": token}, expect=(400,),
                          pause_on_401=False, rate_limited=False)
            except AdapterError:
                logger.warning("[integrations] gmail: revoking a refused grant failed: account=%s",
                               self.account.pk)
            return  # revoking the refresh token revokes the grant

    def grant_for_mailbox(self, address: str):
        """The active grant (refresh credential) of this account for ``address``, or None.

        Only a grant whose owner is an active user of the account's company
        counts: a grant never ingests into another tenant.
        """
        from core.models import IntegrationCredential

        wanted = _norm(address)
        if not wanted:
            return None
        grants = IntegrationCredential.objects.filter(
            account=self.account, kind=IntegrationCredential.Kind.OAUTH_REFRESH, owner__isnull=False,
            revoked_at__isnull=True, owner__company_id=self.account.company_id, owner__user__is_active=True,
        ).select_related("owner", "owner__user").order_by("created_at", "pk")
        return next((g for g in grants if _norm(g.subject) == wanted), None)

    def grant_status(self, owner) -> dict:
        """What staff see for ``owner``'s grant: ``{"state", "mailbox", "paused_until"}``.

        ``state``: ``active``, ``paused``, ``reconnect`` (revoked by the
        provider or the user: ingestion has stopped), or ``none``.
        """
        from core.integrations.ratelimit import paused_for
        from core.models import IntegrationCredential

        latest = IntegrationCredential.objects.filter(
            account=self.account, owner=owner, kind=IntegrationCredential.Kind.OAUTH_REFRESH,
        ).order_by("-created_at").first()
        if latest is None:
            return {"state": "none", "mailbox": "", "paused_until": None}
        if latest.revoked_at is not None:
            return {"state": "reconnect", "mailbox": latest.subject, "paused_until": None}
        if paused_for(latest) > 0:
            return {"state": "paused", "mailbox": latest.subject, "paused_until": latest.paused_until}
        return {"state": "active", "mailbox": latest.subject, "paused_until": None}

    def _check_owner(self, grant) -> None:
        owner = grant.owner
        if owner is None or owner.company_id != self.account.company_id or not owner.user.is_active:
            raise GrantOwnerElsewhere()

    # -- inbound: poll ------------------------------------------------------------

    def _auth(self, token) -> dict:
        return {"Authorization": f"Bearer {token.get()}"}

    def poll(self, cursor: dict, *, credential=None) -> PollPage:
        """One step of one user's mailbox: headers first, bodies only for mail that passes.

        Cursor: ``{"history_id", "synced_at"}`` once listed; while listing
        (first sync, or a resync after Gmail expired the history id)
        ``{"since", "start_history_id", "page_token"}``. Ids read from a
        page but not yet processed wait in ``pending`` (with ``pending_more``,
        whether the page had a next one): when the rate limit runs out
        mid-page, the work done is kept and the next step resumes there.
        ``stats`` carries running counts by outcome, never an address.
        """
        if credential is None:
            raise AdapterError(f"gmail polls per user grant — cause: no grant given. "
                               f"Fix: poll through integrations_poll. {values.DOCS}")
        self._check_owner(credential)
        token = self.access_token(credential.owner)
        auth = self._auth(token)
        stats = Counter(cursor.get("stats") or {})
        position = {k: v for k, v in cursor.items() if k not in ("stats", "pending", "pending_more")}
        pending = [g for g in cursor.get("pending") or [] if isinstance(g, str)]
        if pending:
            more_after = bool(cursor.get("pending_more"))
        elif position.get("history_id") and not position.get("since"):
            pending, position, more_after = self._history(position, auth, credential)
        else:
            pending, position, more_after = self._list(position, auth, credential)
        items, rest = self._fetch(pending, auth, credential, stats)
        after = dict(position)
        if rest:
            after.update(pending=rest, pending_more=more_after)
        after["stats"] = dict(sorted(stats.items()))
        logger.info("[integrations] gmail poll: account=%s grant=%s ids=%s fetched=%s waiting=%s",
                    self.account.pk, credential.pk, len(pending), len(items), len(rest))
        return PollPage(items=items, cursor=after, more=bool(rest) or more_after)

    def _resync_since(self, cursor) -> str:
        """After a history gap: back to the last successful history read, at least 7 days, at most initial_since."""
        now = timezone.now()
        since = now - timedelta(days=values.RESYNC_DAYS)
        try:
            synced = datetime.fromisoformat(str(cursor.get("synced_at") or ""))
            since = min(since, synced - timedelta(days=1))
        except ValueError:
            pass
        floor = now - timedelta(days=int(self._settings().get("initial_since") or values.INITIAL_SINCE_DAYS))
        return max(since, floor).isoformat()

    def _history(self, cursor, auth, grant):
        params = {"startHistoryId": str(cursor["history_id"]), "historyTypes": "messageAdded",
                  "maxResults": str(values.PAGE_SIZE)}
        if cursor.get("page_token"):
            params["pageToken"] = cursor["page_token"]
        response = self.http("GET", f"{values.API}/history", headers=auth, params=params, expect=(404,),
                             grant=grant)
        if response.status == 404:
            # Gmail keeps about a week of history: list the gap again.
            return [], {"since": self._resync_since(cursor)}, True
        body = response.json() or {}
        ids = []
        for entry in body.get("history") or []:
            for added in (entry.get("messagesAdded") or []) if isinstance(entry, dict) else []:
                gid = (added.get("message") or {}).get("id") if isinstance(added, dict) else None
                if isinstance(gid, str) and gid and gid not in ids:
                    ids.append(gid)
        token = body.get("nextPageToken")
        synced_at = timezone.now().isoformat()
        if isinstance(token, str) and token:
            return ids, {"history_id": cursor["history_id"], "page_token": token,
                         "synced_at": cursor.get("synced_at", synced_at)}, True
        latest = body.get("historyId")
        return ids, {"history_id": str(latest) if latest else str(cursor["history_id"]),
                     "synced_at": synced_at}, False

    def _list(self, cursor, auth, grant):
        start = cursor.get("start_history_id")
        if not start:
            profile = self.http("GET", f"{values.API}/profile", headers=auth, grant=grant).json() or {}
            start = str(profile.get("historyId") or "")
            if not start.isdigit():
                raise AdapterError(f"gmail: the profile has no history id — cause: an unexpected "
                                   f"response. Fix: check the API version. {values.DOCS}")
        default = (timezone.now() - timedelta(days=values.INITIAL_SINCE_DAYS)).isoformat()
        since = cursor.get("since") or default
        try:
            after_epoch = int(datetime.fromisoformat(since).timestamp())
        except (TypeError, ValueError):
            after_epoch = int(datetime.fromisoformat(default).timestamp())
        params = {"q": f"after:{after_epoch}", "maxResults": str(values.PAGE_SIZE)}
        if cursor.get("page_token"):
            params["pageToken"] = cursor["page_token"]
        body = self.http("GET", f"{values.API}/messages", headers=auth, params=params, grant=grant).json() or {}
        ids = [m["id"] for m in body.get("messages") or []
               if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]]
        token = body.get("nextPageToken")
        if isinstance(token, str) and token:
            return ids, {"since": since, "start_history_id": start, "page_token": token}, True
        return ids, {"history_id": start, "synced_at": timezone.now().isoformat()}, False

    def _skip_labels(self, auth, grant) -> list[str]:
        """Label ids never read: the exclusions (ids, or user labels by name) and the private label."""
        if grant.pk in self._labels:
            return self._labels[grant.pk]
        body = self.http("GET", f"{values.API}/labels", headers=auth, grant=grant).json() or {}
        names = {n.lower() for n in self.exclude_labels()} | {self.private_label().lower()}
        skip = set(self.exclude_labels())
        for label in body.get("labels") or []:
            if isinstance(label, dict) and str(label.get("name", "")).lower() in names and label.get("id"):
                skip.add(str(label["id"]))
        self._labels[grant.pk] = sorted(skip)
        return self._labels[grant.pk]

    def _fetch(self, ids, auth, grant, stats: Counter) -> tuple[list[dict], list[str]]:
        """``(envelopes, ids not reached)``. A spent rate limit after some progress keeps that progress."""
        if not ids:
            return [], []
        items, done = [], 0
        try:
            skip = self._skip_labels(auth, grant)
            for gid in ids:
                path = f"{values.API}/messages/{gid}"
                meta = self.http("GET", path, headers=auth, params={"format": "metadata"}, expect=(404,),
                                 grant=grant)
                if meta.status == 404:
                    stats["gone"] += 1  # deleted between the list and the read
                    done += 1
                    continue
                message = meta.json() or {}
                try:
                    reason = self.screen(message, grant.subject, skip_labels=skip)
                    if reason is None and self._is_known_copy(message, grant.subject):
                        reason = DUPLICATE
                except AdapterError:
                    reason = MALFORMED
                if reason is None:
                    full = self.http("GET", path, headers=auth, params={"format": "full"}, expect=(404,),
                                     grant=grant)
                    if full.status == 404:
                        reason = "gone"
                    else:
                        items.append(envelope(self.provider_id, {"mailbox": grant.subject, "skip_label_ids": skip,
                                                                 "message": full.json() or {}},
                                              schema_version=self.schema_version))
                        reason = "fetched"
                stats[reason] += 1
                done += 1
        except (RateLimited, Throttled):
            if done == 0:
                raise
        return items, list(ids[done:])

    # -- the filter -----------------------------------------------------------------

    def staff_addresses(self) -> dict[str, list[str]]:
        """Active staff of the company by normalised email -> profile ids (the importer's owner rule)."""
        if self._staff is None:
            from crm_import.match import OwnerIndex

            index = OwnerIndex(self.account.company)
            self._staff = {email: [h["profile"] for h in hits] for email, hits in index.by_email.items() if email}
        return self._staff

    def known_parties(self, message: dict, mailbox: str) -> set:
        """Party ids of the external participants in From / To / Cc that match exactly one party."""
        from parties.models import Party

        company_id = self.account.company_id
        staff = set(self.staff_addresses()) | {_norm(mailbox)}
        found = set()
        for role, address, _name in mapping.addresses(message):
            normalized = _norm(address)
            if role not in mapping.QUALIFYING_ROLES or not normalized or normalized in staff:
                continue
            party_id, _point = match(company_id, EMAIL, normalized)
            if party_id is not None:
                found.add(party_id)
        if not found:
            return found
        internal = set(Party._base_manager.filter(pk__in=found).filter(is_self=True).values_list("pk", flat=True))
        internal |= set(Party._base_manager.filter(pk__in=found, user__is_staff=True).values_list("pk", flat=True))
        return found - internal

    def _is_golden_send(self, message: dict) -> bool:
        """Golden's own document email: the header names an EmailLog this company logged, sent to
        someone this message is addressed to. An id copied into other mail (every recipient sees it)
        hides nothing: the log's recipient would have to be among its To / Cc."""
        from communications.models import Communication

        log_id = _uuid(mapping.golden_log_id(message))
        if log_id is None:
            return False
        recipient = Communication._base_manager.filter(
            email_log_id=log_id, company_id=self.account.company_id,
        ).values_list("email_log__recipient_email", flat=True).first()
        if not recipient:
            return False
        addressed = {_norm(a) for role, a, _n in mapping.addresses(message) if role in ("to", "cc")}
        return _norm(recipient) in addressed

    def screen(self, message: dict, mailbox: str, *, skip_labels=()) -> str | None:
        """Why ``message`` must not be ingested (a reason code), or None. Reads headers only."""
        from core.provenance import is_erased_source
        from parties.erasure import erased_address

        if mapping.label_ids(message) & (self.exclude_labels() | set(skip_labels)):
            return EXCLUDED_LABEL
        if self._is_golden_send(message):
            return GOLDEN_SEND
        company_id = self.account.company_id
        for _role, address, _name in mapping.addresses(message):
            if erased_address(company_id, address):
                return ERASED_ADDRESS
        primary = mapping.external_id(message, mailbox)
        for key in {primary, self._alternate_key(message, primary)}:
            if is_erased_source(company_id, self.provider_id, OBJECT_MESSAGE, key):
                return ERASED_SOURCE
        if not self.known_parties(message, mailbox):
            return NO_KNOWN_PARTY
        return None

    def _existing(self, key: str):
        from communications.models import Communication
        from core.models import ExternalRef

        ref = ExternalRef._base_manager.filter(
            company_id=self.account.company_id, source=self.provider_id, object_type=OBJECT_MESSAGE, source_id=key,
        ).first()
        if ref is None:
            return None
        return Communication._base_manager.filter(pk=ref.object_id, company_id=self.account.company_id).first()

    @staticmethod
    def _alternate_key(message: dict, primary: str) -> str:
        """The key of a *different* message that reuses a stored Message-ID."""
        date, senders, subject = _fingerprint(message)
        digest = hashlib.sha256("\n".join([primary, date, ",".join(senders), subject]).encode()).hexdigest()
        return f"{primary}:{digest[:16]}"

    def _same_email(self, comm, message: dict) -> bool:
        from communications.models import CommunicationRaw

        stored = CommunicationRaw._base_manager.filter(communication=comm).values_list("payload", flat=True).first()
        if not isinstance(stored, dict) or stored == ERASED_PAYLOAD:
            return True  # nothing to compare with; a scrubbed row is never rewritten anyway
        return _fingerprint(stored) == _fingerprint(message)

    def resolve(self, message: dict, mailbox: str):
        """``(key, existing communication or None)`` for this copy of a message."""
        key = mapping.external_id(message, mailbox)
        existing = self._existing(key)
        if existing is not None and not self._same_email(existing, message):
            key = self._alternate_key(message, key)
            existing = self._existing(key)
        return key, existing

    @staticmethod
    def _keys(rows) -> set:
        return {(role, _norm(address)) for role, address, _name in rows if _norm(address)}

    def _stored_keys(self, comm) -> set:
        return {(r, a) for r, a in comm.participants.values_list("role", "address_normalized") if a}

    def _is_known_copy(self, message: dict, mailbox: str) -> bool:
        """Already ingested, and this copy adds no participant: no body read, no write."""
        _key, comm = self.resolve(message, mailbox)
        if comm is None:
            return False
        return _scrubbed(comm) or self._keys(mapping.addresses(message)) <= self._stored_keys(comm)

    # -- inbound: ingest (every path) -------------------------------------------------

    def ingest(self, item: dict) -> list[IngestOutcome]:
        payload = self.read_envelope(item)  # rule 2: a newer envelope is refused here
        if not isinstance(payload, dict):
            return [IngestOutcome(changed=False, skip=True, reason=UNSUPPORTED)]
        if "push" in payload:
            return [self._ingest_notification(payload["push"])]
        message, mailbox = payload.get("message"), payload.get("mailbox")
        if not isinstance(message, dict) or not isinstance(mailbox, str):
            return [IngestOutcome(changed=False, skip=True, reason=UNSUPPORTED)]
        try:
            with transaction.atomic():
                return [self._ingest_message(message, mailbox, payload.get("skip_label_ids") or [])]
        except AdapterError:
            # A resource without a date or any id: permanent, the cursor may pass it.
            return [IngestOutcome(changed=False, skip=True, reason=MALFORMED)]

    def _ingest_message(self, message: dict, mailbox: str, skip_labels) -> IngestOutcome:
        grant = self.grant_for_mailbox(mailbox)
        if grant is None:
            return IngestOutcome(changed=False, skip=True, reason=NO_GRANT)
        _lock(self.account.company_id, mapping.external_id(message, mailbox))
        reason = self.screen(message, mailbox, skip_labels=skip_labels)
        if reason is not None:
            return IngestOutcome(changed=False, skip=True, reason=reason)
        key, existing = self.resolve(message, mailbox)
        rows = mapping.addresses(message)
        participants = [Participant(role, address, name) for role, address, name in rows]
        if existing is not None:
            if _scrubbed(existing):
                return IngestOutcome(changed=False, reason=SCRUBBED, external_id=key)
            if self._keys(rows) <= self._stored_keys(existing):
                return IngestOutcome(changed=False, reason=DUPLICATE, external_id=key)
            added = add_participants(existing, participants)  # only who took part; nothing else changes
            return IngestOutcome(changed=bool(added), external_id=key)
        staff = self.staff_addresses()
        sender = next((_norm(a) for role, a, _n in rows if role == "from"), "")
        outbound = bool(sender) and (sender == _norm(mailbox) or sender in staff)
        performed_by = None
        if outbound:
            if sender == _norm(mailbox):
                performed_by = grant.owner
            elif len(staff.get(sender, [])) == 1:
                from core.models import UserProfile

                performed_by = UserProfile.objects.filter(pk=staff[sender][0],
                                                          company_id=self.account.company_id).first()
        thread = mapping.thread_key(message, mailbox)
        engagement = self._thread_engagement(thread, self.known_parties(message, mailbox))
        attachments = mapping.attachments(message)
        result = record(
            self.account.company, "email",
            direction="outbound" if outbound else "inbound",
            occurred_at=mapping.occurred_at(message),
            participants=participants,
            provider=self.provider_id, external_id=key,
            envelope=envelope(self.provider_id, mapping.raw_message(message), schema_version=self.schema_version),
            known_max_version=self.schema_version,
            subject=mapping.header(message, "Subject"), body_text=mapping.body_text(message),
            thread_key=thread, engagement=engagement, link_engagement=False, performed_by=performed_by,
            metadata={"gmail": {"attachments": len(attachments)}} if attachments else None,
        )
        comm = result.communication
        if result.changed and is_suppressed() and comm.engagement_id is not None:
            # First sync runs inside suppressed(): record does not bump; the adapter does (2.4).
            from crm.engagements import touch_activity

            if comm.status == comm.Status.COMPLETED and comm.occurred_at <= timezone.now():
                touch_activity(comm.engagement_id, comm.occurred_at)
        return IngestOutcome(changed=result.changed, created=result.created, external_id=key)

    def _thread_engagement(self, thread: str, parties: set):
        """The engagement the thread is already on, when exactly one and it concerns one of this
        message's known parties (a forged References header cannot reach another deal); else None."""
        from django.db.models import Q

        from communications.models import Communication
        from crm.models import Engagement

        if not thread or not parties:
            return None
        ids = list(Communication._base_manager.filter(
            company_id=self.account.company_id, thread_key=thread, engagement__isnull=False,
        ).order_by().values_list("engagement_id", flat=True).distinct()[:2])  # no ordering column in DISTINCT
        if len(ids) != 1:
            return None
        return Engagement._base_manager.filter(
            Q(party_id__in=parties) | Q(contacts__party_id__in=parties),
            pk=ids[0], company_id=self.account.company_id,
        ).distinct().first()

    # -- inbound: Pub/Sub push --------------------------------------------------------

    def _jwks(self, kid: str) -> dict:
        """Google's signing keys, cached per transport for their max-age; refetched (at most once a
        minute) when a token names a key the cache does not hold. Fetched outside ``http()``, so
        Google's answer can never pause this account; any failure is a refusal."""
        now = timezone.now()
        cached = _JWKS.get(self.transport)
        if cached is not None:
            keys, fetched_at, max_age = cached
            known = any(isinstance(k, dict) and k.get("kid") == kid for k in keys.get("keys") or [])
            age = (now - fetched_at).total_seconds()
            if age < max_age and (known or age < JWKS_REFETCH_FLOOR_S):
                return keys
        try:
            response = self.transport.request("GET", values.GOOGLE_CERTS_URL, headers={}, timeout=5.0)
            keys = response.json() if response.ok else None
        except Exception:  # noqa: BLE001 — a transport or JSON failure refuses the push
            keys = None
        if not isinstance(keys, dict):
            raise pubsub.TokenRefused("jwks_unavailable")
        _JWKS[self.transport] = (keys, now, pubsub.max_age(response.header("cache-control"),
                                                            JWKS_DEFAULT_MAX_AGE_S))
        return keys

    def verify_webhook(self, headers, body: bytes) -> WebhookCheck:
        """A Pub/Sub push: a notification body and a Google-signed token for this account's audience,
        from the configured service account (required: without it any Google identity could mint one)."""
        service_account = str(self._settings().get("pubsub_service_account") or "")
        if not service_account:
            return WebhookCheck(ok=False, reason="service_account_unset")
        try:
            parsed = pubsub.notification(json.loads(body.decode("utf-8")))
        except ValueError:
            parsed = None
        if parsed is None:
            return WebhookCheck(ok=False, reason="not_a_notification")
        token = pubsub.bearer(headers)
        if not token:
            return WebhookCheck(ok=False, reason="missing_signature")
        try:
            head = pubsub.unverified_header(token)
            audience = self.secret("webhook_secret").get()
            pubsub.verify(token, self._jwks(head["kid"]), audience=audience, now=timezone.now(),
                          service_account=service_account)
        except pubsub.TokenRefused as exc:
            return WebhookCheck(ok=False, reason=exc.reason)
        return WebhookCheck(ok=True, delivery_id=parsed[0])

    def webhook_envelope(self, headers, body: bytes) -> dict:
        """Only the notification is kept — mailbox and history id — so a body can never become mail."""
        parsed = pubsub.notification(json.loads(body.decode("utf-8")))
        if parsed is None:
            raise ValueError("not a Pub/Sub notification")
        _mid, address, history = parsed
        return envelope(self.provider_id, {"push": {"mailbox": address, "history_id": history}},
                        schema_version=self.schema_version)

    def _ingest_notification(self, push) -> IngestOutcome:
        """A push names a mailbox and a history id, no mail: enqueue a poll of that grant."""
        from core import outbox

        if not isinstance(push, dict) or not isinstance(push.get("mailbox"), str) \
                or not str(push.get("history_id") or "").isdigit():
            return IngestOutcome(changed=False, skip=True, reason=UNSUPPORTED)
        grant = self.grant_for_mailbox(push["mailbox"])
        if grant is None:
            return IngestOutcome(changed=False, skip=True, reason=NO_GRANT)
        if not (grant.sync_state or {}).get("initial_sync_done"):
            return IngestOutcome(changed=False, skip=True, reason="initial_sync_pending")
        row = outbox.enqueue(f"{self.provider_id}.poll", outbox.EventSpec(
            idempotency_key=f"{self.account.pk}:{grant.owner_id}:{push['history_id']}",
            event="poll",
            # One pending poll per grant: a burst of pushes is one poll.
            coalesce_key=f"grant:{grant.owner_id}",
            payload={"mailbox": grant.subject}, company_id=self.account.company_id, occurred_at=timezone.now(),
        ))
        return IngestOutcome(changed=bool(row), created=bool(row))

    # -- outbox ------------------------------------------------------------------------

    @classmethod
    def outbox_consumers(cls) -> list:
        from core import outbox

        return super().outbox_consumers() + [
            outbox.OutboxConsumer(name=f"{cls.provider_id}.poll", handle=account_handler(cls, poll_grant),
                                  provider=cls.provider_id),
        ]

    # -- erasure -------------------------------------------------------------------------

    def erasure_refs(self, footprint) -> list:
        """Nothing: golden writes nothing into Gmail (3.3). Erasure scrubs the
        communications and raw payloads; the hashed keys name no one, and the
        erasure suppression stops the person's mail from being ingested again."""
        return []

    def erase(self, refs, *, done=None):
        from core import outbox

        return outbox.Done()


class _GrantPollLock:
    """At most one push-driven poll of a grant at a time (Postgres advisory lock; a no-op elsewhere)."""

    def __init__(self, account_id, owner_id):
        self.key = _key64(POLL_LOCK_NAMESPACE, account_id, owner_id)
        self.held = False

    def __enter__(self) -> bool:
        if connection.vendor != "postgresql":
            self.held = True
            return True
        self.xact = connection.in_atomic_block
        fn = "pg_try_advisory_xact_lock" if self.xact else "pg_try_advisory_lock"
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT {fn}(%s)", [self.key])
            self.held = bool(cursor.fetchone()[0])
        return self.held

    def __exit__(self, *exc):
        if self.held and connection.vendor == "postgresql" and not self.xact:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [self.key])
        return False


def poll_grant(adapter: GmailAdapter, event):
    """``gmail.poll``: poll the one grant a push notification named, until its history is read.

    The same loop as ``integrations_poll`` for one grant (each item in its own
    transaction inside ``origin("gmail")``, the cursor saved after each
    step), live only: a grant whose first sync has not finished is left to
    the scheduled poll, which runs that sync inside ``suppressed()``. A
    second push poll of the same grant while one runs is a no-op.
    """
    from core import outbox
    from core.integrations.ratelimit import paused_for
    from core.models import IntegrationCredential

    grant = adapter.grant_for_mailbox((event.payload or {}).get("mailbox", ""))
    if grant is None or paused_for(grant) > 0:
        return outbox.Done()  # disconnected, revoked or paused: the scheduled poll resumes it
    with _GrantPollLock(adapter.account.pk, grant.owner_id) as held:
        if not held:
            return outbox.Done()
        grant.refresh_from_db(fields=["sync_state"])
        state = dict(grant.sync_state or {})
        if not state.get("initial_sync_done"):
            return outbox.Done()
        cursor = dict(state.get("cursor") or {})
        for _step in range(40):
            page = adapter.poll(cursor, credential=grant)
            for item in page.items:
                try:
                    with outbox.origin(adapter.provider_id), transaction.atomic():
                        adapter.ingest(item)
                except Exception as exc:  # noqa: BLE001 — the cursor does not pass a failed item
                    logger.warning("[integrations] gmail poll item failed: account=%s error=%s",
                                   adapter.account.pk, type(exc).__name__)
                    return outbox.Retry(error=f"ingest_{type(exc).__name__}"[:64])
            cursor = dict(page.cursor or {})
            state["cursor"] = cursor
            IntegrationCredential._base_manager.filter(
                account=adapter.account, owner_id=grant.owner_id, kind=IntegrationCredential.Kind.OAUTH_REFRESH,
                revoked_at__isnull=True,
            ).update(sync_state=state)
            if not page.more:
                break
    return outbox.Done()
