"""Portal login resolver: verified claims → ``UserIdentity`` → ``User`` → ``Party`` → ``Customer``.

Identity unification §3.4 (step 4), with the refusal states and codes of the
Party spec's portal-auth table (``docs/crm/phase2-party.md`` §2.6). Shared
by every portal-capable provider; the provider has already verified the
token (signature, ``exp``, ``aud``, ``iss``, leeway) before this runs.

Given provider P, subject S and email E:

1. ``UserIdentity(P, S)`` exists → its user U → U's person party in the
   portal company (following merges) → the Customer it holds. **linked**,
   or **closed** (U or the Customer inactive, or U has no Customer there),
   or **collision** (U is staff/superuser and P is not ``trusted``: D5).
2. Otherwise the candidate parties for E are looked up
   (:func:`find_candidates`: the matcher, ``parties.match.find`` over the
   portal company's live person parties, oldest first) and:

   ========== ========================================= ====================
   state      condition                                 result
   ========== ========================================= ====================
   match-0    no candidate, E confirmed                 create U + Customer
                                                        + identity + roles
                                                        (``self_register``,
                                                        per-IP throttle)
   unverified E not verified, or P does not confirm it  401 portal_verify_email
   match-N    ≥2 candidates, E verified                 409 portal_account_ambiguous
   collision  1 candidate, but its user is privileged   403 portal_account_collision
              and P untrusted, or it holds a live
              confirmed subject of P, or E is not
              ASCII, or P may not link by email
   match-1    1 candidate, E confirmed                  link (create U if
                                                        none; create an
                                                        individual Customer
                                                        if the party holds
                                                        none)
   ========== ========================================= ====================

"Confirmed" means the provider's own record (:func:`_require_confirmed`),
never the token: Supabase keeps ``email_verified`` in user-writable
``user_metadata``. A provider that cannot be asked refuses (fail closed).
A subject the Customer's user already holds for P is replaced only when P
reports it gone or never confirmed (re-checked under the row lock).
Nothing is ever picked among several candidates.

Phase 2 step 7 re-pointed the lookup at Party (``parties.portal``): it
replaced :func:`find_candidates` and :func:`customer_for_user`, and the
match-1 link now locks the candidate party and the account it holds. Every
branch, state and code is the step-4 one.

The identity table is the only link. The step-4 dual-write of
``Customer.supabase_user_id`` ended with identity step 8, which dropped the
column (``customers.0009_remove_customer_supabase_user_id``).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import exceptions, status

from . import GENERATED_USERNAME_PREFIX
from .models import UserIdentity

logger = logging.getLogger(__name__)

# Refusal codes: part of the consumer contract (Party spec §2.6 and §9). The
# storefront maps codes to screens, never message text.
CODE_VERIFY_EMAIL = "portal_verify_email"
CODE_AMBIGUOUS = "portal_account_ambiguous"
CODE_COLLISION = "portal_account_collision"
CODE_CLOSED = "portal_account_closed"
CODE_NO_ACCOUNT = "portal_no_account"
CODE_NOT_CONFIGURED = "portal_not_configured"
CODE_MERGE_CYCLE = "portal_merge_cycle"

LAST_LOGIN_RESOLUTION = timedelta(minutes=5)


class PortalAuthFailed(exceptions.AuthenticationFailed):
    """401 with a stable ``code`` in the body."""

    def __init__(self, message: str, code: str):
        super().__init__({"detail": message, "code": code}, code=code)


class PortalRefused(exceptions.APIException):
    """403/409 with a stable ``code`` in the body. Nothing was linked."""

    def __init__(self, message: str, code: str, status_code: int):
        self.status_code = status_code
        super().__init__({"detail": message, "code": code}, code=code)


def _verify_email() -> PortalAuthFailed:
    return PortalAuthFailed(
        "Please verify your email address to access your account", CODE_VERIFY_EMAIL,
    )


def _collision() -> PortalRefused:
    return PortalRefused(
        "This email belongs to an account that signs in another way. Sign in with the "
        "method you used before, or contact us.",
        CODE_COLLISION, status.HTTP_403_FORBIDDEN,
    )


def _closed() -> PortalRefused:
    return PortalRefused(
        "This account is closed. Contact us if you think that's a mistake.",
        CODE_CLOSED, status.HTTP_403_FORBIDDEN,
    )


def _ambiguous() -> PortalRefused:
    return PortalRefused(
        "We found more than one account for this email and need to tidy them up before "
        "you can sign in. Our team has been told.",
        CODE_AMBIGUOUS, status.HTTP_409_CONFLICT,
    )


def _not_configured() -> PortalRefused:
    logger.error(
        "Portal auth: no portal company (set GOLDEN_PORTAL_COMPANY; system check parties.E001)"
    )
    return PortalRefused(
        "Sign-in is not available right now.", CODE_NOT_CONFIGURED,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    )


# ── claims ──────────────────────────────────────────────────────────────────


def email_is_verified(claims: dict) -> bool:
    """True only for an explicit boolean ``email_verified: true``.

    Supabase (GoTrue) puts it in ``user_metadata``; some token shapes and
    OIDC carry it top-level. Anything short of ``True`` is unverified.
    """
    metadata = claims.get("user_metadata") or {}
    return metadata.get("email_verified") is True or claims.get("email_verified") is True


def names_from_claims(claims: dict, email: str) -> tuple[str, str]:
    """Best-effort first/last name; falls back to the email local part."""
    metadata = claims.get("user_metadata") or {}
    first = str(metadata.get("first_name") or claims.get("given_name") or "").strip()
    last = str(metadata.get("last_name") or claims.get("family_name") or "").strip()
    if not first:
        full = str(metadata.get("full_name") or metadata.get("name") or claims.get("name") or "")
        full = full.strip()
        if full:
            parts = full.split(None, 1)
            first = parts[0]
            if not last and len(parts) > 1:
                last = parts[1]
    if not first:
        first = email.split("@", 1)[0]
    return first[:100], last[:100]


def _metadata(claims: dict) -> dict:
    """Allow-listed claims only (§2.2): never tokens, never the whole payload."""
    metadata = claims.get("user_metadata") or {}
    kept = {}
    for key in ("name", "full_name", "picture", "avatar_url"):
        value = metadata.get(key) or claims.get(key)
        if isinstance(value, str) and value:
            kept[key] = value[:500]
    return kept


@dataclass(frozen=True)
class Claims:
    subject: str
    email: str
    email_verified: bool
    raw: dict

    @classmethod
    def from_payload(cls, payload: dict) -> Claims:
        subject = payload.get("sub")
        if not subject or not isinstance(subject, str):
            raise exceptions.AuthenticationFailed("Token missing sub claim")
        if len(subject) > 255:  # never truncate: two subjects could collide
            raise exceptions.AuthenticationFailed("Token sub claim is too long")
        return cls(
            subject=subject,
            email=str(payload.get("email") or "").strip(),
            email_verified=email_is_verified(payload),
            raw=payload,
        )


# ── lookups (the part Phase 2 step 7 swaps) ─────────────────────────────────


def find_candidates(email: str) -> list:
    """Live person parties of the portal company holding E, oldest first (2.6).

    The matcher normalises E (``parties.normalize``). Callers still link only
    ASCII addresses (see :func:`_match_1`): casefolding maps ``ß`` to ``ss``.
    """
    from parties.portal import PortalCompanyMissing
    from parties.portal import find_candidates as party_candidates

    try:
        return party_candidates(email)
    except PortalCompanyMissing:
        raise _not_configured() from None


def customer_for_user(user):
    """The Customer a portal user acts for, through its party, or None."""
    from parties.portal import MergeCycle, PortalCompanyMissing
    from parties.portal import customer_for_user as party_customer

    try:
        return party_customer(user)
    except PortalCompanyMissing:
        raise _not_configured() from None
    except MergeCycle as exc:
        logger.error("Portal auth: merge chain of party %s loops; refusing user %s", exc, user.pk)
        raise PortalRefused(
            "Your account could not be loaded. Contact us.", CODE_MERGE_CYCLE,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        ) from None


# ── helpers ─────────────────────────────────────────────────────────────────


def is_privileged(user) -> bool:
    """Staff flag, a staff-kind or service role, or superuser.

    Such an account is never reachable through an untrusted provider's login.
    """
    from core.role_registry import ROLES
    from core.roles import has_staff_role

    if user.is_superuser or user.is_staff or has_staff_role(user):
        return True
    service = [name for name, role in ROLES.items() if role.kind == "service"]
    return user.groups.filter(name__in=service).exists()


def _new_username() -> str:
    # §7: "u-<32 hex>", random for users created after the backfill.
    return f"{GENERATED_USERNAME_PREFIX}{uuid.uuid4().hex}"


def create_portal_user(email: str, first_name: str, last_name: str, roles) -> User:
    """A customer-only ``User``: unusable password, the provider's roles."""
    from core.roles import grant_role

    user = User(
        username=_new_username(),
        email=email.lower(),
        first_name=first_name[:150],
        last_name=last_name[:150],
        is_active=True,
    )
    user.set_unusable_password()
    user.save()
    for role in roles:
        grant_role(user, role)
    if user.is_staff:  # defence in depth: roles_on_create is validated portal-only
        raise RuntimeError(f"portal user {user.username} became staff on create")
    return user


