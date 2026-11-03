"""Shared fixtures for the Phase 3 communication tests."""

from __future__ import annotations

from django.apps import apps
from django.db import IntegrityError, connection, models, transaction
from django.test import TestCase
from django.utils import timezone

from core.models import Company
from crm.tests.helpers import organisation, person, profile
from parties.models import PartyEmail, PartyPhone

NOW = timezone.now()


def email(party, address, **kwargs):
    kwargs.setdefault("purpose", "work")
    return PartyEmail.objects.create(party=party, email=address, **kwargs)


def phone(party, number, **kwargs):
    kwargs.setdefault("purpose", "work")
    return PartyPhone.objects.create(party=party, number_raw=number, **kwargs)


class CommTestCase(TestCase):
    """Two tenants; Ada (a person with an email and a phone) and Acme in tenant A."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other_company = Company.objects.create(name="Tenant B")
        cls.org = organisation(cls.company)
        cls.ada = person(cls.company)
        cls.ada_email = email(cls.ada, "Ada@Example.com")
        cls.ada_phone = phone(cls.ada, "+1 415 555 0100")
        cls.rep = profile(cls.company, "rep")

    def assertViolates(self, name, create):
        with self.assertRaises(IntegrityError) as ctx, transaction.atomic():
            create()
        constraint = next(
            c for model in apps.get_app_config("communications").get_models()
            for c in model._meta.constraints if c.name == name
        )
        if connection.vendor == "postgresql" or isinstance(constraint, models.CheckConstraint):
            self.assertIn(name, str(ctx.exception))
