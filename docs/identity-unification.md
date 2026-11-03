# Identity unification — design spec

Status: **proposed** (2026-10-08). Steps 1 to 6 and 8 of §8 are built
(`core/permissions.py`, `core/role_registry.py`, `core/roles.py`,
`core/identity/`, `core/service_accounts.py`; see `docs/roles.md`, "As built:
step 3" under 3.3, "As built: step 4" under 3.4, "As built: step 5" under
2.6, "As built: step 6" under 6.2 and "As built: step 8" under 3.7). Step 7
(storefront `/account`) is not built yet.

Requirement (product owner, 2026-10-08): one auth system for staff and
customers; roles that grow over time (today staff vs customer; later admin,
manager, VIP customer); the pattern ships in golden with room for a different
OAuth/OIDC provider per deployment; portal basics (a customer sees order
status) in scope, a full self-service portal later (Phase 5b).

## Decisions at a glance

| # | Decision | Section |
|---|----------|---------|
| D1 | Every human is one `auth.User`. No custom user model. | 2.1 |
| D2 | External logins live in `UserIdentity(provider, subject)`; `UserProfile.supabase_uid` and `Customer.supabase_user_id` are retired. | 2.2 |
| D3 | Roles are Django Groups declared in `GOLDEN_ROLES`, a validated registry. `is_staff` is *derived* from role membership and written only by the role sync. | 2.3 |
| D4 | Providers are declared in `GOLDEN_IDENTITY_PROVIDERS`; each one names the entry points it may serve (`erp`, `portal`). | 2.4 |
| D5 | Auto-linking by verified email only ever attaches to a customer-only account. Staff accounts are linked by an admin or by a provider marked `trusted`. | 3.4, 7 |
| D6 | The staff API stops treating "any authenticated user" as staff. The default DRF permission becomes role-aware before the first customer `auth.User` exists. | 4 |
| D7 | API keys become service accounts: one `auth.User` + hashed key per integration, scoped by a service role. | 2.6 |

---

## 1. Current state

### 1.1 Staff plane: Django session, `auth.User`

- `/erp/login/` is `core.throttling.ThrottledLoginView` (stock `LoginView`,
  20 POSTs/min per IP+username). `LOGIN_URL = "/erp/login/"`,
  `LOGIN_REDIRECT_URL = "/erp/"` (`config/settings/base.py`).
  `AUTHENTICATION_BACKENDS` is not set, so only `ModelBackend` (password).
- `/erp/` gating is `is_staff or is_superuser`, enforced by
  `core.middleware.StaffOnlyERPMiddleware`:
  signed-in non-staff get 403 everywhere under `/erp/` except login, logout
  and password reset. Before the middleware, `ERPViewMixin` was only `LoginRequiredMixin`.
- `core.signals.link_profile_on_login` creates a `UserProfile` for *every*
  session login, with the placeholder `supabase_uid = f"session:{user.pk}"`,
  and attaches the company when exactly one `Company` exists.
- `ERPUserSettings` (OneToOne to `auth.User`): `must_change_password`,
  `email_verified`. Read by `ForcePasswordChangeMiddleware` on `/erp/`.
- `DemoGuardMiddleware` restricts usernames in `DEMO_USERNAMES` (staging demo).
- Outbound hand-off: `core.sso_views.storefront_staff_sso`
  (`/erp/sso/storefront/`) mints a 90-second HMAC token
  (`STAFF_SESSION_SECRET`, `sub = username`) for a staff user and redirects to
  the fixed `STAFF_SSO_CALLBACK_URL`. Django is the identity source; the
  receiving app (storefront staff area, or a chat app in a client deployment)
  holds no user store. It is not an inbound login and stays that way.

### 1.2 Customer plane: Supabase JWT, `Customer`, no `auth.User`

- `core/portal_auth.py` `SupabaseJWTAuthentication` decodes an HS256 token
  with `SUPABASE_JWT_SECRET`, `audience="authenticated"`, no leeway, and
  resolves `sub`:
  1. `Customer.supabase_user_id == sub` → that Customer.
  2. Else, a Customer whose `email` matches `iexact` **and** the token says
     `email_verified is True` (top-level or `user_metadata`) → link it,
     overwriting a stale `supabase_user_id` with a warning.
  3. Else, an existing email match with an unverified token → **refuse**
     (account-takeover guard).
  4. Else, create a minimal active individual `Customer` (race-safe via
     `IntegrityError` re-fetch).
- It returns `CustomerUser(customer)`, a duck-typed proxy (`is_staff=False`,
  `pk = customer.pk`). `core/portal_permissions.IsPortalCustomer` accepts only
  that proxy and sets `request.customer`.
- `portal/views.py` uses `_AUTH = [SupabaseJWTAuthentication]`,
  `_PERM = [IsPortalCustomer]` and scopes every queryset to
  `request.customer`. Mounted at `/api/v1/portal/customer/` and
  `/api/portal/customer/`.
- `core/portal_views.py` `PortalInviteView` / `PortalInviteResendView`
  (`IsAdminUser`, `/api/v1/portal/invite/…`) call the Supabase Admin invite
  API with `SUPABASE_SERVICE_ROLE_KEY` and store the returned id on
  `Customer.supabase_user_id`.
- `Customer.email` is `unique=True` (a case-sensitive DB constraint; lookups
  are `iexact`). `Customer.supabase_user_id` is a nullable unique `UUIDField`.

The planes never meet: a person who is both an employee and a customer has
two unrelated records, and nothing in `/erp/` can say "this customer's portal
login".

### 1.3 Machine identities

| Identity | How it authenticates | Resolves to |
|---|---|---|
| `API_KEY` env var | `Authorization: Api-Key <k>` (or legacy `X-API-Key`) via `core.authentication.APIKeyAuthentication` **and** `core.middleware.APIKeyMiddleware` (all `/api/` paths) | `User.objects.filter(is_superuser=True).first()`, creating `api-service` only when no superuser exists. In practice the key acts as, and audits as, whichever human admin sorts first. |
| Golden-issued JWT, `sub="api-service"` | `BearerTokenAuthentication`, HS256 signed with `API_KEY`, `iss="golden-erp"` | Same first superuser. |
| Golden-issued JWT, `sub="storefront"` | Same class; issued by `/api/v1/core/auth/storefront-token/` | `storefront-api` user (`core/storefront.py`): not staff, unusable password, group `storefront` holding 6 view perms + `orders.add_order`; `StorefrontContractPermission` enforces it. |

One key, one shared principal: no per-integration revocation, no
per-integration audit trail.

### 1.4 What `UserProfile.supabase_uid` means today

`CharField(max_length=255, unique=True)`, required. It dates from a Supabase
login for staff that no longer exists. Today it holds either a historical
Supabase uid (possibly on a profile with `user=None`), the placeholder
`session:<pk>` written by the login signal, or arbitrary strings in ~25 test
files (`supabase_uid="uid-…"`). Nothing authenticates through it. Its only
live effect is that every `UserProfile` must invent a unique value. Its
admin/search exposure (`core/admin.py`, `core/views/__init__.py`
`search_fields`) is all that reads it. `UserProfile` itself must stay: it is
the FK target for `sales_rep`, `approved_by`, `converted_by`, `processed_by`,
`fraud_reviewed_by`, `AuditLog.user`, `Customer.default_sales_rep`, and it
carries `company` for `scope_to_company`.

---

## 2. Target model

### 2.1 One principal

- Every human is one `django.contrib.auth.models.User`. No swap to a custom
  user model: that cannot be done safely mid-project and golden ships to
  deployments with existing data.