class HeldIdentity(Exception):
    """The user already holds another subject of this provider (not replaced)."""


def attach_identity(provider, user, claims: Claims, *, login: bool = True,
                    replace: frozenset | set = frozenset(),
                    provenance: dict | None = None) -> UserIdentity:
    """Create the ``(provider, subject)`` row for ``user``.

    A user holds at most one subject per provider. Other subjects it holds are
    deleted only when listed in ``replace`` (subjects the caller checked with
    the provider); any other one raises :class:`HeldIdentity`. Call it under
    the Customer row lock so the check and the delete see the same rows.
    """
    name = provider.config.name
    others = UserIdentity.objects.filter(user=user, provider=name).exclude(subject=claims.subject)
    held = set(others.values_list("subject", flat=True))
    if held - set(replace):
        raise HeldIdentity(sorted(held - set(replace)))
    for subject in held:
        logger.warning(
            "Portal auth: replacing %s identity %s of user %s with %s",
            name, subject, user.pk, claims.subject,
        )
    others.delete()
    return UserIdentity.objects.create(
        user=user,
        provider=name,
        subject=claims.subject,
        email=claims.email,
        email_verified=claims.email_verified,
        last_login_at=timezone.now() if login else None,
        # provenance: how the row came to be (e.g. {"created_via": "portal",
        # "linked_by": "email"}); the OIDC erp login reads it (step 6).
        metadata={**_metadata(claims.raw), **(provenance or {})},
    )


