"""The Communication schema (docs/crm/phase3-engagement.md 2.2).

Every constraint in the ``communications`` app has a
``test_constraint_<name>`` method that violates it
(``test_every_constraint_has_a_violation_test`` enforces that).
"""

from __future__ import annotations

import datetime
from io import StringIO

from django.apps import apps
from django.contrib.admin.sites import site
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import RequestFactory

from communications.models import (
    Communication,
    CommunicationParticipant,
    CommunicationRaw,
    EmailLog,
)
from core.role_registry import STAFF_ROLE
from crm import engagements
from crm.models import Task

from .helpers import NOW, CommTestCase


def comm(company, **kwargs):
    kwargs.setdefault("kind", "call")
    kwargs.setdefault("direction", "outbound")
    kwargs.setdefault("occurred_at", NOW)
    return Communication.objects.create(company=company, **kwargs)


class ConstraintTests(CommTestCase):
    def test_every_constraint_has_a_violation_test(self):
        names = {
            constraint.name
            for model in apps.get_app_config("communications").get_models()
            for constraint in model._meta.constraints
        }
        self.assertEqual(names, {"comm_end_after_start"})
        missing = sorted(n for n in names if not hasattr(self, f"test_constraint_{n}"))
        self.assertEqual(missing, [])

    def test_constraint_comm_end_after_start(self):
        comm(self.company, ended_at=NOW)  # equal is fine
        comm(self.company, ended_at=None)
        self.assertViolates("comm_end_after_start", lambda: comm(
            self.company, ended_at=NOW - datetime.timedelta(seconds=1)))

    def test_email_log_is_one_to_one(self):
        from django.db import IntegrityError, transaction

        log = EmailLog.objects.create(
            document_type="quote", document_id=self.ada.pk, document_number="QT-1",
            recipient_email="ada@example.com", subject="s", template_name="t", status="sent",
            triggered_by="manual",
        )
        comm(self.company, kind="email", email_log=log)
        with self.assertRaises(IntegrityError), transaction.atomic():
            comm(self.company, kind="email", email_log=log)


class TenancyTests(CommTestCase):
    def test_children_copy_company_from_the_communication(self):
        c = comm(self.company)
        part = CommunicationParticipant.objects.create(
            communication=c, company=self.other_company, role="to")
        raw = CommunicationRaw.objects.create(
            communication=c, company=self.other_company, provider="x", schema_version=1,
            payload={"a": 1}, content_hash="0" * 64, fetched_at=NOW)
        self.assertEqual(part.company_id, self.company.pk)
        self.assertEqual(raw.company_id, self.company.pk)

    def test_check_tenancy_covers_communications(self):
        c = comm(self.company, party=self.ada)
        CommunicationParticipant.objects.create(communication=c, role="to", party=self.ada,
                                                party_email=self.ada_email)
        call_command("check_tenancy", "--app", "communications", stdout=StringIO())
        Communication.objects.filter(pk=c.pk).update(company=self.other_company)
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("check_tenancy", "--app", "communications", stdout=StringIO())

    def test_clean_refuses_cross_tenant_links_unknown_kind_and_bad_direction(self):
        c = Communication(company=self.other_company, kind="note", direction="outbound",
                          occurred_at=NOW, party=self.ada, duration_seconds=5)
        with self.assertRaises(ValidationError) as ctx:
            c.full_clean()
        self.assertEqual(set(ctx.exception.message_dict) >= {"party", "direction", "duration_seconds"}, True)
        c = Communication(company=self.company, kind="fax", direction="outbound", occurred_at=NOW)
        with self.assertRaises(ValidationError) as ctx:
            c.full_clean()
        self.assertIn("kind", ctx.exception.message_dict)

    def test_unknown_kind_stays_readable(self):
        c = comm(self.company, kind="voicemail", direction="inbound")
        c.refresh_from_db()
        self.assertEqual(c.kind, "voicemail")
        self.assertEqual(c.kind_entry.icon, "ti ti-message-question")


class TaskCommunicationTests(CommTestCase):
    def test_task_links_a_communication_and_survives_its_deletion(self):
        c = comm(self.company)
        task = Task.objects.create(company=self.company, title="Send the spec", communication=c)
        self.assertEqual(list(c.tasks.all()), [task])
        c.delete()
        task.refresh_from_db()
        self.assertIsNone(task.communication_id)

    def test_task_clean_refuses_another_tenants_communication(self):
        c = comm(self.other_company)
        task = Task(company=self.company, title="x", communication=c)
        with self.assertRaises(ValidationError) as ctx:
            task.full_clean()
        self.assertIn("communication", ctx.exception.message_dict)

    def test_check_engagements_reports_a_cross_tenant_task_communication(self):
        from django.core.management.base import CommandError

        c = comm(self.other_company)
        task = Task.objects.create(company=self.company, title="x")
        Task.objects.filter(pk=task.pk).update(communication=c)
        with self.assertRaises(CommandError):
            call_command("check_engagements", stdout=StringIO())

    def test_engagement_reverse_relation(self):
        eng = engagements.create(company=self.company, party=self.org)
        c = comm(self.company, engagement=eng)
        self.assertEqual(list(eng.communications.all()), [c])


class AdminTests(CommTestCase):
    def test_admin_is_read_only(self):
        request = RequestFactory().get("/")
        from django.contrib.auth import get_user_model

        request.user = get_user_model().objects.create_superuser("root", password="x")
        for model in (Communication, CommunicationParticipant, CommunicationRaw):
            admin = site._registry[model]
            self.assertFalse(admin.has_add_permission(request))
            self.assertFalse(admin.has_change_permission(request))
            self.assertFalse(admin.has_delete_permission(request))

    def test_raw_needs_its_own_permission_which_staff_does_not_carry(self):
        from django.contrib.auth import get_user_model

        perm = Permission.objects.get(content_type__app_label="communications",
                                      codename="view_raw_communication")
        staff_group = Group.objects.filter(name=STAFF_ROLE).first()
        if staff_group is not None:
            self.assertNotIn(perm, staff_group.permissions.all())
        user = get_user_model().objects.create_user("clerk", password="x", is_staff=True)
        request = RequestFactory().get("/")
        request.user = user
        admin = site._registry[CommunicationRaw]
        self.assertFalse(admin.has_view_permission(request))
        user.user_permissions.add(perm)
        request.user = get_user_model().objects.get(pk=user.pk)
        self.assertTrue(admin.has_view_permission(request))
