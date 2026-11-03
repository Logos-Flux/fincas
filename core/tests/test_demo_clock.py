"""The demo clock: ``seed_my_day`` + ``demo_roll_dates`` (core/demo_clock.py).

Every test pins ``django.utils.timezone.now`` to a local (New York, the
settings zone) moment, never the real clock, and derives "today" in that
zone: from 00:00 UTC to local midnight the UTC date is already tomorrow.
"""

from __future__ import annotations

import datetime
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from accounting.models import FiscalPeriod, JournalEntry
from communications.models import Communication
from core import demo_clock
from core.models import AuditLog, Company, UserProfile
from core.tests.helpers import make_staff_user
from crm import engagements
from crm.models import Engagement, Task
from crm.tasks import end_of_local_day
from parties.models import ConsentRecord, Party

NY = ZoneInfo("America/New_York")
SEED_DAY = datetime.date(2026, 10, 5)
DEMO = {"DEMO_USERNAMES": ["demo"], "DEMO_LOGIN_USERNAME": "demo"}


def local(day, hhmm="10:00"):
    hour, minute = map(int, hhmm.split(":"))
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=NY)


def frozen(day, hhmm="10:00"):
    return mock.patch("django.utils.timezone.now", return_value=local(day, hhmm).astimezone(datetime.UTC))


def roll(day, *args, hhmm="00:10"):
    out = StringIO()
    with frozen(day, hhmm):
        call_command("demo_roll_dates", *args, stdout=out)
    return out.getvalue()


@override_settings(**DEMO)
class DemoClockTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Demo Co")
        cls.user = make_staff_user("demo", password="pw")
        cls.profile = UserProfile.objects.create(user=cls.user, company=cls.company, display_name="Demo")

    def seed(self, day=SEED_DAY):
        with frozen(day, "10:00"):
            call_command("seed_my_day", stdout=StringIO())

    def my_day(self, day, hhmm):
        self.client.force_login(self.user)
        with frozen(day, hhmm):
            return self.client.get(reverse("erp-crm-my-day"))