- `Customer` gets `user = OneToOneField("auth.User", null=True, blank=True,
  on_delete=SET_NULL, related_name="customer")`. In the Party phase this
  moves to `Party.user`, with the same semantics.
- `UserProfile` stays the **staff** profile (company, display name, FK target).
  Customers do not get one.
- One `User` may be staff *and* customer: it holds both roles, has a
  `UserProfile` for back-office work and a `Customer` for its own orders.

### 2.2 `UserIdentity`

New model in `core` (`core/identity/models.py`, re-exported from `core.models`):

| Field | Type | Notes |
|---|---|---|
| `id` | UUID pk | |
| `user` | FK `auth.User`, `CASCADE`, `related_name="identities"` | |
| `provider` | `CharField(64)` | key in `GOLDEN_IDENTITY_PROVIDERS`; persisted forever, so treat like a theme name |
| `subject` | `CharField(255)` | the provider's stable id (`sub`) |
| `email` | `EmailField(blank=True)` | last-seen, informational, lower-cased |
| `email_verified` | `BooleanField(default=False)` | last-seen |
| `created_at` / `last_login_at` | `DateTimeField` | |
| `metadata` | `JSONField(default=dict)` | allow-listed claims only (name, picture); never tokens |

Constraints: `UniqueConstraint(fields=["provider", "subject"])` and
`UniqueConstraint(fields=["user", "provider"])` (a user holds at most one
subject per provider; `core.0012`, which refuses to run while duplicates
exist and lists them, replaced the plain `(user, provider)` index).

Password logins write no row: the password lives on `User`. Rows exist only
for external subjects.

### 2.3 Roles: `GOLDEN_ROLES`

A settings registry with the same loader shape as `core/theme_registry.py` /
`core/nav_registry.py`: a frozen dataclass, `ImproperlyConfigured` at import on
any bad entry, rebuilt on `setting_changed`. New module `core/role_registry.py`.

```python
# illustrative
@dataclass(frozen=True)
class RoleConfig:
    name: str                 # group name, slug [a-z0-9_-]+, persisted
    label: str
    kind: str = "human"       # "human" | "service"
    staff: bool = False       # grants is_staff (back-office access)
    portal: bool = False      # may use the customer portal API
    permissions: tuple[str, ...] = ()   # "app_label.codename", synced onto the Group
```

- Built in, always present, and an overlay cannot remove them or flip their
  `staff`/`portal` flags: `staff` (`staff=True`), `customer` (`portal=True`),
  `storefront` (`kind="service"`, the current `STOREFRONT_PERMISSIONS`).
  A deployment *appends*, e.g. `manager` (`staff=True`, extra perms) or
  `vip_customer` (`portal=True`).
- Import-time validation: unique names, slug shape, known `kind`, a `service`
  role cannot be `staff` or `portal`, permission strings well-formed.
  Whether a permission row actually exists is checked by a system check after
  migrate, because `Permission` rows are created by `post_migrate`.
- Groups are created and their permissions converged by
  `core.roles.ensure_roles()`, idempotent like `ensure_storefront_identity()`,
  run from a `post_migrate` receiver and `manage.py sync_roles`. No migration
  file is needed for roles.
- **`is_staff` is derived:** `is_staff = any(role.staff for role in
  user_roles)`. Only `core.roles.sync_user_flags(user)` writes it, called from
  an `m2m_changed` receiver on `User.groups` and from `sync_roles`. The
  `is_staff` checkbox in `UserAdmin` becomes read-only. `is_superuser` stays
  orthogonal: break-glass, not a role.
- VIP pricing and similar perks stay record-based (`Customer.pricing_tier`,
  price lists). A `vip_customer` role gates portal *features*, not prices.

### 2.4 Pluggable providers: `GOLDEN_IDENTITY_PROVIDERS`

Validated registry (`core/identity/registry.py`, same loader shape):

```python
# illustrative
@dataclass(frozen=True)
class ProviderConfig:
    name: str                       # persisted in UserIdentity.provider
    backend: str                    # dotted path to a Provider class
    entry_points: tuple[str, ...]   # subset of ("erp", "portal")
    self_register: bool = False     # may create a User on first login
    roles_on_create: tuple[str, ...] = ()
    link_by_verified_email: bool = False   # portal only; see 3.4
    trusted: bool = False           # may grant/link staff-kind roles
    options: dict = field(default_factory=dict)  # backend-specific; secrets by *_env name
```

Shipped backends (`core/identity/providers/`):

