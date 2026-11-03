"""List-page accessibility, CRM Phase 4 step 0 (phase4-fields.md 5.7).

Filter selects get an Apply button and auto-submit for pointer users only;
the column gear has an accessible name; tables carry a visually hidden
caption naming the list, its search, filters and sort; the result count sits
in a polite live region. Every ``ERPListView`` page is checked, so a new list
inherits the gate.
"""

from __future__ import annotations

import re

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import get_resolver, reverse

from core.models import Company, UserProfile
from core.views.list_view import ERPListView
from customers.models import Customer


def _form(page, form_id):
    start = page.index(f'id="{form_id}"')
    return page[start : page.index("</form>", start)]


def _caption(page):
    match = re.search(r'<caption class="visually-hidden">(.*?)</caption>', page, re.S)
    return match.group(1).strip() if match else None


def _status(page):
    return re.search(r'<p class="visually-hidden" id="erp-list-status"[^>]*>', page)


class _StaffCase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme A11y")
        self.user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        UserProfile.objects.create(user=self.user, company=self.company)
        b2b = Customer.CustomerType.choices[0]
        self.type_value, self.type_label = b2b
        for first, last, email in (("Ada", "Lovelace", "ada@example.com"),
                                    ("Alan", "Turing", "alan@example.com")):
            Customer.objects.create(first_name=first, last_name=last, email=email,
                                    customer_type=self.type_value, company=self.company)
        self.client.force_login(self.user)


class GenericListTests(_StaffCase):
    def test_filters_share_one_form_with_apply_and_pointer_only_auto_submit(self):
        page = self.client.get(reverse("erp-customer-list"),
                               {"q": "Ada", "sort": "email", "dir": "desc"}).content.decode()
        self.assertNotIn('onchange="this.form.submit()"', page)
        form = _form(page, "erp-list-filters")
        self.assertIn('name="q"', form)
        self.assertEqual(form.count("data-auto-submit"), 2)  # both filters
        self.assertRegex(form, r'<button type="submit"[^>]*>Apply</button>')
        # The sort survives a filter change.
        self.assertIn('<input type="hidden" name="sort" value="email">', form)
        self.assertIn('<input type="hidden" name="dir" value="desc">', form)
        # The pointer-only handler is on the page.
        self.assertIn('select[data-auto-submit]', page)
        self.assertIn('"pointerdown"', page)

    def test_gear_button_has_an_accessible_name_and_controls_the_panel(self):
        # Step 5 (5.3, 5.7): the chooser is a native <details> disclosure, so
        # the summary is the named, keyboard-operable button and the browser
        # owns expanded/collapsed state.
        page = self.client.get(reverse("erp-customer-list")).content.decode()
        chooser = re.search(r'<details [^>]*id="column-chooser"[^>]*>.*?</details>', page, re.S).group(0)
        summary = re.search(r"<summary [^>]*>", chooser).group(0)
        self.assertIn('aria-label="Configure columns"', summary)
        self.assertIn('<form method="post"', chooser)
        self.assertIn('e.key !== "Escape"', page)
        self.assertNotIn("onclick=", page)

    def test_caption_names_list_search_filter_and_sort(self):
        page = self.client.get(reverse("erp-customer-list"), {
            "q": "Ada", "customer_type": self.type_value, "sort": "email", "dir": "desc",
        }).content.decode()
        caption = _caption(page)
        self.assertIsNotNone(caption)
        self.assertTrue(caption.startswith("Customers"), caption)
        self.assertIn("search \u201cAda\u201d", caption)
        self.assertIn(f"filtered by Type: {self.type_label}", caption)
        self.assertIn("sorted by Email, descending", caption)

    def test_unfiltered_caption_is_just_the_list(self):
        caption = _caption(self.client.get(reverse("erp-customer-list")).content.decode())
        self.assertEqual(caption, "Customers")

    def test_caption_escapes_the_search_text(self):
        page = self.client.get(reverse("erp-customer-list"),
                               {"q": "<script>x</script>"}).content.decode()
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", _caption(page))

    def test_result_count_is_in_a_polite_live_region(self):
        page = self.client.get(reverse("erp-customer-list"), {"q": "Ada"}).content.decode()
        status = _status(page)
        self.assertIsNotNone(status)
        tag = status.group(0)
        self.assertIn('role="status"', tag)
        self.assertIn('aria-live="polite"', tag)
        self.assertIn("data-announce", tag)  # a search result is announced
        self.assertRegex(page, r'id="erp-list-status"[^>]*>1 result</p>')

    def test_unfiltered_list_count_is_not_announced(self):
        page = self.client.get(reverse("erp-customer-list")).content.decode()
        self.assertNotIn("data-announce", _status(page).group(0))
        self.assertRegex(page, r'id="erp-list-status"[^>]*>2 results</p>')

    def test_unknown_filter_value_is_shown_as_given(self):
        class V(ERPListView):
            model = Customer
            list_columns = [{"field": "email", "label": "Email"}]

        view = V()
        view.request = RequestFactory().get("/", {"t": "zzz"})
        status = view.get_list_status(
            {"page_title": "People", "object_list": []},
            [{"field": "t", "label": "Type", "choices": [("a", "A")]}], {"t": "zzz"}, "", "asc",
        )
        self.assertEqual(status["list_caption"], "People, filtered by Type: zzz")
        self.assertEqual(status["list_status"], "0 results")
        self.assertTrue(status["list_filtered"])