def _may_link_privileged(provider) -> bool:
    """May a portal email link attach this provider to a staff/service/superuser account?

    ``trusted`` (D5), and the backend may narrow it: an OIDC provider whose
    back-office email linking is off (``erp_link_by_email``) must not get the
    same link through the portal, or one bearer request would hand its next
    ``/erp/`` login a staff identity row.
    """
    hook = getattr(provider, "may_link_privileged", None)
    return provider.config.trusted and (hook is None or hook())


def _audit_refusal(record, code: str, provider, claims: Claims) -> None:
    """One ``AuditLog`` row per refused candidate (a Party or a Customer)."""
    from core.models import AuditLog

    try:
        AuditLog.objects.create(
            action="portal_refused",
            model_name=f"{record._meta.app_label}.{record._meta.object_name}",
            object_id=record.pk,
            changes={"code": code, "provider": provider.config.name, "subject": claims.subject},
        )
    except Exception:  # an audit failure must not turn a refusal into a 500
        logger.exception("Portal auth: could not write the refusal audit entry")


def _check_usable(provider, user, customer) -> None:
    """Refusals that apply to any resolved user (linked or just linked)."""
    if is_privileged(user) and not provider.config.trusted:
        logger.warning(
            "Portal auth: refused %s login for privileged user %s (provider not trusted)",
            provider.config.name, user.pk,
        )
        raise _collision()
    if not user.is_active or customer is None or not customer.is_active:
        raise _closed()