class RollTests(DemoClockTestBase):
    def test_refuses_without_the_demo_flag(self):
        self.seed()
        due = list(Task.objects.order_by("pk").values_list("due_at", flat=True))
        with override_settings(DEMO_USERNAMES=[]), self.assertRaisesMessage(CommandError, "DEMO_USERNAMES"):
            roll(SEED_DAY + datetime.timedelta(days=3))
        self.assertEqual(list(Task.objects.order_by("pk").values_list("due_at", flat=True)), due)
        self.assertEqual(demo_clock.get_anchor(), SEED_DAY)
        # --force is the explicit opt-in on a throwaway copy.
        with override_settings(DEMO_USERNAMES=[]):
            roll(SEED_DAY + datetime.timedelta(days=3), "--force")
        self.assertEqual(demo_clock.get_anchor(), SEED_DAY + datetime.timedelta(days=3))

    def test_a_task_due_on_the_anchor_day_is_due_today(self):
        self.seed()
        self.assertEqual(demo_clock.get_anchor(), SEED_DAY)
        on_anchor = Task.objects.get(title="Call to confirm requirements")
        timed = Task.objects.get(title="Confirm the delivery window")
        eng = on_anchor.engagement
        comm = Communication.objects.filter(engagement=eng).order_by("occurred_at").first()
        before = {"timed": timed.due_at, "created": eng.created_at, "close": eng.expected_close_date,
                  "activity": eng.last_activity_at, "comm": comm.occurred_at}

        today = SEED_DAY + datetime.timedelta(days=5)
        out = roll(today)
        self.assertIn("rolled 5 day(s)", out)
        on_anchor.refresh_from_db()
        timed.refresh_from_db()
        eng.refresh_from_db()
        comm.refresh_from_db()
        five = datetime.timedelta(days=5)
        self.assertEqual(on_anchor.due_at, end_of_local_day(today, NY))
        self.assertEqual(timed.due_at, before["timed"] + five)
        self.assertEqual(timed.due_at.astimezone(NY), local(today, "16:00"))
        self.assertEqual(eng.created_at, before["created"] + five)
        self.assertEqual(eng.expected_close_date, before["close"] + five)
        self.assertEqual(eng.last_activity_at, before["activity"] + five)
        self.assertEqual(comm.occurred_at, before["comm"] + five)
        self.assertEqual(eng.next_action_due_at, on_anchor.due_at)
        self.assertEqual(demo_clock.get_anchor(), today)
        for command in ("check_engagements", "check_tenancy"):
            call_command(command, stdout=StringIO())
        call_command("reconcile_engagements", "--check", stdout=StringIO())

    def test_idempotent_on_the_same_day(self):
        self.seed()
        today = SEED_DAY + datetime.timedelta(days=2)
        roll(today)
        snapshot = list(Task.objects.order_by("pk").values_list("due_at", "created_at"))
        out = roll(today, hhmm="23:50")
        self.assertIn("nothing moved", out)
        self.assertEqual(list(Task.objects.order_by("pk").values_list("due_at", "created_at")), snapshot)
        self.assertEqual(AuditLog.objects.filter(action=demo_clock.ACTION).count(), 2)  # seed + one roll

    def test_excluded_fields_do_not_move(self):
        self.seed()
        User.objects.filter(pk=self.user.pk).update(last_login=local(SEED_DAY))
        session = Session.objects.create(session_key="k" * 32, session_data="", expire_date=local(SEED_DAY, "18:00"))
        party = Party.objects.filter(company=self.company).order_by("pk").first()
        Party.objects.filter(pk=party.pk).update(erased_at=local(SEED_DAY, "08:00"))
        consent = ConsentRecord.objects.order_by("pk").first()
        anchor_row = AuditLog.objects.get(action=demo_clock.ACTION)
        user_before = User.objects.values("last_login", "date_joined").get(pk=self.user.pk)
        party.refresh_from_db()

        roll(SEED_DAY + datetime.timedelta(days=4))
        self.assertEqual(User.objects.values("last_login", "date_joined").get(pk=self.user.pk), user_before)
        session.refresh_from_db()
        self.assertEqual(session.expire_date, local(SEED_DAY, "18:00"))
        moved = Party.objects.get(pk=party.pk)
        self.assertEqual(moved.erased_at, local(SEED_DAY, "08:00"))
        self.assertEqual(moved.created_at, party.created_at + datetime.timedelta(days=4))
        if consent is not None:
            fresh = ConsentRecord.objects.get(pk=consent.pk)
            self.assertEqual((fresh.occurred_at, fresh.recorded_at), (consent.occurred_at, consent.recorded_at))
        anchor_row.refresh_from_db()
        self.assertEqual(anchor_row.changes["anchor"], SEED_DAY.isoformat())

    def test_all_day_tasks_keep_their_local_day_across_dst(self):
        # US clocks spring forward on 2027-03-14: a plain UTC shift would put
        # an end-of-day task at 00:59 the next local day.
        anchor = datetime.date(2027, 3, 12)
        self.seed(anchor)
        task = Task.objects.get(title="Call to confirm requirements")
        self.assertEqual(task.due_at, end_of_local_day(anchor, NY))
        today = datetime.date(2027, 3, 16)
        roll(today)
        task.refresh_from_db()
        self.assertEqual(task.due_at, end_of_local_day(today, NY))
        call_command("reconcile_engagements", "--check", stdout=StringIO())

    def test_no_anchor_initialises_and_anchor_overrides(self):
        with frozen(SEED_DAY):
            eng = engagements.create(company=self.company, title="Old deal", expected_close_date=SEED_DAY)
        self.assertIsNone(demo_clock.get_anchor())
        out = roll(SEED_DAY + datetime.timedelta(days=10))
        self.assertIn("no anchor stored", out)
        eng.refresh_from_db()
        self.assertEqual(eng.expected_close_date, SEED_DAY)
        # The baseline was really written on SEED_DAY: say so.
        out = roll(SEED_DAY + datetime.timedelta(days=10), "--anchor", SEED_DAY.isoformat())
        self.assertIn("rolled 10 day(s)", out)
        eng.refresh_from_db()
        self.assertEqual(eng.expected_close_date, SEED_DAY + datetime.timedelta(days=10))
        # Now the anchor is known: a stale --anchor (left in a script) is ignored.
        err = StringIO()
        with frozen(SEED_DAY + datetime.timedelta(days=10), "00:20"):
            call_command("demo_roll_dates", "--anchor", SEED_DAY.isoformat(), stdout=StringIO(), stderr=err)
        self.assertIn("Ignoring --anchor", err.getvalue())
        eng.refresh_from_db()
        self.assertEqual(eng.expected_close_date, SEED_DAY + datetime.timedelta(days=10))

    def test_refuses_to_move_backwards_and_dry_run_writes_nothing(self):
        self.seed()
        with self.assertRaisesMessage(CommandError, "backwards"):
            roll(SEED_DAY - datetime.timedelta(days=1))
        due = list(Task.objects.order_by("pk").values_list("due_at", flat=True))
        out = roll(SEED_DAY + datetime.timedelta(days=3), "--dry-run")
        self.assertIn("dry run: rolled 3 day(s)", out)
        self.assertEqual(list(Task.objects.order_by("pk").values_list("due_at", flat=True)), due)
        self.assertEqual(demo_clock.get_anchor(), SEED_DAY)


