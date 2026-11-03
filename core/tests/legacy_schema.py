"""Test support for code that runs on the pre-step-8 schema.

Identity step 8 dropped ``Customer.supabase_user_id`` and
``UserProfile.supabase_uid``. The data migrations that read them
(``customers.0004``, ``0006``, ``0008``, via ``core.identity.backfill`` and
``core.identity.retire``) take a historical ``apps`` registry, and the test
settings sync the database from the current models (no migrations). So
:class:`LegacyColumnsTestCase` puts the two columns back on the test tables
for the duration of the class, and ``self.H`` is the historical registry at
``customers.0008`` (both columns, plus ``legacy_external_id``).

Rows are created with the live models (the columns are nullable) and the
legacy values are set and read through ``self.H`` managers.
"""

from __future__ import annotations

import functools

from django.db import connection, models
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase, override_settings

LEGACY_NODE = ("customers", "0008_identity_retire_precheck")


@functools.cache
def legacy_apps():
    """Historical registry with both legacy columns (state at ``LEGACY_NODE``)."""
    with override_settings(MIGRATION_MODULES={}):
        loader = MigrationLoader(None, ignore_no_migrations=True)
    return loader.project_state(LEGACY_NODE).apps


def _legacy_fields():
    from core.models import UserProfile
    from customers.models import Customer

    # Plain nullable columns: on SQLite that is an ALTER TABLE ADD/DROP COLUMN,
    # not a table rebuild. The unique constraints are not under test here.
    uid = models.CharField(max_length=255, null=True, blank=True)
    uid.set_attributes_from_name("supabase_uid")
    uid.model = UserProfile
    sub = models.UUIDField(null=True, blank=True)
    sub.set_attributes_from_name("supabase_user_id")
    sub.model = Customer
    return [(UserProfile, uid), (Customer, sub)]


class LegacyColumnsTestCase(TestCase):
    """A ``TestCase`` whose tables carry the two pre-step-8 legacy columns."""

    @classmethod
    def setUpClass(cls):
        # Outside the class transaction: SQLite cannot alter a table inside one.
        with connection.schema_editor() as editor:
            for model, field in _legacy_fields():
                editor.add_field(model, field)
        cls.H = legacy_apps()
        try:
            super().setUpClass()
        except Exception:
            cls._drop_legacy_columns()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            cls._drop_legacy_columns()

    @classmethod
    def _drop_legacy_columns(cls):
        with connection.schema_editor() as editor:
            for model, field in _legacy_fields():
                editor.remove_field(model, field)

    # -- legacy values, through the historical models -------------------------

    def legacy(self, app, model):
        return self.H.get_model(app, model)._base_manager

    def set_customer_uid(self, customer, value):
        self.legacy("customers", "Customer").filter(pk=customer.pk).update(supabase_user_id=value)

    def customer_uid(self, customer):
        return self.legacy("customers", "Customer").filter(pk=customer.pk).values_list(
            "supabase_user_id", flat=True).get()

    def set_profile_uid(self, profile, value):
        self.legacy("core", "UserProfile").filter(pk=profile.pk).update(supabase_uid=value)

    def profile_uid(self, profile):
        return self.legacy("core", "UserProfile").filter(pk=profile.pk).values_list(
            "supabase_uid", flat=True).get()

    def make_profile(self, uid=None, **fields):
        from core.models import UserProfile

        profile = UserProfile.objects.create(**fields)
        if uid is not None:
            self.set_profile_uid(profile, uid)
        return profile