# ── the resolver ────────────────────────────────────────────────────────────


def resolve_portal_login(provider, payload: dict, request=None):
    """Map verified claims to ``(user, customer)`` or raise a coded refusal."""
    claims = Claims.from_payload(payload)
    name = provider.config.name

    identity = (
        UserIdentity.objects.select_related("user")
        .filter(provider=name, subject=claims.subject).first()
    )
    if identity is not None:
        return _linked(provider, identity, claims)

    if not claims.email:
        raise PortalAuthFailed("No customer linked to this account", CODE_NO_ACCOUNT)

    candidates = find_candidates(claims.email)
    if not candidates:
        return _match_0(provider, claims, request)
    if not claims.email_verified:
        # Never link (or create a duplicate) on an unverified email.
        raise _verify_email()
    if len(candidates) > 1:
        # The merge queue's scan (step 6) proposes the cluster (R1); the audit
        # rows record that a login was refused on it.
        logger.warning(
            "Portal auth: %d parties match the verified email of %s subject %s; refusing",
            len(candidates), name, claims.subject,
        )
        for party in candidates:
            _audit_refusal(party, CODE_AMBIGUOUS, provider, claims)
        raise _ambiguous()
    return _match_1(provider, candidates[0], claims)


def _linked(provider, identity, claims: Claims):
    user = identity.user
    customer = customer_for_user(user)
    _check_usable(provider, user, customer)
    # One write per LAST_LOGIN_RESOLUTION, not one per portal request.
    now = timezone.now()
    UserIdentity.objects.filter(pk=identity.pk).filter(
        Q(last_login_at__isnull=True) | Q(last_login_at__lt=now - LAST_LOGIN_RESOLUTION)
    ).update(last_login_at=now, email=claims.email.lower(), email_verified=claims.email_verified)
    return user, customer


def _refetch(provider, claims: Claims):
    """Lost a race with a concurrent first login for the same subject."""
    identity = (
        UserIdentity.objects.select_related("user")
        .filter(provider=provider.config.name, subject=claims.subject).first()
    )
    if identity is None:
        raise PortalAuthFailed("No customer linked to this account", CODE_NO_ACCOUNT)
    return _linked(provider, identity, claims)


def _require_confirmed(provider, claims: Claims) -> None:
    """The provider itself must confirm the address before it creates or links.

    The token's ``email_verified`` is not enough: Supabase keeps it in
    ``user_metadata``, which the user can write. ``None`` (no service key,
    provider down) refuses too: failing open would trust that claim.
    """
    confirmed = provider.confirms_email(claims.subject, claims.email)
    if confirmed is not True:
        logger.warning(
            "Portal auth: %s did not confirm the address of subject %s (%s); refusing",
            provider.config.name, claims.subject,
            "unconfirmed" if confirmed is False else "provider could not be asked",
        )
        raise _verify_email()


def _throttle_signup(request) -> None:
    from rest_framework.exceptions import Throttled

    from core.throttling import PortalSignupThrottle

    if request is None:
        return
    throttle = PortalSignupThrottle()
    if not throttle.allow_request(request, None):
        raise Throttled(wait=throttle.wait())


