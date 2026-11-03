"""Owners → ``core.UserProfile`` and teams → ``core.Team`` (docs/crm/phase5-importer.md 2.3;
phase4-fields.md 2.1, 2.5).

- **create**: a login-less ``UserProfile`` (``user=None``, the tenant,
  ``display_name`` from the source). The importer never creates a login, a
  password or a role for anyone: a profile with no user is "archived" to
  ``core.staff.is_archived``, which keeps it out of new assignments while it
  stays the name on history.
- **link** (one active staff login with the owner's email, 2.3): only the
  ExternalRefs. The staff member's profile, team and login are not touched.
- **Teams**: the owner's primary team becomes a ``core.Team`` of the tenant,
  found by its ExternalRef, else by key (``[teams."<id>"] key = …`` in the
  mapping file, else ``<provider>_<id>``), else created; a created profile
  gets it as ``team``. All the owner's teams, primary or not, are kept on the
  owner's ExternalRef (``metadata["teams"]``, 2.3) so nothing is lost.

``UserProfile`` has no sidecar: an owner's source properties are kept on its
ExternalRef too (``metadata["source_properties"]``).
"""

from __future__ import annotations

import re

from .base import INVALID_VALUE, Row, RowError, applied_ref

_KEY_CHARS = re.compile(r"[^a-z0-9_]")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def team_key(row: Row, team: dict) -> str:
    mapped = ((row.mapping.get("teams") or {}).get(str(team.get("source_id"))) or {}).get("key")
    key = mapped or f"{row.provider}_{_KEY_CHARS.sub('_', str(team.get('source_id')).lower())}"[:32]
    if not KEY_RE.match(key):
        raise RowError(INVALID_VALUE, f"team key {key!r} does not match [a-z][a-z0-9_]{{0,31}}.")
    return key


def ensure_team(row: Row, team: dict):
    from core.models import Team

    found = row.resolve({"object_type": "team", "source_id": team.get("source_id")}, Team, required=False)
    if found is None:
        key = team_key(row, team)
        found = Team.objects.filter(company_id=row.company.pk, key=key).first()
        if found is None:
            found = Team(company_id=row.company.pk, key=key, name=(team.get("name") or key)[:100])
            found.save()
            row.session.create(found)
        row.ref_for(found, team.get("source_id"), object_type="team")
    return found


def _ref_metadata(row: Row) -> dict:
    meta = {}
    teams = [{"source_id": str(t.get("source_id")), "name": t.get("name") or "", "primary": bool(t.get("primary"))}
             for t in row.data.get("teams") or []]
    if teams:
        meta["teams"] = teams
    if row.properties:
        meta["source_properties"] = dict(row.properties)
    return meta


def apply(row: Row) -> str:
    from core.models import UserProfile

    teams = row.data.get("teams") or []
    primary = next((t for t in teams if t.get("primary")), teams[0] if teams else None)
    if row.decision == "create":
        team = ensure_team(row, primary) if primary else None
        profile = UserProfile(user=None, company_id=row.company.pk, team=team,
                              display_name=(row.data.get("display_name") or "")[:255])
        profile.save()
        row.session.create(profile)
    elif row.decision in ("link", "update"):
        profile = row.target(UserProfile)
        if primary:
            ensure_team(row, primary)  # the team exists for later owners; the staff profile keeps its own
    else:
        raise RowError(INVALID_VALUE, f"an owner row cannot be applied as {row.decision!r}.")
    meta = _ref_metadata(row)
    for key in row.keys:
        row.ref_for(profile, key, metadata=meta)
    return applied_ref(UserProfile, profile.pk)