| Backend | Entry points | Django surface |
|---|---|---|
| `password` | `erp` (and `portal` if a deployment wants storefront passwords, later) | `ModelBackend`, unchanged |
| `supabase_jwt` | `portal` | DRF authentication class; HS256 secret **or** JWKS (Supabase's asymmetric keys) |
| `oidc` (generic) | `erp` and/or `portal` | Session login for `/erp/` through an auth-code + PKCE flow; bearer validation for the portal API through the issuer's JWKS (`jwt.PyJWKClient`, already in PyJWT) |

Wiring is derived, never hand-listed: `AUTHENTICATION_BACKENDS` and the portal
`authentication_classes` are built from the registry (a helper in
`core/identity/registry.py` that settings call at the end, like
`compose_installed_apps`). Bearer tokens from several issuers share one header,
so the DRF class reads the unverified `iss` claim, picks the provider
configured for that issuer, then verifies with that provider's key. A token
whose `iss` matches no provider for the current entry point is rejected rather
than tried against every key.

`erp` and `portal` are the only entry points. Staff SSO to the storefront or a
chat app (`core/sso_views.py`) stays an outbound hand-off and is not a provider.

### 2.5 Two entry points over one table

- **`/erp/login/`** (staff): the password form plus one button per provider
  whose `entry_points` include `erp`. A session is created only for a user
  holding a `staff`-kind role (or superuser); others get the existing 403 from
  `StaffOnlyERPMiddleware`.
- **Storefront login** (customers): the storefront obtains a provider token
  (Supabase JS, or OIDC PKCE in the browser) and calls the portal API with
  `Authorization: Bearer`. No Django session for customers. The resolver
  (3.4) maps token → `UserIdentity` → `User` → `Customer`.
- One person may hold both roles. Their identities may differ per entry point
  (a password for `/erp/`, Supabase for the storefront) or be the same OIDC
  subject used at both.

### 2.6 Service accounts

New model `core.ApiKey`:

| Field | Notes |
|---|---|
| `user` | FK `auth.User` (the service account), `CASCADE` |
| `name` | human label, unique |
| `prefix` | 8 chars, indexed; shown in the UI |
| `key_hash` | SHA-256 of the secret; the secret is shown once |
| `created_at`, `last_used_at`, `expires_at` (null), `revoked_at` (null) | |

- Key format `gk_<prefix>_<secret>`; lookup by prefix, constant-time compare
  of the hash.
- Each integration gets its own user `svc-<name>`: unusable password,
  `is_staff=False`, member of one or more `kind="service"` roles. The
  storefront identity becomes the first instance of this pattern (role
  `storefront`), and `StorefrontContractPermission` generalises into a
  service-contract check driven by the role's group permissions.
- Golden-issued JWTs (`/api/v1/core/auth/token/`) carry `sub = svc-<name>`
  and are signed with a dedicated `API_JWT_SIGNING_KEY` (defaulting to a
  salted derivation of `SECRET_KEY`), not the API key. `sub` must resolve to a
  user that holds an `ApiKey`, never to a human.
- Back-compat: the `API_KEY` env var maps to a dedicated `api-service` user
  (`GOLDEN_LEGACY_API_KEY_USER`), created as a superuser so existing
  integrations keep their power. It never again resolves to "the first
  superuser", so the audit trail stops naming a human. Deprecated with a
  warning on every use. `APIKeyMiddleware` and `APIKeyAuthentication` share
  one resolver.

**As built: step 5.** Model `core.ApiKey` (the table above plus `scope` and
`created_by`), logic in `core/service_accounts.py`, migrations
`core.0008_apikey` (table) and `core.0009_api_service_account` (creates `api-service`;
reverse is a no-op, the account may be an FK target by then). Commands
`create_api_key --name <svc> --role <service-role> [--scope <token scope>]
[--expires-in 90d] [--company <name|id>] [--key-name …]` (prints the key once,
on stdout) and `revoke_api_key <prefix>… | --name … | --service …`; the
admin lists keys read-only with a revoke action.

- **Contract.** `PrincipalPermission` checks `is_service_account` (an `svc-*`
  username, the storefront identity, a member of a `kind="service"` role, or a
  key holder) *before* the superuser shortcut and gives it
  `service_contract_allows`: safe methods need `view_<model>`; viewset
  `create`/`update`/`partial_update`/`destroy` need `add_`/`change_`/`delete_`;
  custom actions and model-less views are refused; a service account flagged
  superuser or staff is refused outright (`has_perm` would otherwise pass it
  everything). Writes need a pinned company (`--company`, required by
  `create_api_key` once more than one company exists): `CompanyScopedViewSet`
  only polices writes for a resolvable company, so an unpinned account could
  otherwise create rows in any company. The storefront's only write verb is
  `add`, whatever its group holds: the old contract exactly.
  `backfill_tenancy` gives no service account a profile.
- **Resolution.** `resolve_api_key` (shared by `APIKeyAuthentication` and
  `APIKeyMiddleware`; the middleware hands its result to the class so a
  request resolves once) refuses a revoked, expired or unknown `gk_` key
  (401) and a key whose account is no longer sound (inactive, has a usable
  password, staff or superuser): escalating a service account in the admin
  disables its keys instead of empowering them. `last_used_at` is one
  conditional `UPDATE`, at most once a minute per key.
- **Scope.** `ApiKey.scope` is a registered bearer scope (`"api"` by
  default). A plugin scope binds the key to that scope's `path_prefix` on
  direct use (scopes with a `check` callable must go through a bearer) and the
  key mints only that scope. `token_scope(request)` reports it for Api-Key
  requests too.
- **Bearers.** Signed with `API_JWT_SIGNING_KEY` (unset: `salted_hmac` of
  `SECRET_KEY`, system check `core.W110`; equal to `API_KEY`: `core.W111`).
  `sub` is `api-service`, `storefront` or `svc-<name>`; a `svc-*` token carries
  `akp` (its key's prefix, a reserved claim) and is re-checked against that
  key on every request, so revoking or expiring the key kills its tokens, and
  its `exp` never passes the key's. A per-key caller may not request another
  `subject`. `api-service` tokens carry `akh`, a fingerprint of `API_KEY`, so
  rotating or removing the key retires them. Bearers signed with `API_KEY`
  itself (minted before this step) are refused unless a deployment opts into
  `API_JWT_ACCEPT_API_KEY_SIGNATURE` for a short transition (default off since
  2026-10-08), and never with the legacy path disabled.
- **Legacy key decision.** `api-service` is a **superuser** service account,
  as this section specifies, plus `is_staff` so the `IsAdminUser` endpoints
  the old first-superuser reached keep working. Unusable password; never a
  human: refused (not "repaired") when it is inactive, has a usable password
  or holds a non-backfill identity, and disabled entirely with
  `GOLDEN_LEGACY_API_KEY_USER = None`. Created only by `core.0009` or
  `manage.py ensure_legacy_api_user`, never on a request, so deleting it
  switches the legacy key off. A `service-full` role was
  rejected because the legacy key's integrations read company-scoped data
  across companies, which only the superuser bypass in `scope_to_company`
  gives; a role would have silently emptied their results. Deprecation:
  logged per use, plus system check `core.W112` while `API_KEY` is set.
- **Storefront.** Same identity, group and public issuance
  (`/api/v1/core/auth/storefront-token/`, `sub="storefront"`, still gated on
  `API_KEY` being configured); its bearer resolves through
  `resolve_token_subject` and its contract through the shared check.
- **Not built here:** refusing `u-`/`svc-` usernames in the ERP user form and
  `UserAdmin` (§7), and an ERP page for keys. Per-key accounts get no
  company unless `--company` pins one (company-scoped endpoints fail closed to
  no rows).

---

## 3. Migration path

Ordered; each step is reversible until step 7.

### 3.1 Schema (one migration per app)

- `core`: create `UserIdentity`; make `UserProfile.supabase_uid`
  `null=True, blank=True` (unique stays; Postgres allows many NULLs).
  `link_profile_on_login` stops writing `session:<pk>`.
- `customers`: add `Customer.user`.

### 3.2 Roles (no migration)

`ensure_roles()` creates the `staff`, `customer` and `storefront` groups. A
one-shot `sync_roles --adopt-existing` adds every `is_staff=True` user to
`staff` **before** derivation turns on, so no current staff member loses
access. Superusers are not added to `staff`; they pass every gate already.

### 3.3 Backfill (data migration in `customers`, reversible)

1. For each `Customer` with `supabase_user_id`:
   - create `User(username=f"u-{customer.pk.hex}", email=customer.email.lower(),
     first_name, last_name, is_active=customer.is_active)`, then
     `set_unusable_password()` and an explicit `last_login` (the frozen
     `auth.0001` declares it NOT NULL; see `core/storefront.py`);
   - set `customer.user`;
   - create `UserIdentity(provider="supabase", subject=str(supabase_user_id))`;
   - add the user to `customer`.
   Usernames come from the Customer pk, so a re-run is idempotent and never
   collides with a staff username (staff names are human-chosen; the `u-` and
   `svc-` prefixes become reserved, see 7).
2. For each `UserProfile` whose `supabase_uid` is not NULL and does not start
   with `session:`: if `user` is set, create
   `UserIdentity(provider="legacy_supabase", subject=uid)`. That provider name
   is configured nowhere, so the row records history and cannot log anyone in.
   If `user` is NULL, leave the value in place and count it; the migration
   prints the count. Those rows stay FK targets for historical records.
3. Customers without a portal account get **no** `User`. Users are created
   lazily at first portal login or at invite time.

Reverse: delete the `UserIdentity` rows and the `u-…` users created by the
forward pass. The legacy columns were never cleared, so nothing else needs
restoring.

**As built: step 3.** Migrations `core.0006_identity_schema` (table, nullable
`supabase_uid`), `customers.0003_customer_user` and
`customers.0004_backfill_user_identity`. The logic is
`core/identity/backfill.py`, written against historical models.

- `supabase_uid` keeps `unique=True` with Django's NULL semantics (NULLs are
  distinct on Postgres and SQLite); no partial index. The login signal no
  longer writes `session:<pk>`. Reversing the core migration refills NULLs
  (`session:<user pk>`, or `restored:<profile pk>` without a user) so
  `NOT NULL` can return.
- `local:<pk>` placeholders written by `backfill_tenancy` become
  `provider="local"` rows. Subjects are the stored value unchanged
  (`local:5`, the legacy uid). `local` and `legacy_supabase` are reserved
  (`core.identity.RESERVED_PROVIDER_NAMES`): the step-4 registry must refuse
  a provider configured under either name. `backfill_tenancy` still writes
  the placeholder; a custom `--placeholder-uid-prefix` lands as
  `legacy_supabase`, equally inert.
- Generated users get `last_login = NULL`: the migration depends on
  `auth.0012`, so its model state has the nullable column.
- A customer is not linked when its linked user is staff or superuser (D5),
  or when `u-<customer pk hex>` already exists unlinked: the pass never adopts
  a user it did not create, so reverse deletes only its own. The migration
  prints those customer pks.
- `backfill_tenancy` skips customer-only users: a customer never gets a
  staff `UserProfile`.
- Generated users carry the customer's email, so the ERP "Create user" form
  (which refuses a duplicate `User.email`) refuses a staff account for someone
  with a portal login. That is D1: grant the existing user a staff role
  instead of creating a second `User`.
- `UserIdentity` has only the `view` permission and a read-only admin.
  `Customer.user` is read-only in `CustomerDetailSerializer` and the admin.
- **For step 4:** the resolver is unchanged until then and links new portal
  customers through `supabase_user_id` only. Step 4 must re-run
  `backfill_identities` (idempotent) in a migration before its resolver stops
  reading `supabase_user_id`.

### 3.4 Portal auth rewritten

`core/portal_auth.py` becomes the `supabase_jwt` provider plus a shared
resolver, `core/identity/resolve.py`, used by every portal-capable provider:

1. Validate the token (signature, `exp`, `aud`, `iss`, leeway from
   `GOLDEN_IDENTITY_JWT_LEEWAY_S`, default 30).
2. `UserIdentity(provider, sub)` found → `user`. Update `last_login_at`.
3. Not found, token email **verified**, provider has `link_by_verified_email`:
   look up `Customer` by `email__iexact` (oldest first, as today).
   - Customer has a `user` that holds a `staff`-kind role, and the provider is
     not `trusted` → **refuse** and log. Staff accounts are never taken over by
     a self-signup provider.
   - Customer has a `user` → attach a new `UserIdentity` to it. If that user
     already has a row for this provider with another subject, delete the stale
     row and log a warning. This is today's "relink stale `supabase_user_id`"
     behaviour.
   - Customer has no `user` → create the `User` (3.3 shape), link it, add
     `customer`.
4. Not found, an email match exists, token **unverified** → refuse with
   today's message ("Please verify your email address…"). Unchanged.
5. No match and `self_register` → create `User` + `Customer` + `UserIdentity`
   + `customer` role in one `transaction.atomic()`. On `IntegrityError`,
   re-fetch by `(provider, subject)`.
6. `request.user` is the real `User`. `IsPortalCustomer` requires
   `user.is_active`, a `portal` role, `hasattr(user, "customer")` and
   `customer.is_active`, then sets `request.customer`. `portal/views.py` does
   not change.

During the transition the resolver also writes `Customer.supabase_user_id`
(dual-write), so an emergency revert of the code is safe without a data fix.

`CustomerUser` is deleted. `core/schema.py`'s `SupabaseJWTScheme` retargets
the new class path.

`PortalInviteView` / `PortalInviteResendView`: get-or-create the customer's
`User`, call the provider's optional `invite(user)` capability (only
`supabase_jwt` implements it), and store the returned subject as a
`UserIdentity`. The 409 check reads `UserIdentity`, not `supabase_user_id`.