def _match_0(provider, claims: Claims, request=None):
    """No candidate: self-register a User + Customer + identity + roles.

    Only for an address the provider confirms: an unconfirmed sign-up would
    otherwise squat the address, and a later order staff attach to it would
    land in the squatter's account.
    """
    from customers.models import Customer
    from parties.portal import (
        PortalCompanyMissing,
        mark_email_verified,
        new_portal_person,
        portal_company_id,
    )

    if not provider.config.self_register:
        raise PortalAuthFailed("No customer linked to this account", CODE_NO_ACCOUNT)
    if not claims.email_verified:
        raise _verify_email()
    _throttle_signup(request)
    _require_confirmed(provider, claims)
    try:
        company_id = portal_company_id()
    except PortalCompanyMissing:
        raise _not_configured() from None
    first_name, last_name = names_from_claims(claims.raw, claims.email)
    try:
        with transaction.atomic():
            user = create_portal_user(
                claims.email, first_name, last_name, provider.config.roles_on_create,
            )
            # A new person party (origin=portal), then the Customer held by
            # it; the party sync (Customer.save) adds its PartyEmail,
            # Party.user and PartyRole(customer) in this transaction (2.6).
            person = new_portal_person(company_id, first_name, last_name, claims.email)
            customer = Customer(
                customer_type=Customer.CustomerType.INDIVIDUAL,
                first_name=first_name,
                last_name=last_name,
                email=claims.email,
                phone=str(claims.raw.get("phone") or "")[:50],
                is_active=True,
                user=user,
                company_id=company_id,
                party=person,
                account_holder=person,
            )
            customer.party_origin = "portal"
            customer.save(force_insert=True)
            if customer.account_holder_id:
                mark_email_verified(customer.account_holder, claims.email)
            attach_identity(provider, user, claims, provenance={"created_via": "portal"})
    except IntegrityError:
        if not UserIdentity.objects.filter(provider=provider.config.name,
                                           subject=claims.subject).exists() and \
                Customer.email_taken(claims.email, company_id):
            # No party matched E, yet uniq_customer_email_ci_company holds it:
            # the database's LOWER() folds E onto another account's address
            # (Postgres folds U+212A KELVIN SIGN to "k"; the party lookup does
            # not). That account is someone else's, as for a non-ASCII match-1:
            # collision. It reveals no more than "no account" did: both refuse
            # a sign-up that would otherwise succeed, and only after the
            # provider confirmed E. No audit row: there is no candidate party.
            logger.warning(
                "Portal auth: refused %s subject %s: uniq_customer_email_ci_company "
                "already holds its address (as folded by the database) with no party "
                "matching it", provider.config.name, claims.subject,
            )
            raise _collision() from None
        return _refetch(provider, claims)  # a concurrent first login won
    logger.info(
        "Portal auth: created customer %s and user %s for %s subject %s",
        customer.pk, user.pk, provider.config.name, claims.subject,
    )
    return user, customer


def _refuse_collision(provider, candidate, claims: Claims, reason: str, *args):
    logger.warning("Portal auth: " + reason, *args)
    _audit_refusal(candidate, CODE_COLLISION, provider, claims)
    return _collision()