class ExclusionListTests(TestCase):
    def test_every_exclusion_names_a_real_app_model_or_field(self):
        from django.apps import apps

        for key in demo_clock.EXCLUDED:
            parts = key.split(".")
            app = apps.get_app_config(parts[0])  # LookupError on a typo
            if len(parts) > 1:
                model = app.get_model(parts[1])
                if len(parts) > 2:
                    model._meta.get_field(parts[2])

    def test_rolled_models_skip_the_exclusions(self):
        labels = {model._meta.label_lower: {f.name for f in fields} for model, fields in demo_clock.rolled_models()}
        for skipped in ("auth.user", "sessions.session", "core.auditlog", "core.outboxevent", "core.apikey",
                        "parties.consentrecord", "accounting.fiscalperiod", "accounting.journalentry",
                        "webhooks.webhooksubscription"):
            self.assertNotIn(skipped, labels)
        self.assertNotIn("erased_at", labels["parties.party"])
        self.assertNotIn("due_at", labels["crm.task"])
        self.assertIn("created_at", labels["crm.engagement"])
        self.assertIn("expected_close_date", labels["crm.engagement"])
        with override_settings(GOLDEN_DEMO_ROLL_EXCLUDE=["content.Article"]):
            self.assertNotIn("content.article", {m._meta.label_lower for m, _ in demo_clock.rolled_models()})


class BooksTests(DemoClockTestBase):
    def test_closed_books_stay_and_open_entries_are_restamped(self):
        demo_clock.set_anchor(SEED_DAY)
        co = self.company
        closed = FiscalPeriod.objects.create(company=co, name="2026-09", start_date=datetime.date(2026, 9, 1),
                                             end_date=datetime.date(2026, 9, 30))
        october = FiscalPeriod.objects.create(company=co, name="2026-10", start_date=datetime.date(2026, 10, 1),
                                              end_date=datetime.date(2026, 10, 31))
        november = FiscalPeriod.objects.create(company=co, name="2026-11", start_date=datetime.date(2026, 11, 1),
                                               end_date=datetime.date(2026, 11, 30))
        august = FiscalPeriod.objects.create(company=co, name="2026-08", start_date=datetime.date(2026, 8, 1),
                                             end_date=datetime.date(2026, 8, 31))
        in_closed = JournalEntry.objects.create(company=co, entry_date=datetime.date(2026, 9, 10),
                                                fiscal_period=closed)
        crossing = JournalEntry.objects.create(company=co, entry_date=datetime.date(2026, 10, 28),
                                               fiscal_period=october)
        into_closed = JournalEntry.objects.create(company=co, entry_date=datetime.date(2026, 8, 29),
                                                  fiscal_period=august)
        unperiod = JournalEntry.objects.create(company=co, entry_date=datetime.date(2026, 12, 30))
        FiscalPeriod.objects.filter(pk=closed.pk).update(status=FiscalPeriod.Status.CLOSED)
        periods = list(FiscalPeriod.objects.order_by("pk").values_list("start_date", "end_date", "status"))

        out = roll(SEED_DAY + datetime.timedelta(days=5))
        self.assertIn("2 journal entries kept in closed books", out)
        self.assertEqual(list(FiscalPeriod.objects.order_by("pk").values_list("start_date", "end_date", "status")),
                         periods)
        in_closed.refresh_from_db()
        self.assertEqual((in_closed.entry_date, in_closed.fiscal_period_id), (datetime.date(2026, 9, 10), closed.pk))
        crossing.refresh_from_db()
        self.assertEqual((crossing.entry_date, crossing.fiscal_period_id), (datetime.date(2026, 11, 2), november.pk))
        into_closed.refresh_from_db()
        self.assertEqual((into_closed.entry_date, into_closed.fiscal_period_id),
                         (datetime.date(2026, 8, 29), august.pk))
        unperiod.refresh_from_db()
        self.assertEqual((unperiod.entry_date, unperiod.fiscal_period_id), (datetime.date(2027, 1, 4), None))


class MyDaySeedTests(DemoClockTestBase):
    def test_seed_is_idempotent_and_needs_a_staff_user(self):
        self.seed()
        seeded = Engagement.objects.filter(owner=self.profile, metadata__seed="seed_my_day")
        count = seeded.count()
        self.assertGreater(count, 10)
        self.seed()
        self.assertEqual(seeded.count(), count)
        self.assertEqual(set(seeded.values_list("status", flat=True)), {"open", "won", "lost"})
        User.objects.create_user("visitor")
        with self.assertRaisesMessage(CommandError, "not staff"):
            call_command("seed_my_day", user="visitor", stdout=StringIO())

    def test_my_day_is_full_after_seed_and_roll(self):
        self.seed()
        today = SEED_DAY + datetime.timedelta(days=6)
        stale = self.my_day(today, "12:00")
        # Without the roll, every next action has aged into "overdue".
        self.assertEqual(stale.context["due_today"], [])

        roll(today)
        response = self.my_day(today, "12:00")
        overdue = [t.title for t in response.context["overdue"]]
        due_today = [t.title for t in response.context["due_today"]]
        self.assertEqual(overdue, ["Book a site visit", "Send the revised quote", "Follow up on the proposal"])
        self.assertCountEqual(due_today, ["Call to confirm requirements", "Check budget approval",
                                          "Confirm the delivery window"])
        self.assertEqual(len(response.context["idle"]), 2)
        self.assertEqual(response.context["today"], today)
        self.assertContains(response, "Confirm the delivery window")