class SortableColumnOptOutTests(TestCase):
    def test_a_column_can_opt_out_of_sorting(self):
        class V(ERPListView):
            model = Customer
            list_columns = [{"field": "email", "label": "Email", "sortable": False},
                            {"field": "last_name", "label": "Last"}]

        view = V()
        view.request = RequestFactory().get("/", {"sort": "email"})
        self.assertEqual(set(view.get_sortable_fields()), {"last_name"})
        self.assertEqual(view.get_sort(), ("", "asc"))  # ignored, default order


class PartyListTests(_StaffCase):
    def test_party_list_has_caption_live_region_and_pointer_only_filters(self):
        from parties.models import Party

        Party.objects.create(company=self.company, kind=Party.Kind.PERSON,
                             display_name="Ada Lovelace")
        page = self.client.get(reverse("erp-party-list"), {
            "q": "Ada", "kind": "person", "sort": "display_name", "dir": "asc",
        }).content.decode()
        self.assertNotIn('onchange="this.form.submit()"', page)
        form = _form(page, "party-filters")
        self.assertIn("data-auto-submit", form)
        self.assertRegex(form, r'<button type="submit"[^>]*>Apply</button>')
        caption = _caption(page)
        self.assertTrue(caption.startswith("Parties"), caption)
        self.assertIn("filtered by Kind: ", caption)
        self.assertIn("sorted by Name, ascending", caption)
        self.assertIn("data-announce", _status(page).group(0))
        self.assertIn('select[data-auto-submit]', page)


def _list_url_names():
    """Every named URL whose view is an ``ERPListView`` with no URL arguments."""
    names = []
    for name, entries in get_resolver().reverse_dict.lists():
        if not isinstance(name, str):
            continue
        for possibility, _pattern, _defaults, _converters in entries:
            if possibility[0][1]:
                continue
            try:
                view = get_resolver().resolve(reverse(name)).func
            except Exception:  # noqa: BLE001 - a name that does not reverse plainly
                continue
            cls = getattr(view, "view_class", None)
            if cls and issubclass(cls, ERPListView):
                names.append(name)
            break
    return sorted(set(names))


class EveryListPageTests(TestCase):
    def setUp(self):
        company = Company.objects.create(name="Acme Every")
        user = User.objects.create_superuser(username="root", password="pw", email="r@example.com")
        UserProfile.objects.create(user=user, company=company)
        self.client.force_login(user)

    def test_every_list_page_has_caption_live_region_and_no_auto_submit_on_change(self):
        names = _list_url_names()
        self.assertGreaterEqual(len(names), 10, names)
        for name in names:
            with self.subTest(name=name):
                page = self.client.get(reverse(name), {"q": "zz"}).content.decode()
                self.assertIsNotNone(_caption(page), name)
                self.assertIsNotNone(_status(page), name)
                self.assertNotIn('onchange="this.form.submit()"', page)
                self.assertNotRegex(page, r'<th[^>]*onclick')


class CrmListTests(_StaffCase):
    """The CRM lists (Phase 3 step 8) share the partials: caption, live
    region, pointer-only filters, and Apply keeps the sort."""

    def test_task_list_caption_names_the_effective_default_filters(self):
        page = self.client.get(reverse("erp-crm-task-list")).content.decode()
        caption = _caption(page)
        self.assertTrue(caption.startswith("Tasks"), caption)
        self.assertIn("Status: Open", caption)  # the default, though not in the URL
        self.assertNotIn("data-announce", _status(page).group(0))  # a default page is quiet

    def test_task_list_filter_is_announced_and_keeps_the_sort(self):
        page = self.client.get(reverse("erp-crm-task-list"),
                               {"due": "overdue", "sort": "due", "dir": "desc"}).content.decode()
        self.assertIn("Due: Overdue", _caption(page))
        self.assertIn("sorted by Due, descending", _caption(page))
        self.assertIn("data-announce", _status(page).group(0))
        self.assertIn('<input type="hidden" name="sort" value="due_at">', page)
        self.assertIn("data-auto-submit", page)
        self.assertNotIn('onchange="this.form.submit()"', page)

    def test_engagement_and_lead_lists(self):
        page = self.client.get(reverse("erp-crm-engagement-list"), {"chip": "stale"}).content.decode()
        self.assertIn("View: Stale", _caption(page))
        self.assertIn("data-announce", _status(page).group(0))
        self.assertIn('select[data-auto-submit]', page)
        page = self.client.get(reverse("erp-crm-lead-list"), {"q": "x"}).content.decode()
        self.assertTrue(_caption(page).startswith("Leads, search"), _caption(page))
        self.assertIn('select[data-auto-submit]', page)