def _match_1(provider, candidate, claims: Claims):
    """One candidate party, verified email: link it, or refuse with a code."""
    from customers.models import Customer
    from parties.models import Party
    from parties.portal import AmbiguousAccount, held_customer, link_login, mark_email_verified

    name = provider.config.name
    if not provider.config.link_by_verified_email:
        raise _refuse_collision(provider, candidate, claims,
                                "%s does not link by email (party %s)", name, candidate.pk)
    if not claims.email.isascii():
        raise _refuse_collision(provider, candidate, claims,
                                "refused to link a non-ASCII address to party %s", candidate.pk)
    try:
        account = held_customer(candidate)
    except AmbiguousAccount as exc:
        # 2.1: a person holds at most one commerce account; never pick one.
        logger.warning("Portal auth: party %s holds %d accounts; refusing %s subject %s",
                       candidate.pk, len(exc.customers), name, claims.subject)
        for customer in exc.customers:
            _audit_refusal(customer, CODE_AMBIGUOUS, provider, claims)
        raise _ambiguous() from None
    try:
        existing = _login_of(candidate, account)
    except HeldIdentity as exc:
        raise _refuse_collision(provider, candidate, claims,
                                "refused to link %s subject %s to party %s: %s",
                                name, claims.subject, candidate.pk, exc) from None
    if (existing is not None and is_privileged(existing)
            and not _may_link_privileged(provider)):
        raise _refuse_collision(
            provider, candidate, claims,
            "refused to link %s subject %s to party %s: its user is staff (D5)",
            name, claims.subject, candidate.pk,
        )
    _require_confirmed(provider, claims)

    # Subjects of this provider the login already holds. One is replaced only
    # when the provider says it is gone (deleted, re-created) or never
    # confirmed any address (an unconfirmed squat on this email). A live,
    # confirmed one is somebody else whose address drifted from the party's.
    replaceable = set()
    if existing is not None:
        held = UserIdentity.objects.filter(user=existing, provider=name).exclude(
            subject=claims.subject
        ).values_list("subject", flat=True)
        for subject in held:
            if provider.subject_status(subject) in ("gone", "unconfirmed"):
                replaceable.add(subject)

    try:
        with transaction.atomic():
            # Lock order Customer → Party → User (lock_login_rows), the
            # order erasure and merge take too; the rows are read after the
            # lock. The account's own parties are locked as well: saving it
            # below runs the party sync, which writes them.
            lock_login_rows(
                customers=[account.pk] if account is not None else (),
                parties=[candidate.pk, *((account.party_id, account.account_holder_id)
                                         if account is not None else ())],
            )
            customer = None
            if account is not None:
                customer = Customer.objects.get(pk=account.pk)
            party = Party.objects.get(pk=candidate.pk)
            if party.merged_into_id is not None or party.erased_at is not None:
                raise HeldIdentity("a merged or erased party")
            held = held_customer(party)
            if (held.pk if held else None) != (account.pk if account else None):
                raise HeldIdentity("an account that changed during sign-in")
            user = _login_of(party, customer)
            if user is not None and is_privileged(user) and not _may_link_privileged(provider):
                raise HeldIdentity("staff")
            if (customer is not None and not customer.is_active) or (
                user is not None and not user.is_active
            ):
                raise _closed()
            if user is None:
                first, last = _names_for(party, customer, claims)
                user = create_portal_user(
                    customer.email if customer is not None else claims.email,
                    first, last, provider.config.roles_on_create,
                )
            else:
                # An account made elsewhere (an invite, the backfill, staff)
                # may lack the portal role; the provider's roles are portal
                # roles only (validated), so this never grants staff.
                from core.roles import grant_role

                for role in provider.config.roles_on_create:
                    grant_role(user, role)
            if customer is None:
                # 2.6 match-1: a party that holds no Customer gets an
                # individual one, countersigned and held by the party itself.
                first, last = _names_for(party, None, claims)
                customer = Customer(
                    customer_type=Customer.CustomerType.INDIVIDUAL,
                    first_name=first, last_name=last, email=claims.email, is_active=True,
                    user=user, company_id=party.company_id, party=party, account_holder=party,
                )
                customer.party_origin = "portal"
                customer.save(force_insert=True)
            elif customer.user_id is None:
                customer.user = user  # the sync sets account_holder and Party.user
                customer.party_origin = "portal"
                customer.save(update_fields=["user", "updated_at"])
            party.refresh_from_db(fields=["user"])
            link_login(party, user)
            mark_email_verified(party, claims.email)
            # Re-checked here, under the lock: a concurrent login may have
            # attached a subject since the snapshot above.
            attach_identity(provider, user, claims, replace=replaceable,
                            provenance={"created_via": "portal", "linked_by": "email"})
    except HeldIdentity as exc:
        raise _refuse_collision(
            provider, candidate, claims,
            "refused to link %s subject %s to party %s: its user holds %s",
            name, claims.subject, candidate.pk, exc,
        ) from None
    except AmbiguousAccount:  # a second account appeared since the snapshot
        raise _ambiguous() from None
    except IntegrityError:
        return _refetch(provider, claims)
    logger.info(
        "Portal auth: linked party %s / customer %s (user %s) to %s subject %s via confirmed email",
        party.pk, customer.pk, user.pk, name, claims.subject,
    )
    _check_usable(provider, user, customer)
    return user, customer


