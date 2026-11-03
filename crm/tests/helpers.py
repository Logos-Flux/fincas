"""Shared fixtures for the Phase 3 engagement tests."""

from __future__ import annotations

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import IntegrityError, connection, models, transaction
from django.test import TestCase

from core.models import Company, UserProfile
from crm import engagements
from crm.models import Pipeline
from parties.models import Party


def person(company, given="Ada", family="Lovelace", **kwargs):
    return Party.objects.create(
        company=company, kind=Party.Kind.PERSON, given_name=given, family_name=family,
        origin=Party.Origin.ERP, **kwargs,
    )


def organisation(company, name="Acme Widgets", **kwargs):
    return Party.objects.create(
        company=company, kind=Party.Kind.ORGANISATION, display_name=name,
        origin=Party.Origin.ERP, **kwargs,
    )


def profile(company, username, *, timezone="", perms=()):
    user = get_user_model().objects.create_user(username, password="x")
    for perm in perms:
        app_label, codename = perm.split(".")
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename)
        )
    return UserProfile.objects.create(
        user=user, company=company, display_name=username, timezone=timezone,
    )


class CrmTestCase(TestCase):
    """Two tenants, each seeded with golden's ``sales`` pipeline on create."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other_company = Company.objects.create(name="Tenant B")
        cls.pipeline = Pipeline.objects.get(company=cls.company, key="sales")
        cls.other_pipeline = Pipeline.objects.get(company=cls.other_company, key="sales")
        cls.org = organisation(cls.company)
        cls.ada = person(cls.company)
        cls.rep = profile(cls.company, "rep")

    def stage(self, key, pipeline=None):
        return (pipeline or self.pipeline).stages.get(key=key)

    def engagement(self, **kwargs):
        kwargs.setdefault("company", self.company)
        kwargs.setdefault("party", self.org)
        return engagements.create(**kwargs)

    def assertViolates(self, name, create):
        """``create()`` raises IntegrityError, naming ``name`` where the backend does."""
        with self.assertRaises(IntegrityError) as ctx, transaction.atomic():
            create()
        # Postgres names every constraint; SQLite names CHECK constraints only.
        names_constraint = connection.vendor == "postgresql" or isinstance(
            self._constraint(name), models.CheckConstraint
        )
        if names_constraint:
            self.assertIn(name, str(ctx.exception))

    @staticmethod
    def _constraint(name):
        for model in apps.get_app_config("crm").get_models():
            for constraint in model._meta.constraints:
                if constraint.name == name:
                    return constraint
        raise AssertionError(f"no constraint named {name!r} in crm")
