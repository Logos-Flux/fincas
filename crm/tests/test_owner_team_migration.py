"""CRM Phase 4 step 4: the ``owner_team`` data steps on the historical registry.

``parties.0005_stamp_owner_team`` and ``crm.0016_stamp_owner_team`` run
``core.teams.backfill_owner_teams`` with the ``apps`` of their own migration
state (the ``core/tests/legacy_schema.py`` pattern: the test settings build
tables from the live models, so the migration functions are called with a
historical registry loaded by ``MigrationLoader``). Rows written around
``save()`` start unstamped; the step stamps them, a second run changes
nothing, and the reverse is a no-op.
"""

from __future__ import annotations

import functools
import importlib

from django.db.migrations.loader import MigrationLoader
from django.test import override_settings

from core.models import Team
from crm.models import Engagement
from parties.models import Party

from .helpers import CrmTestCase, organisation, profile

PARTY_STEP = ("parties", "0005_stamp_owner_team")
ENGAGEMENT_STEP = ("crm", "0016_stamp_owner_team")


@functools.cache
def historical_apps(node):
    with override_settings(MIGRATION_MODULES={}):
        loader = MigrationLoader(None, ignore_no_migrations=True)
    return loader.project_state(node).apps


def step(node):
    return importlib.import_module(f"{node[0]}.migrations.{node[1]}")


class OwnerTeamDataStepTests(CrmTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.west = Team.objects.create(company=cls.company, key="west", name="West")
        cls.rep.team = cls.west
        cls.rep.save()
        cls.loner = profile(cls.company, "loner")

    def unstamped(self):
        """Owned rows as a pre-step database has them: owner set, no team."""
        party = organisation(self.company, "Legacy Co", owner=self.rep)
        loner_party = organisation(self.company, "Loner Co", owner=self.loner)
        eng = self.engagement(owner=self.rep)
        free = self.engagement(owner=None)
        Party._base_manager.update(owner_team=None)
        Engagement._base_manager.update(owner_team=None)
        return party, loner_party, eng, free

    def team_of(self, obj):
        return type(obj)._base_manager.values_list("owner_team_id", flat=True).get(pk=obj.pk)

    def test_forwards_stamps_idempotently_and_reverse_is_a_noop(self):
        party, loner_party, eng, free = self.unstamped()
        for node in (PARTY_STEP, ENGAGEMENT_STEP):
            step(node).forwards(historical_apps(node), None)
        expected = [self.west.pk, None, self.west.pk, None]
        rows = [party, loner_party, eng, free]
        self.assertEqual([self.team_of(r) for r in rows], expected)
        for node in (PARTY_STEP, ENGAGEMENT_STEP):  # idempotent
            step(node).forwards(historical_apps(node), None)
        self.assertEqual([self.team_of(r) for r in rows], expected)
        for node in (PARTY_STEP, ENGAGEMENT_STEP):
            migration = step(node).Migration
            operation = migration.operations[0]
            self.assertTrue(operation.reversible)
            operation.reverse_code(historical_apps(node), None)
        self.assertEqual([self.team_of(r) for r in rows], expected)

    def test_a_team_named_on_the_row_is_never_overwritten(self):
        east = Team.objects.create(company=self.company, key="east", name="East")
        eng = self.engagement(owner=self.rep, owner_team=east)
        step(ENGAGEMENT_STEP).forwards(historical_apps(ENGAGEMENT_STEP), None)
        self.assertEqual(self.team_of(eng), east.pk)