def lock_login_rows(*, customers=(), parties=(), users=()) -> set:
    """Lock ``Customer``, then ``Party``, then ``User`` rows: the one lock order.

    Every path that changes who a login belongs to takes its locks in this
    order: portal login (match-1), the staff invite and party erasure.
    Customer → Party is the order ``Customer.save()`` and its party sync write
    in; User comes last (OIDC locks only User). Two transactions that take
    overlapping rows in the same order wait for each other instead of
    deadlocking (Postgres aborts one side of a deadlock with 40P01, which the
    user would see as an error). Each table's rows are locked in pk order, so
    two erasures with overlapping footprints are ordered too.

    The Users named by the locked Customer and Party rows are locked with
    ``users``; their ids are read by the locking statement itself, so after a
    lock wait they are the committed values. Callers re-read the rows they
    need after this returns (never ``select_related`` into a lock, see
    :func:`lock_users`). Returns the ids of the Users it locked. A no-op on
    backends without ``SELECT ... FOR UPDATE``.
    """
    from customers.models import Customer
    from parties.models import Party

    named = list(users)
    for model, pks in ((Customer, customers), (Party, parties)):
        ids = {pk for pk in pks if pk is not None}
        if ids:
            named += [user_id for _pk, user_id in model._base_manager.select_for_update()
                      .filter(pk__in=ids).order_by("pk").values_list("pk", "user_id")]
    return lock_users(*named)


def lock_users(*user_ids) -> set:
    """Lock the ``User`` rows a just-locked Customer / Party names (ids may be None).

    Called after those rows are locked, never joined into their locking query:
    ``select_related("user")`` there is a LEFT JOIN, which Postgres refuses to
    lock ("FOR UPDATE cannot be applied to the nullable side of an outer
    join"), and even narrowed with ``of=("self",)`` the joined row is the one
    the query saw before waiting. Postgres re-reads only the locked row, so
    after a concurrent first login commits, ``customer.user`` came back None
    while ``customer.user_id`` was set. The FK is instead loaded lazily, after
    the lock, by a fresh statement. Locking the User also serialises every
    writer of its identities (``attach_identity`` checks one subject per user
    and provider), whichever Customer or Party row each one locked. Returns
    the ids locked.
    """
    ids = sorted({pk for pk in user_ids if pk is not None})
    if ids:
        list(User.objects.select_for_update().filter(pk__in=ids).order_by("pk").values_list(
            "pk", flat=True))
    return set(ids)


def _login_of(party, customer):
    """The login a candidate already has: ``Party.user``, else the dual-written
    ``Customer.user``. Two different logins on them is a collision."""
    if party.user_id is not None and customer is not None and customer.user_id not in (
        None, party.user_id,
    ):
        raise HeldIdentity("two logins (party and customer differ)")
    if party.user_id is not None:
        return party.user
    return customer.user if customer is not None else None


def _names_for(party, customer, claims: Claims) -> tuple[str, str]:
    if customer is not None:
        return customer.first_name, customer.last_name
    if party.given_name or party.family_name:
        return party.given_name, party.family_name
    return names_from_claims(claims.raw, claims.email)
