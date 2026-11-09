# Upgrading Fincas

This page is the contract between Fincas releases and the deployments that run
them: what a version number promises, how long a deprecated feature keeps
working, and how an upgrade's migrations are tested.

## Versions

Fincas follows [semantic versioning](https://semver.org/) for its public
surface, with the usual reading for a 0.x project:

| Release | Example | What it may contain |
| :--- | :--- | :--- |
| **patch** | 0.4.1 → 0.4.2 | bug and security fixes; migrations only when a fix needs one; no removals, no new required settings |
| **minor** | 0.4.x → 0.5.0 | new features, new migrations, new settings with safe defaults, deprecations; removal of anything deprecated in an earlier minor release |
| **major** | 0.x → 1.0 | reserved for a change in the support policy itself |

The **public surface** is what a deployment touches: the settings
(`GOLDEN_*` and the other documented names), the REST API under `/api/v1/`,
webhook payloads (versioned per event, see [webhooks.md](webhooks.md)),
management commands and their exit codes, the plugin hooks and lifecycle
signals in [plugins.md](plugins.md), the import paths those documents name,
and the templates and static files a deployment may override. Anything else
(private helpers, module layout not named in the docs) can change in any
release.

Pre-release tags (`0.5.0rc1`) are for testing an upgrade before it is final;
don't run them in production.

## Deprecation policy

A feature is never removed without warning:

1. The release that deprecates it keeps it working and emits a
   `DeprecationWarning` (Python imports and calls), a system-check warning
   (settings), or a note in the API schema and release notes (API fields). The release notes and
   [CHANGELOG.md](../CHANGELOG.md) list it under **Deprecated**, with what to use
   instead.
2. It keeps working, with the warning, for **at least one minor release**
   after the one that deprecated it: deprecated in 0.4.x, it is still there in
   every 0.5.x release and is removed in 0.6.0 at the earliest.
3. The release that removes it lists it under **Removed**.

To see deprecations in your own code before they bite, run your tests with
`python -W error::DeprecationWarning manage.py test`.

## The migration contract

- **Every release upgrades from the latest patch of the previous minor
  release.** A 0.5.0 database upgrade starts from 0.4.*latest*; to skip a minor
  release, upgrade through each one in turn (install, `migrate`, start, then
  the next).
- **CI tests it.** Besides migrating an empty database, CI migrates a database
  at each earlier release it supports upgrading from, seeds it with that
  release's own code, migrates it with the new release and runs the
  consistency checks (`check_schema_parity`, the engagement and party
  reconcilers). Row-dependent data migrations are exercised this way, not only
  on empty tables.
- **Data migrations use historical models only**, so a database jumping
  several migrations ahead never meets a column that does not exist yet.
- **One-way migrations are listed.** Each release's notes list the migrations
  that do not reverse. Restoring the pre-upgrade backup is the rollback for
  those; for the rest, `migrate <app> <previous migration>` works.

## How to upgrade

1. **Pin an exact version** in your requirements: `fincas==0.4.4` (or
   `fincas[prod]==0.4.4`). Never let a resolver pick up a new Fincas on its
   own.
2. **Read the release notes** for every version between yours and the target
   (the GitHub release, or `docs/releases/` in the repository): "Before you
   upgrade" lists new required secrets, settings and one-way migrations.
3. **Back up the database** ([deployment.md](deployment.md#backups-and-restore)).
4. **Install the new pin**, run `python manage.py migrate --noinput`, and
   restart the web processes and the outbox worker. If the release adds a
   required secret, `migrate` refuses (`core.E060`) until it is set: generate
   it with `manage.py generate_secrets --only <NAME>`.
5. **Check it**: `python manage.py check --deploy`,
   `python manage.py smoke_cutover`, and for an upgrade that touched money
   `python manage.py check_money` ([deployment.md](deployment.md#operator-commands)).

If you override templates or static files, diff the packaged originals
between the two versions: an overridden template does not pick up upstream
changes.

## One company per instance

Every upgrade assumes one `core.Company` per database. `migrate` refuses while
more than one exists (`core.E070`); see
[deployment.md](deployment.md#one-instance-per-company).
