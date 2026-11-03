# Roles

Roles are Django `Group`s with a declared meaning, configured in
`settings.GOLDEN_ROLES` and loaded by `core/role_registry.py`. The design behind them is in
[`docs/identity-unification.md`](identity-unification.md).

## Built-in roles

| Role | Kind | Meaning |
|---|---|---|
| `staff` | human | Back-office access. Grants `is_staff`. Carries `parties.export_pii` (personal-data columns in CRM CSV exports) unless `GOLDEN_STAFF_EXPORT_PII = False`; to revoke on a live deployment, set that and remove the permission from the group (declared permissions are a floor). |
| `customer` | human | May use the customer portal API (`IsPortalCustomer`). Granted to the accounts a portal provider creates or links (`roles_on_create`). |
| `storefront` | service | The storefront channel identity's group (`core/storefront.py`). |

They always exist. A deployment cannot redefine, remove or re-flag them: an
entry with a built-in name raises `ImproperlyConfigured` at startup.

## Adding roles in a deployment

Append to `GOLDEN_ROLES` in the deployment's settings overlay:

```python
GOLDEN_ROLES = [
    {"name": "manager", "label": "Manager", "staff": True,
     "permissions": ["orders.change_quote", "accounting.change_journalentry"]},
    {"name": "vip-customer", "label": "VIP customer", "portal": True},
]
```

| Key | Default | Notes |
|---|---|---|
| `name` | required | Lower-case slug (`[a-z0-9][a-z0-9_-]*`). Stored as `Group.name`: never rename a role in place. |
| `label` | required | Display text. |
| `kind` | `"human"` | `"human"` or `"service"`. A service role cannot be `staff` or `portal`. |
| `staff` | `False` | Membership grants back-office access (`is_staff`). |
| `portal` | `False` | Membership allows the customer portal. |
| `permissions` | `()` | `"app_label.codename"` strings added to the group. |

Bad entries fail at import with `ImproperlyConfigured`. A permission that is
well-formed but does not exist is a system-check warning (`core.W104`), because
permission rows only exist after `migrate`.

VIP pricing stays record-based (`Customer.pricing_tier`, price lists). A
`vip-customer` role gates portal features, not prices.

## Archiving a person

`core.staff.archive_profile(profile, by)` is how someone who leaves stops being a login without disappearing from
history: it deactivates the user, makes the password unusable, **removes every
role** (every `GOLDEN_ROLES` group; other groups stay), freezes the profile's
`display_name` and writes an `AuditLog` row (`action="archive"`). It never
deletes: the records they own keep them as owner and show "Owner archived" in
the CRM attention queue until reassigned. A returning employee is the same
`auth.User`, reactivated and given roles again, never a second account.
Superusers, service accounts and archiving yourself are refused.

## Party roles are not auth roles

`GOLDEN_PARTY_ROLES` (`parties/role_registry.py`) says what a *party* is to the
business; the roles on this page say what a *login* may do. They never share a
registry. Built-in party roles: `customer` and `lead` and `staff` (system
managed), `supplier`, `partner`, `referral_source` and
**`client`** (staff managed, both kinds): a party the tenant delivers services
to. Active client = an open `PartyRole(client)`; former = a closed one. It is
independent of `customer`, which tracks commerce accounts. A deployment that
declared its own `client` must drop that entry: redefining a built-in fails at
startup.

## How roles reach the database

- `migrate` creates every role's group and adds its declared permissions
  (`core.roles.ensure_roles`, a `post_migrate` receiver). No migration file.
- Declared permissions are a floor. Permissions an admin adds to a role's group
  by hand are kept.
- `manage.py sync_roles` does the same on demand and re-derives `is_staff` for
  every user. `--dry-run` reports without writing.

## `is_staff` is derived

For a non-superuser, `is_staff` and staff-role membership are kept in step.
The role is the source of truth; the flag follows it:

- **Group changes** on either side of the relation (`user.groups.add(...)`,
  `group.user_set.remove(...)`, `clear()`, `set()`) re-derive the affected
  users. Deleting a group re-derives its former members.