**As built: step 4.** Registry `core/identity/providers.py`, resolver
`core/identity/resolve.py`, DRF class `core.portal_auth.PortalTokenAuthentication`
(`SupabaseJWTAuthentication` is kept as an alias name), permission
`core.portal_permissions.IsPortalCustomer`. `CustomerUser` is gone:
`request.user` is the customer's own `auth.User`. Migration
`customers.0006_rerun_identity_backfill` re-runs `backfill_identities` (no
schema change, reverse is a no-op), so customers the old resolver linked by
`supabase_user_id` alone get their identity before the new code reads only
`UserIdentity`.

Settings shape (golden's `base.py` default):

```python
SUPABASE_JWT_SECRET = env("SUPABASE_JWT_SECRET") or None   # HS256 (legacy keys)
SUPABASE_JWKS_URL = env("SUPABASE_JWKS_URL", "")          # asymmetric keys; wins when set
SUPABASE_ISSUER = env("SUPABASE_ISSUER", "")              # when set, `iss` must equal it
GOLDEN_IDENTITY_PROVIDERS = [
    {"name": "supabase", "backend": "core.identity.providers.SupabaseJWT",
     "entry_points": ["portal"], "self_register": True,
     "roles_on_create": ["customer"], "link_by_verified_email": True,
     "options": {"jwt_secret_setting": "SUPABASE_JWT_SECRET",
                 "jwks_url_setting": "SUPABASE_JWKS_URL",
                 "issuer_setting": "SUPABASE_ISSUER",
                 "audience": "authenticated",
                 "url_setting": "SUPABASE_URL",
                 "service_role_key_setting": "SUPABASE_SERVICE_ROLE_KEY"}},
]
GOLDEN_IDENTITY_JWT_LEEWAY_S = 30
```

Validation (`ImproperlyConfigured` at import, rebuilt on `setting_changed`, no
database access): `password` (`ModelBackend`, `erp`, trusted) is built in and
cannot be redefined; `local` and `legacy_supabase` are refused; names are
slugs of at most 64 characters and unique; the backend must be an importable
`Provider` subclass that serves every declared entry point;
`self_register` / `link_by_verified_email` need `portal`; `roles_on_create`
names known roles, never a staff-kind or service role (even for a trusted
provider: staff access comes from an admin or, in step 6, a trusted
`role_map`), and a portal provider must grant at least one portal role;
options are checked per backend, and a secret (`jwt_secret`,
`service_role_key`) may only be named through `<option>_setting` (a Django
setting) or `<option>_env` (an environment variable), never written inline;
with several portal token providers, each must declare an issuer.

Dispatch and verification: the class reads the unverified `iss`, picks the
portal provider configured for exactly that issuer (a provider with no
issuer is accepted only as the sole portal provider, which is the default),
and verifies with that provider's key alone. The key fixes the algorithm,
never the token: an `HS256` token is checked only against the secret (so
tokens issued before a move to asymmetric keys keep working while both are
set), any other token only against the JWKS key its `kid` names, with that
key's own algorithm (`RS256`/`ES256`/`EdDSA`). An unknown `kid` refetches the
key set at most once a minute, so forged `kid`s cannot amplify into requests
to the provider. `aud` is checked, `exp` and `sub` are required, and the leeway
covers both `exp` and an `iat` slightly in the future. A `sub` longer than
255 characters is refused rather than truncated.

**The provider confirms the address, the token never does.** Supabase keeps
`email_verified` in `user_metadata`, which the user can write. So creating
(match-0) and linking (match-1) both need the provider's own record: for
`SupabaseJWT`, `GET /auth/v1/admin/users/<sub>` with the service-role key must
show `email_confirmed_at` and the same address. Anything else refuses with
`portal_verify_email`, including no key, a 5xx, a timeout (fail closed). The
registry refuses `self_register` or `link_by_verified_email` on a backend that
cannot be asked (for Supabase: no `url` and `service_role_key` options), and
golden's default declares both. A deployment with an empty
`SUPABASE_SERVICE_ROLE_KEY` therefore serves existing logins and invites but
creates and links nothing until the key is set.

States as implemented (Party spec 2.6 codes; the lookup is Customer, not Party,
by `Lower(email)` on both sides):

| State | Condition | Result |
|---|---|---|
| linked | `UserIdentity(P, S)` found | that user and its Customer; `last_login_at`, email and `email_verified` refreshed at most every 5 minutes, only after every refusal check passed |
| match-0 | no candidate, `self_register`, E verified in the token **and** confirmed by P | per-IP throttle (`GOLDEN_PORTAL_SIGNUP_RATE`, default 20/hour, **429**), then one `atomic()`: customer-only `User` (`u-<random hex>`, unusable password, `roles_on_create`), individual active `Customer`, `UserIdentity`, dual-write. An `IntegrityError` re-fetches by `(P, S)`. **Deviation from Party 2.6:** an unverified sign-up creates nothing (401), so nobody can squat an address that staff later attach orders to |
| match-1 | one candidate, E verified and confirmed by P, ASCII, `link_by_verified_email` | the Customer row is locked; its `User` is created if missing, or given `roles_on_create`; identity + dual-write |
| match-1, user holds another subject S' of P | as above | S' is replaced only when P reports it **gone** (404: deleted and re-created, the old "relink stale `supabase_user_id`") or **unconfirmed** (never confirmed any address: a squat left by pre-fix code). The held subjects are re-read under the row lock and `attach_identity` deletes only the ones checked (`replace=`). Otherwise, including when P cannot be asked, **403** `portal_account_collision`: Customer.email may have drifted from S'’s address, and the newcomer is someone else |
| unverified | E not verified in the token (anything but boolean `true`), or P does not confirm it | **401** `portal_verify_email`; nothing written |
| match-N | ≥2 candidates (emails differing only by case), E verified | **409** `portal_account_ambiguous`; nothing linked; one `AuditLog` row (`action="portal_refused"`) per candidate. Replaces the old "oldest first" pick |
| collision | the user found (linked) or the candidate's user is staff, holds a staff-kind or service role, or is superuser, and P is not `trusted` (D5); a held live subject (above); a non-ASCII E (Postgres `iexact` is `UPPER()`, which folds `ı`→`I`, `ſ`→`S`); or P does not link by email | **403** `portal_account_collision`; nothing linked; `AuditLog` row and a warning log |
| closed | the user or its Customer is inactive, or the user has no Customer | **403** `portal_account_closed` |
| no account | no `email` claim, or match-0 without `self_register` | **401** `portal_no_account` |

Bodies are `{"detail": …, "code": …}`. `IsPortalCustomer` additionally
requires that the request was authenticated by the portal class (so a staff
session or bearer for a dual-role user never reaches portal routes), an
active user, a portal role and an active Customer. The class is registered
only on portal views, so a portal token never authenticates the staff API.

Invites: `invite_provider()` is the first portal provider whose backend can
invite (`SupabaseJWT.invite`, the old Supabase Admin call). The 409 reads
`UserIdentity` for that provider; a Customer whose user is staff gets 409
before any provider call (the resolver would refuse the login anyway); the
provider is called first and the `User` + identity (`last_login_at` NULL) +
dual-write are written only after it succeeds; a subject the provider
returns that already belongs to another user is a 409, and so is a subject
another request attached between the 409 check and the row lock (the check
is repeated under the lock; an invite never replaces an identity). Resend needs the
identity and never relinks. Responses keep `supabase_user_id` (the subject)
and add `user_id` and `provider`.

Phase 2 step 7 swapped `resolve.find_candidates` (the Customer email lookup)
and `resolve.customer_for_user` for the Party matcher, scoped to
`GOLDEN_PORTAL_COMPANY`; every branch and code stays.

Review notes (kept as risks): `backfill_identities` writes
`provider="supabase"`, so a deployment that names its Supabase provider
differently must not rename it; a `trusted` provider with `self_register` /
`link_by_verified_email` may link staff accounts by email (that is §6.2's
design for a company IdP, never for a public signup provider); when an
unconfirmed squat is replaced, the owner inherits whatever the squatter put
on that Customer (addresses). Production should set `SUPABASE_ISSUER`. When
the dual-write is skipped because another customer still holds that
`supabase_user_id` (a backfill conflict), the Party code still counts that
customer as having a login: `parties.mirror.has_login` and
`parties.dedupe_legacy` read `user_id or supabase_user_id`. That is
conservative (it can only add an account holder) and goes away with step 8.
(Done: see 3.7.)

Not built in step 4: deriving `AUTHENTICATION_BACKENDS` from the registry
(only `ModelBackend` exists until the OIDC step), the system check for
`UserIdentity` rows whose provider is not configured, unmounting portal
routes when no provider declares `portal` (the base default declares
Supabase, as before), and storefront handling of the refusal codes.

### 3.5 `Customer.email unique=True`

Keep it for this track: the verified-email rule depends on there being at most
one candidate. Two changes only: lower-case on write (`Customer.save` and
serializers), and the backfill reports, but does not merge, Customers whose
emails differ only by case. The Party phase drops the constraint (a party has
several emails). The verified-email lookup then has to choose between several
parties, and that choice belongs in the Party spec. It must keep the
"unverified never links" rule.

Done: the constraint
is now `uniq_customer_email_ci_company`, unique per tenant on `Lower(email)`.

### 3.6 Tests that change

- `core/tests/test_portal_auth.py`: same four scenario classes, asserting on
  `UserIdentity` / `User` / `Customer.user` instead of `supabase_user_id`.
  `test_stale_supabase_link_is_overwritten_on_verified_email` becomes "stale
  identity replaced". New: staff-held Customer refuses email auto-link; leeway;
  issuer dispatch; JWKS path.
- `core/tests/test_auth_matrix.py`: the portal↔staff separation tests stay.
  Add: a customer-only `User` with a session or bearer gets 403 on staff API
  viewsets; a dual-role user reaches both.
- `core/tests/test_portal_invite_resend.py`, `portal/tests.py`: fixtures
  create `User` + `UserIdentity`.
- `core/tests/test_storefront_identity.py`: unchanged in intent, and keeps
  passing when the identity moves to the service-role mechanism.
- `core/tests/test_authentication.py`: legacy key → `api-service`, never a
  human superuser; per-key auth, revocation, expiry.
- New: `core/tests/test_role_registry.py` (config errors, built-ins
  immutable, `is_staff` derivation, adopt-existing),
  `core/tests/test_identity_backfill.py` (forward/reverse, idempotence,
  `legacy_supabase` rows inert).
- The 24 test files that build `UserProfile(supabase_uid="uid-…")` keep
  working while the column is nullable. They are rewritten only in step 8.

### 3.7 Legacy columns retired (step 8)

**As built: step 8.** Four migrations, in this order:

1. `core.0010_userprofile_legacy_external_id` adds
   `UserProfile.legacy_external_id` (`CharField(255)`, blank, not editable).
   It is added on every deployment, empty unless step 2 finds an orphan, so
   the schema is identical everywhere (§7 said "only if the count is
   non-zero"; a conditional column would break schema parity).
2. `customers.0008_identity_retire_precheck` runs
   `core.identity.retire.retire_legacy_columns` on the historical registry:
   - re-runs `backfill_identities` (idempotent), so any legacy value the
     step-3 rules can capture becomes a `UserIdentity` now;
   - **archives orphan uids**: a real Supabase uid (not `session:`,
     `restored:` or `local:`) on a profile with `user` NULL is copied to
     `legacy_external_id`. It cannot be a `UserIdentity` (that row needs a
     user), and the profile must stay as an FK target. Values on profiles
     with a user already live on as inert `legacy_supabase` / `local` rows;
   - **refuses the drop** (`LegacyIdentityNotCaptured`, the migration fails
     and nothing is dropped) when a `Customer.supabase_user_id` has no
     `UserIdentity` with that subject under a real provider (any configured
     name; `legacy_supabase` and `local` rows never count), or only on users
     that have no customer (the column would be the login's only customer
     link), or a linked profile's uid has no inert row. The message lists customer and profile
     pks only. The backfill leaves a customer uncaptured when its user is
     staff or superuser (D5) or `u-<customer pk hex>` is held by an unlinked
     user. Fix each by hand, then migrate again: attach the login to the right
     user as `UserIdentity(provider, subject)` and link the customer, or NULL
     the column with SQL (`dbshell`; the models no longer have the field) if
     the login is dead;
   - reports, without refusing, a customer whose uid is another user's
     identity (the resolver relinked the subject): that login already
     resolves through the identity table, so the drop changes nothing. If
     that customer has no user, its `account_holder` is cleared: it held one
     only through the column (Party spec, "account holder = a login").
   If the re-run linked customers, it asks for `backfill_parties --step 7`
   (their `Party.user`).
3. `customers.0009_remove_customer_supabase_user_id` and
4. `core.0011_remove_userprofile_supabase_uid` drop the columns (separate
   from the data step, so Postgres never alters a table with pending
   deferred-FK events).

Reverse runs the four backwards: the columns come back nullable, then
`restore_legacy_columns` refills `Customer.supabase_user_id` from the
customer's user's UUID-shaped subject and `UserProfile.supabase_uid` from the
archive or the user's inert row, skipping any value another row holds.

Code: the resolver and the invite views no longer dual-write (the invite
responses keep the `supabase_user_id` key, now documented as the provider
subject, and add `subject`); `backfill_tenancy` creates profiles without a
placeholder and `--placeholder-uid-prefix` is gone; "has a login" in
`parties.mirror.has_login`, `parties.dedupe_legacy` and the D7 backfill step is
`Customer.user` (the D3/D7 code still honours the legacy column on a historical
model, so `parties.0002` replays unchanged); `parties.sync`, `parties.erasure`,
``CustomerSerializer` (**API change:** staff Customer responses no longer carry
`supabase_user_id`), the `UserProfile` admin (now searchable by username and
`legacy_external_id`) and the staff profile API's `search_fields` drop the
columns. `core.identity.backfill` stays: migrations `0004`, `0006` and `0007`
import it, and it only works on a historical registry. Tests that exercise
it, or the pre-check, use `core.tests.legacy_schema.LegacyColumnsTestCase`,
which re-adds both columns to the test tables for the class and exposes the
historical registry as `self.H`.

Precondition met downstream: the 0.3.0-rc.1 rehearsal ran the step-3/4
backfill (`identity backfill: local placeholder identities (inert): 4`), and
production holds no `supabase_user_id` value.

---

## 4. Authorization

| Check | Today | Target | Kind |
|---|---|---|---|
| `/erp/` access | `StaffOnlyERPMiddleware`: `is_staff or is_superuser` | unchanged code; `is_staff` now derived from a `staff`-kind role | role |
| Django admin | `is_staff` + model perms | same; `is_staff` checkbox read-only | role |
| Staff API default (`StorefrontContractPermission`) | any authenticated user passes; only `storefront-api` is restricted | `PrincipalPermission`: superuser passes; `staff`-kind role passes; service account passes only what its role's group permits (today's storefront contract, generalised); everyone else, including customer-only users, gets **403** | role |
| `IsAdminUser` endpoints (company list, portal invite) | `is_staff` | unchanged (derived flag) | role |
| Portal `_PERM` (`IsPortalCustomer`) | `isinstance(user, CustomerUser)` | `portal` role + active `User` + active linked `Customer` | role |
| Portal querysets | `customer=request.customer` | unchanged | record |
| Company scoping (`core.managers.scope_to_company`, `CompanyScopedViewSet`, `accounting.erp_views.resolve_company`) | `user.userprofile.company`; superuser unfiltered; fail closed | unchanged. Only staff-kind users get a `UserProfile`: `link_profile_on_login` is gated on the role, so a customer session never mints a profile | record |
| Manager-only actions (future) | n/a | model permissions on the `manager` role's group, checked with `has_perm` | role → perm |

Why D6 is a hard prerequisite: today every `auth.User` is staff or a service,
so `IsAuthenticated` was effectively "is staff". The moment customers become
`auth.User` rows, a customer who obtains a Django session (an OIDC login on the
ERP origin, a future password login) would pass `IsAuthenticated` on every
viewset that is not company-scoped. `PrincipalPermission` must land before the
first customer `User` is created (step 1 in §8).

Related writes to close in the same step: `customers/serializers.py`
`CustomerSerializer` is `fields="__all__"`. `user` (and, until removed,
`supabase_user_id`) must be in `read_only_fields`, or a staff API client can
re-point a portal login at another customer. For the same reason,
`erp_assist/core/_writable_fields.py` `CUSTOMER_WRITABLE` must drop
`supabase_user_id` and must never gain `user`: the assistant can otherwise
relink portal accounts. Identity links change only through the resolver, the
invite views, or an explicit admin action.

---

## 5. Portal basics

Existing customer API (`portal/urls.py`, under `/api/v1/portal/customer/`):

| Endpoint | Methods |
|---|---|
| `profile/` | GET, PUT/PATCH |
| `addresses/`, `addresses/<uuid>/` | GET, POST; PUT/PATCH |
| `contacts/` | GET |
| `orders/` (`?status=`, ordering), `orders/<uuid>/` | GET (detail includes line items and sub-orders with `tracking_number`, `tracking_url`, estimated/actual ship and delivery dates) |
| `quotes/`, `quotes/<uuid>/` | GET |
| `invoices/` | GET |
| `documents/` | GET (verified product docs for ordered variants) |
| `activity/` | GET (last 20 order/quote events) |
| `module-counts/` | GET |

Admin side: `/api/v1/portal/invite/`, `/api/v1/portal/invite/resend/`.

The storefront (`storefront/src/routes/`) has no auth and no account area
today. It needs:

1. A small auth adapter (`storefront/src/lib/auth/`), one implementation per
   provider kind (Supabase JS, OIDC PKCE), chosen by `VITE_AUTH_PROVIDER`.
   Tokens are kept in memory with refresh, not in `localStorage` where the
   provider SDK allows it.
2. `/account/login`, `/account` (redirects to orders), `/account/orders`
   (list: number, date, status, payment and fulfillment status, total),
   `/account/orders/[id]` (lines, sub-orders, tracking links). Order status
   ships first; profile and addresses follow over the same endpoints.
3. `CORS_ALLOWED_ORIGINS` must include the storefront origin. e2e tests mock
   the portal API at `E2E_API_ORIGIN`, like the catalog journeys (see
   CLAUDE.md), with no backend on :8000.

The `/account` pages can be built against today's Supabase-backed portal API
before unification lands. The adapter boundary keeps them provider-agnostic.

**Phase 5b, out of scope here:** quote acceptance, invoice payment from the
account area, returns/RMAs, a document centre, reorder, B2B multi-user
accounts (contacts as users with their own roles), notification preferences,
VIP self-service.

---

## 6. Per-deployment configuration

Golden's `base.py` default is password-only staff with no portal provider.

### 6.1 Password staff, Supabase customers (the current client shape)

```python
GOLDEN_IDENTITY_PROVIDERS = [
    {"name": "password", "backend": "core.identity.providers.Password",
     "entry_points": ["erp"], "trusted": True},
    {"name": "supabase", "backend": "core.identity.providers.SupabaseJWT",
     "entry_points": ["portal"], "self_register": True,
     "roles_on_create": ["customer"], "link_by_verified_email": True,
     "options": {"jwt_secret_env": "SUPABASE_JWT_SECRET",
                 "jwks_url_env": "SUPABASE_JWKS_URL",   # either one
                 "audience": "authenticated",
                 "issuer_env": "SUPABASE_ISSUER",
                 "service_role_key_env": "SUPABASE_SERVICE_ROLE_KEY",  # enables invite()
                 "url_env": "SUPABASE_URL"}},
]
GOLDEN_ROLES = []   # built-ins only: staff, customer, storefront
```

### 6.2 OIDC for both, via Authentik

As built in step 6 (`core.identity.oidc.OIDC`, also importable as
`core.identity.providers.OIDC`):

```python
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
GOLDEN_IDENTITY_PROVIDERS = [
    # `password` (ModelBackend, erp, trusted) is built in: break-glass admin.
    {"name": "authentik", "backend": "core.identity.providers.OIDC",
     "entry_points": ["erp", "portal"], "trusted": True,
     "self_register": True, "roles_on_create": ["customer"],
     "link_by_verified_email": True,
     "options": {"issuer": "https://auth.example.com/application/o/golden/",
                 "client_id_env": "OIDC_CLIENT_ID",
                 "client_secret_setting": "OIDC_CLIENT_SECRET",
                 # optional: "discovery_url", or explicit "authorization_endpoint",
                 # "token_endpoint", "jwks_uri" (each overrides discovery)
                 "scopes": ["openid", "email", "profile", "groups"],
                 "claims": {"groups": "groups"},   # also email, email_verified, name
                 "role_map": {"golden-staff": "staff", "golden-managers": "manager"},
                 "label": "Company SSO",           # "Sign in with Company SSO"
                 "erp_link_by_email": True,  # /erp/: link existing staff by verified email
                 # optional: "audience" (portal bearer aud, default client_id),
                 # "redirect_uri" (default: this host's /erp/oidc/<name>/callback/),
                 # "token_endpoint_auth": "client_secret_basic" | "client_secret_post"
                 }},
]
GOLDEN_ROLES = [
    {"name": "manager", "label": "Manager", "staff": True,
     "permissions": ["orders.change_quote", "accounting.change_journalentry"]},  # illustrative
]
```

Register `https://<erp host>/erp/oidc/authentik/callback/` as the redirect URI
at the IdP. `/erp/login/` shows one "Sign in with <label>" button per trusted
OIDC provider serving `erp`.

**As built: step 6.**

- **Session login (`erp`).** `/erp/oidc/<name>/start/` (throttled, 20/min per
  client IP, 429) stores state, nonce and a PKCE (S256) verifier in the
  session (at most 5 pending flows, 10 minutes each) and redirects to the
  issuer. `/callback/` pops the flow for `state` (unknown, reused, expired or
  another provider's state: 400), exchanges the code with the verifier, and
  verifies the ID token: JWKS key named by `kid`, asymmetric algorithms only,
  `iss` = issuer, `aud` = client id (and `azp` with several audiences),
  `exp`/`iat`/`sub` required with the step-4 leeway, `nonce` equal. The
  discovery document is cached for an hour and its `issuer` must equal the
  configured one (cache keyed by URL and issuer; a failed fetch fails fast
  for 30 s). Resolution: `UserIdentity(name, sub)` → its user; else a user
  with the token's email, linked only when the provider opts in with
  `erp_link_by_email: True` (default off: some IdPs assert `email_verified`
  for any self-enrolled address), is `trusted`, and the token has
  `email_verified: true` (a boolean); never an inactive user, one of several
  users with that email, or a user holding another subject of the provider
  (an address that exists is refused, not duplicated); else a new `u-<hex>`
  user only when the IdP groups map to a staff-kind role. Superusers and
  service accounts are refused on every branch, including an identity row
  the portal resolver linked. Then roles sync, and the session is created
  only for an active staff principal (otherwise 403 on the login page).
  The portal resolver records provenance on rows it writes
  (`metadata.created_via = "portal"`, plus `linked_by = "email"` for a
  match-1 link). With `erp_link_by_email` off, a row linked by email on the
  portal never opens `/erp/`, and `_match_1` refuses to link a staff,
  service or superuser account for that provider at all (provider hook
  `may_link_privileged`), so one bearer request cannot do what the erp
  switch forbids. `django.contrib.auth.login()` cycles the session key; the session
  is restored by `ModelBackend` (no new entry in `AUTHENTICATION_BACKENDS`).
- **Bearer (`portal`).** Same JWKS machinery and `iss` dispatch as
  `SupabaseJWT`; `aud` = `audience` option or client id. Claims pass through
  the `claims` mapping and only top-level boolean `email_verified` counts
  (nothing nested is forwarded to the resolver). An unusable provider (an
  unset `client_id_env`) refuses with 401 "Sign-in provider unavailable",
  never a 500. The provider "confirms" an
  address when the token verified in that same request asserted it: unlike
  Supabase's `user_metadata`, an OIDC `email_verified` is the IdP's signed
  statement. A held subject cannot be checked with the IdP, so the resolver
  treats it as live (collision).
- **Role sync (`role_map`).** At every session login, and on portal requests
  whose token carries the groups claim: mapped roles the user lacks are
  added; roles this provider added earlier (`UserIdentity.metadata
  ["granted_roles"]`) and no longer mapped are removed, unless another
  identity of the same user still maps them (its `metadata["mapped_roles"]`). A role the user
  already held (granted by hand) is not recorded and never removed by the
  IdP. A token without the groups claim changes nothing. Removal takes
  effect at the next login (§7).
- **Trust, at registry build.** An `erp` entry point needs `trusted: True`
  (a back-office session is staff access, D5); any `role_map` needs
  `trusted: True`; a `role_map` target must be a known role and never a
  service role, and a staff-kind target needs the `erp` entry point (a
  portal-only provider would grant staff from a bearer token). Two portal
  token providers may not share an issuer. `scopes` must include `openid`; `scopes`, `claims`,
  `role_map`, `label` and `token_endpoint_auth` are inline only;
  `client_secret` only through `_setting`/`_env`.
- **Known limit:** the JWKS key fixes the algorithm; a JWK without `alg` is
  read with its key type's default (RS256 for RSA), so an IdP that signs
  PS256/RS384 must publish `alg` in its JWKS. Refused, never accepted.
- **Dependency:** none added. Discovery, the token exchange and the JWKS are
  a few `httpx` calls plus PyJWT, and the verification half is shared with
  the portal bearer path, which no Django OIDC client library covers.
- **Code:** `core/identity/oidc.py` (provider, flow, `resolve_erp_login`,
  `sync_roles`), `core/oidc_views.py`, `core/identity/base.py` (the
  `Provider` base and JWKS client, split out of `providers.py` so a backend
  module can subclass it without importing the registry), `core.roles.
  revoke_role`, and an `after_portal_login` hook in `PortalTokenAuthentication`.

### 6.3 No customers (internal ERP only)

```python
GOLDEN_IDENTITY_PROVIDERS = [
    {"name": "password", "backend": "core.identity.providers.Password",
     "entry_points": ["erp"], "trusted": True},
]
```

No provider declares `portal`, so the portal customer routes are not mounted
(404) and nothing can grant `customer`. The role still exists, inert.

---

## 7. Risks and open questions

| Risk / question | Proposed default |
|---|---|
| **Username collisions.** Generated users need usernames that never clash with staff. | `u-<32 hex>` for humans (from the Customer pk in the backfill, random afterwards), `svc-<name>` for services. Both prefixes are reserved: the ERP user-create form and `UserAdmin` refuse them for hand-made accounts. Email is never the username (it changes and would leak in URLs/logs). |
| **Email case.** `Customer.email` unique is case-sensitive in the DB while matching is `iexact`; `User.email` is not unique at all. | Lower-case on write going forward; report existing case-duplicates without merging; match `iexact`, oldest first (today's rule). No uniqueness on `User.email`. A functional unique index on `Lower(email)` is a Party-phase decision. |
| **Email auto-link reaching a staff account** through a self-signup provider (a Supabase signup with an employee's address). | Refused unless the provider is `trusted` (D5). Staff accounts gain external identities only through an admin "link identity" action or a trusted provider. Tested. |
| **Session fixation / login CSRF on link.** | Linking happens only inside a login flow, and `django.contrib.auth.login()` cycles the session key. A later "connect another login" page (not in this track) requires re-authentication within 5 minutes plus the OIDC `state`/`nonce`/PKCE checks. Bearer portal auth is stateless and has no session to fix. |
| **JWT clock skew.** Today: zero leeway, so a skewed box sees spurious "Token expired". | `GOLDEN_IDENTITY_JWT_LEEWAY_S = 30`; reject `iat` more than leeway in the future; `exp` required. |
| **Supabase key rotation to asymmetric signing.** | `supabase_jwt` accepts either an HS256 secret or a JWKS URL; JWKS is preferred when both are set. `PyJWKClient` caches keys. |
| **Token confusion:** golden-issued, Supabase and OIDC tokens all arrive as `Bearer`. | Dispatch on `iss`, verify with that issuer's key only, check `aud`. Golden-issued tokens are accepted only on staff API classes, provider tokens only on their declared entry points. The existing "portal token rejected on staff route" test stays. |
| **`is_staff` derivation vs Django admin.** An admin ticks `is_staff` by hand; sync later clears it, or a sync bug locks staff out. | Adopt existing staff into `staff` before derivation turns on (3.2). The admin checkbox is read-only and help text points to roles. A system check warns when `is_staff` disagrees with roles. Superuser stays separate, so a superuser can always get in to repair roles. |
| **IdP role sync removes access mid-session.** | Roles are re-synced at login, not per request. Removal takes effect on the next login or session expiry. Documented; a deployment needing faster revocation shortens `SESSION_COOKIE_AGE`. |
| **Customer deactivation vs user deactivation.** | Portal requires both `User.is_active` and `Customer.is_active`. Deactivating a `Customer` does not touch a dual-role user's staff access. |
| **Deleting a User** would cascade `UserIdentity` and null `Customer.user`. | Allowed. Orders stay attached to the Customer. Next login with the same provider subject goes through the verified-email path again. |
| **Renamed or removed provider** orphans `UserIdentity` rows (as `legacy_supabase` rows do on purpose). | System check *warns* on rows whose provider is not configured; never auto-deletes. |
| **Orphan `UserProfile` rows** (`user=None`, real Supabase uid). | Left as FK targets. Step 8 archives their uid into `UserProfile.legacy_external_id` before dropping the column (3.7; the column exists everywhere, filled only for orphans). |
| **Multi-company deployments:** one human buying from two companies on one instance. | One `Customer` per `User` (OneToOne) until Party. Flag it in the Party spec. |
| **Account-creation abuse** via portal self-registration (each new verified token creates rows). | Rely on the provider's signup rate limits; add a per-IP DRF throttle on the create path only. |
| **OIDC library.** | Decided in step 6: hand-rolled on PyJWT + `httpx` (both already dependencies), so the session flow and portal bearer validation share one verifier (§6.2 "As built"). Open question: `django-allauth` if a deployment wants several social providers. |
| **Test churn from D6:** API tests that authenticate a plain `create_user()` now get 403. | Same remedy as the staff-only middleware: such test users get the `staff` role (one shared `make_staff_user` test helper). Count before starting; the PR lists every file it touches. |

---

## 8. Implementation plan

Rule for every step that **adds a migration** (marked **[M]**): it lands after
the Pin's core schema freeze **unless the same PR regenerates the schema-parity
baseline** and amends the parity manifest with evidence. Steps without a
migration may land at any time.

| # | PR | Migration | Tests |
|---|---|---|---|
| 0 | **Prereq:** `StaffOnlyERPMiddleware` merged. | — | (with the middleware) |
| 1 | **Close the staff-API door (D6) + write hardening.** `PrincipalPermission` replaces `StorefrontContractPermission` on every viewset (staff = `is_staff`/superuser for now; storefront contract kept). `CustomerSerializer` read-only `supabase_user_id`; drop it from `CUSTOMER_WRITABLE`. `link_profile_on_login` only for staff. | none | auth matrix: non-staff human 403 on every guarded viewset; storefront contract unchanged; erp_assist cannot write the link; test-user helper migration |
| 2 | **Roles registry.** `core/role_registry.py`, `GOLDEN_ROLES`, `ensure_roles()` on `post_migrate`, `sync_roles [--adopt-existing]`, `is_staff` derivation + read-only admin field, system checks. `PrincipalPermission` switches to roles. | none (groups via `post_migrate`) | `test_role_registry.py`: ImproperlyConfigured cases, built-ins immutable, derivation on group add/remove, adopt-existing, superuser unaffected |
| 3 | **Identity schema + backfill.** `UserIdentity`, `Customer.user`, `UserProfile.supabase_uid` nullable, data migration 3.3. | **[M]** core, customers | `test_identity_backfill.py` forward/reverse/idempotent; `legacy_supabase` inert |
| 4 | **Provider registry + portal resolver.** `GOLDEN_IDENTITY_PROVIDERS`, `supabase_jwt` provider (secret + JWKS, leeway, `iss` dispatch), `core/identity/resolve.py`, new `IsPortalCustomer`, `CustomerUser` deleted, invite views on `UserIdentity`, dual-write of `supabase_user_id`. | none | rewritten `test_portal_auth.py`, invite tests, `portal/tests.py`; staff-held Customer refusal; dual-role user |
| 5 | **Service accounts.** `ApiKey`, `svc-*` users, per-key auth in both the class and the middleware, legacy `API_KEY` → `api-service`, `API_JWT_SIGNING_KEY`, storefront identity on the service-role path, management command `create_api_key`. | **[M]** core | `test_authentication.py` (legacy never resolves to a human; revoke/expire; `sub` must be a service), `test_storefront_identity.py` green |
| 6 | **OIDC provider.** Session login for `/erp/` (button on the login page), bearer validation for the portal, `role_map` sync, `trusted` enforcement; new dependency. | none | flow tests against a stub issuer (local JWKS); role sync add/remove; untrusted provider cannot map to staff |
| 7 | **Storefront `/account` (orders first).** Auth adapter, login, orders list/detail; profile/addresses after. Can start any time against the current API. | none | Playwright journeys with mocked portal API; axe on the new pages |
| 8 | **Retire legacy columns.** Stop dual-write; drop `Customer.supabase_user_id`; archive orphan uids, then drop `UserProfile.supabase_uid`; rewrite the 24 test files' fixtures; remove admin/search references. Only after every deployment has run step 3's backfill. | **[M]** core, customers | full suite; schema-parity baseline regenerated |

Steps 1, 2, 4, 6 and 7 carry no migration and are not blocked by the Pin;
step 4 requires step 3. Steps 3, 5 and 8 are blocked by the freeze rule above.