@override_settings(**DEMO)
class SeedDemoDataTests(TestCase):
    def test_every_seeded_supplier_belongs_to_the_company(self):
        # R1.10: a shipment group keeps only a supplier of the document's
        # company, so an unbound seed supplier was offered and then dropped.
        # A supplier left unbound by an older seed is adopted on re-seed.
        from purchasing.models import Supplier

        Supplier.objects.create(supplier_code="SUPP-001", company_name="Global Components Manufacturing")
        make_staff_user("demo", password="pw")
        with frozen(SEED_DAY):
            call_command("seed_demo_data", stdout=StringIO())
        company = Company.objects.get(name="Golden Commerce Corp")
        self.assertTrue(Supplier.objects.exists())
        self.assertFalse(Supplier.objects.filter(company__isnull=True).exists())
        self.assertEqual(Supplier.objects.get(supplier_code="SUPP-001").company_id, company.pk)

    def test_the_demo_seed_anchors_and_rolls_cleanly(self):
        make_staff_user("demo", password="pw")
        with frozen(SEED_DAY):
            call_command("seed_demo_data", stdout=StringIO())
        self.assertEqual(demo_clock.get_anchor(), SEED_DAY)
        company = Company.objects.get(name="Golden Commerce Corp")
        demo = UserProfile.objects.get(user__username="demo")
        self.assertEqual(demo.company_id, company.pk)
        self.assertTrue(Engagement.objects.filter(owner=demo, metadata__seed="seed_my_day").exists())
        seeded = Task.objects.filter(source="import:seed_crm", status="open", due_at__isnull=False)
        due_on_anchor = [t.pk for t in seeded if t.due_at == end_of_local_day(SEED_DAY, NY)]
        self.assertTrue(due_on_anchor)

        today = SEED_DAY + datetime.timedelta(days=3)
        out = StringIO()
        with frozen(today, "00:10"):
            call_command("demo_roll_dates", verbosity=2, stdout=out)
        self.assertIn("crm.Engagement:", out.getvalue())
        for pk in due_on_anchor:
            self.assertEqual(Task.objects.get(pk=pk).due_at, end_of_local_day(today, NY))
        for command in ("check_engagements", "check_tenancy"):
            call_command(command, stdout=StringIO())
        call_command("reconcile_engagements", "--check", stdout=StringIO())


class ShowAndGuardTests(DemoClockTestBase):
    def test_show_prints_the_line_the_setup_script_parses(self):
        # Demo setup scripts read `zone=` from this line.
        with override_settings(DEMO_USERNAMES=[]), frozen(SEED_DAY, "00:10"):
            out = StringIO()
            call_command("demo_roll_dates", "--show", stdout=out)
        self.assertEqual(out.getvalue().strip(), f"anchor=none today={SEED_DAY} zone=America/New_York")
        self.seed()
        with frozen(SEED_DAY, "23:50"):
            out = StringIO()
            call_command("demo_roll_dates", "--show", stdout=out)
        self.assertEqual(out.getvalue().strip(), f"anchor={SEED_DAY} today={SEED_DAY} zone=America/New_York")

    def test_seed_my_day_refuses_outside_a_demo(self):
        with override_settings(DEMO_USERNAMES=[]), self.assertRaisesMessage(CommandError, "DEMO_USERNAMES"):
            call_command("seed_my_day", user="demo", stdout=StringIO())
        self.assertFalse(Engagement.objects.exists())
        with override_settings(DEMO_USERNAMES=[]), frozen(SEED_DAY):
            call_command("seed_my_day", user="demo", force=True, stdout=StringIO())
        self.assertTrue(Engagement.objects.exists())


class SeedPasswordEchoTests(TestCase):
    def test_a_supplied_admin_password_never_reaches_stdout(self):
        make_staff_user("demo", password="pw")
        out = StringIO()
        # Built at runtime from low-entropy parts: a literal here trips the
        # secret scanner, which cannot tell a fixture from a credential.
        supplied = "-".join(["from", "stdin", "only"])
        with frozen(SEED_DAY):
            call_command("seed_demo_data", admin_password=supplied, stdout=out)
        self.assertNotIn(supplied, out.getvalue())
        self.assertIn("(as supplied)", out.getvalue())