- **Creating a user with `is_staff=True`** (`create_user(..., is_staff=True)`)
  adopts it into `staff`, so it is a whole staff account (flag and role), not a
  half one.
- **Saving a user whose flag this copy turned on** (`user.is_staff = True;
  user.save()`) adopts it the same way. **Turning it off** while the user holds
  a staff role revokes the staff roles. An explicit change wins.
- **Any other save** re-derives the flag from the roles and logs a warning when
  that reverts it. This covers a stale in-memory copy saved after its role was
  removed, a superuser flag removed from an account that never held a role, and
  a flag written around the ORM. A save whose `update_fields` names neither
  flag is skipped. The login's `last_login` update is one such save.
- The Django admin shows `is_staff` read-only. Grant access by adding the user
  to a staff role's group. The built-in groups cannot be deleted or renamed
  there. The ERP "Create user" form adds new users to `staff`.
- `is_superuser` is independent break-glass. Derivation never touches a
  superuser, so a superuser can always get in to repair roles.

**One answer for every staff gate.** `/erp/` (`StaffOnlyERPMiddleware`, and
`StaffRequiredMixin` in `core/views/account.py`), the plugin URL gate
(`core/plugins/url_auth.py`), the catalog's staff checks and the staff API
(`core.permissions.PrincipalPermission`) all call
`core.permissions.is_staff_principal`. It returns true for a superuser, or for
an active user holding a staff role and the flag. A flag without a role, or a
role without the flag, is refused everywhere until the two agree. That state
can come from a renamed group, a `GOLDEN_ROLES` edit or a membership row
written around the ORM. This was chosen over re-deriving eagerly on
`post_migrate` because it is right at request time, between deploys, at the
cost of one `EXISTS` query per staff request. Django admin and `IsAdminUser`
endpoints still read the flag alone.

## First deploy

Golden creates role groups through one helper, `core.roles.ensure_role`. That
helper is used by `migrate`, `sync_roles`, `grant_role`, `make_staff_user` and
`setup_demo_user`. Whichever of them creates the `staff` group first adds every
existing `is_staff` non-superuser to it, so nobody loses access and a race
between `setup_demo_user` and the entrypoint's `migrate` is harmless.

`manage.py sync_roles --dry-run` prints the same plan the real run would carry
out.

### A group with a role's name already exists

If a deployment already has a group named like a `staff=True` role (for
example its own `staff` group), its members would become back-office staff.
When any such member lacks `is_staff`, `migrate` (and `sync_roles`) **fails**
with `ImproperlyConfigured` and lists them, instead of silently promoting them.
Resolve it one of these ways:

- rename the group, or remove those members, and migrate again; or
- take the group over: set `GOLDEN_ROLES_ADOPT_PREEXISTING = True` (or run
  `manage.py sync_roles --accept-preexisting`). Its members are promoted and
  current `is_staff` users are adopted into `staff`.

The same refusal fires if a staff-role member ever loses its flag through a
write around the ORM. `core.W102` lists such members.

If the group existed and held only staff, nothing fails and nobody is adopted
automatically. Those flagged users are refused everywhere until adopted, and
`migrate` prints them as a `Roles: WARNING` line. Adopt them by hand:

```sh
manage.py check --database default        # core.W101 lists staff users with no role
manage.py sync_roles --adopt-existing --dry-run
manage.py sync_roles --adopt-existing
```

## System checks

These run with `migrate` and `manage.py check --database default`.

| Id | Meaning |
|---|---|
| `core.W101` | Staff user with no staff role. Derivation would demote it. Silent until the `staff` group exists, because creating it adopts them. |
| `core.W102` | Staff-role member without `is_staff`. `migrate`/`sync_roles` refuse until it is resolved (see above). |
| `core.W103` | A role's group does not exist yet. |
| `core.W104` | A declared permission does not exist. |

## Tests

Build staff actors with `core.tests.helpers.make_staff_user(...)`, which adds
the user to `staff` (or `role=...`). `create_user(is_staff=True)` also works,
because creation adopts it. To build a pre-roles account (flag, no role), create
the user and then `User.objects.filter(pk=...).update(is_staff=True)`.
