"""The storefront's category and brand read contract (docs/storefront-seo.md).

GET /api/products/categories/ and /brands/ are public for reads, like the
product catalog: anyone who isn't ERP staff sees visible rows only, through
the public serializer, which carries the SEO copy and never migration
provenance. Products filter by ``category_slug`` (whole subtree) and
``brand_slug``.
"""

from django.contrib import messages
from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from core.tests.helpers import make_staff_user

from .models import Brand, Category, Product, ProductCategory
from .seo_checks import seo_copy_warnings

CATS = "/api/products/categories/"
BRANDS = "/api/products/brands/"
PRODUCTS = "/api/products/products/"


def _rows(response):
    data = response.json()
    return data["results"] if isinstance(data, dict) else data


class TaxonomyFixture(TestCase):
    def setUp(self):
        self.root = Category.objects.create(
            title="Power", sort_order=1, seo_title="Power systems for the field",
            seo_meta_description="Batteries and chargers for remote sites.",
            body_copy="## About\n\nReal copy.", bc_id="legacy-1",
        )
        self.child = Category.objects.create(title="Batteries", parent=self.root, sort_order=1)
        self.grandchild = Category.objects.create(title="Lithium", parent=self.child)
        self.hidden = Category.objects.create(title="Internal", parent=self.root, visible_on_website=False)
        self.brand = Brand.objects.create(
            title="Acme", description="Makers of things.", seo_title="Acme gear", erpnext_name="ACME"
        )
        Brand.objects.create(title="Secret", visible_on_website=False)

        self.p_root = Product.objects.create(title="Inverter", brand=self.brand)
        self.p_grand = Product.objects.create(title="Cell")
        self.p_other = Product.objects.create(title="Cable")
        ProductCategory.objects.create(product=self.p_root, category=self.root)
        ProductCategory.objects.create(product=self.p_grand, category=self.grandchild)
        # A product in two categories of the same subtree is listed once.
        ProductCategory.objects.create(product=self.p_grand, category=self.child)
        self.client = APIClient()


class PublicCategoryReadTests(TaxonomyFixture):
    def test_anonymous_list_is_visible_rows_with_tree_fields(self):
        rows = {r["slug"]: r for r in _rows(self.client.get(CATS))}
        self.assertNotIn("internal", rows)
        power, lithium = rows["power"], rows["lithium"]
        self.assertIsNone(power["parent_slug"])
        self.assertEqual([c["slug"] for c in power["children"]], ["batteries"])  # hidden child left out
        self.assertEqual(lithium["parent_slug"], "batteries")
        self.assertEqual([a["slug"] for a in lithium["ancestors"]], ["power", "batteries"])
        self.assertEqual(power["seo_title"], "Power systems for the field")
        self.assertEqual(power["seo_meta_description"], "Batteries and chargers for remote sites.")
        self.assertEqual(power["body_copy"], "## About\n\nReal copy.")
        self.assertNotIn("bc_id", power)
        self.assertNotIn("visible_on_website", power)

    def test_slug_filter_returns_one_category(self):
        rows = _rows(self.client.get(CATS, {"slug": "batteries"}))
        self.assertEqual([r["title"] for r in rows], ["Batteries"])

    def test_hidden_category_is_404_for_the_public_and_visible_to_staff(self):
        url = f"{CATS}{self.hidden.pk}/"
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(make_staff_user("clerk", password="pw"))
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("bc_id", response.json())  # staff keep the full row

    def test_list_query_count_does_not_grow_with_rows(self):
        self.client.get(CATS)
        with CaptureQueriesContext(connection) as small:
            self.client.get(CATS)
        for n in range(10):
            Category.objects.create(title=f"Extra {n}", parent=self.child)
        with CaptureQueriesContext(connection) as large:
            self.client.get(CATS)
        self.assertEqual(len(small), len(large))

    def test_draft_copy_written_around_clean_is_never_served(self):
        Category.objects.filter(pk=self.root.pk).update(body_copy="status: DRAFT\n\nNot yet.")
        row = _rows(self.client.get(CATS, {"slug": "power"}))[0]
        self.assertIsNone(row["body_copy"])

    def test_writes_stay_closed_to_anonymous_and_shoppers(self):
        self.assertIn(self.client.post(CATS, {"title": "X"}, format="json").status_code, (401, 403))
        self.client.force_login(User.objects.create_user("shopper", password="x"))
        self.assertEqual(self.client.post(CATS, {"title": "X"}, format="json").status_code, 403)
        self.assertEqual(
            self.client.patch(f"{CATS}{self.root.pk}/", {"title": "X"}, format="json").status_code, 403
        )
        self.assertFalse(Category.objects.filter(title="X").exists())


class PublicBrandReadTests(TaxonomyFixture):
    def test_anonymous_list_hides_invisible_brands_and_provenance(self):
        rows = _rows(self.client.get(BRANDS))
        self.assertEqual([r["slug"] for r in rows], ["acme"])
        acme = rows[0]
        self.assertEqual(acme["seo_title"], "Acme gear")
        self.assertEqual(acme["description"], "Makers of things.")
        self.assertIsNone(acme["logo_url"])  # no CDN base in tests: null, not invented
        self.assertNotIn("erpnext_name", acme)

    def test_slug_filter(self):
        self.assertEqual([r["title"] for r in _rows(self.client.get(BRANDS, {"slug": "acme"}))], ["Acme"])
        self.assertEqual(_rows(self.client.get(BRANDS, {"slug": "secret"})), [])


class ProductSlugFilterTests(TaxonomyFixture):
    def _titles(self, **params):
        return sorted(r["title"] for r in _rows(self.client.get(PRODUCTS, params)))

    def test_category_slug_takes_the_whole_subtree(self):
        self.assertEqual(self._titles(category_slug="power"), ["Cell", "Inverter"])
        self.assertEqual(self._titles(category_slug="batteries"), ["Cell"])
        self.assertEqual(self._titles(category_slug="lithium"), ["Cell"])

    def test_unknown_category_slug_is_empty_not_everything(self):
        self.assertEqual(self._titles(category_slug="nope"), [])

    def test_brand_slug(self):
        self.assertEqual(self._titles(brand_slug="acme"), ["Inverter"])
        self.assertEqual(self._titles(brand_slug="nope"), [])

    def test_rows_carry_category_and_brand_slugs(self):
        rows = {r["title"]: r for r in _rows(self.client.get(PRODUCTS))}
        self.assertEqual(rows["Inverter"]["category_slug"], "power")
        self.assertEqual(rows["Inverter"]["brand_slug"], "acme")
        self.assertIsNone(rows["Cable"]["category_slug"])
        self.assertIsNone(rows["Cable"]["brand_slug"])

    def test_product_detail_carries_seo_fields(self):
        Product.objects.filter(pk=self.p_root.pk).update(seo_title="Inverter, 3 kW", seo_meta_description="Quiet.")
        data = self.client.get(f"{PRODUCTS}{self.p_root.pk}/").json()
        self.assertEqual(data["seo_title"], "Inverter, 3 kW")
        self.assertEqual(data["seo_meta_description"], "Quiet.")


class SeoCopyWarningTests(TestCase):
    def test_clean_copy_has_no_warnings(self):
        cat = Category.objects.create(
            title="Batteries", seo_title="Batteries for off-grid sites",
            seo_meta_description="Lithium and lead-acid batteries sized for cabins, boats and remote telemetry.",
        )
        self.assertEqual(seo_copy_warnings(cat), [])

    def test_truncated_and_placeholder_copy_warns(self):
        cat = Category.objects.create(
            title="Batteries", seo_title="Shop {name} | Store",
            seo_meta_description="Lithium and lead-acid batteries sized for cabins, boats and remote...",
        )
        text = " ".join(seo_copy_warnings(cat))
        self.assertIn("placeholder", text)
        self.assertIn("ellipsis", text)

    def test_mid_sentence_and_length_warn(self):
        cat = Category.objects.create(title="Cables", seo_title="C" * 70, seo_meta_description="Cables and")
        text = " ".join(seo_copy_warnings(cat))
        self.assertIn("full stop", text)
        self.assertIn("70 characters", text)
        self.assertIn("only 10 characters", text)

    def test_same_template_with_the_name_swapped_warns(self):
        Category.objects.create(title="Batteries", seo_title="Shop Batteries Equipment | Store")
        Category.objects.create(title="Cables", seo_title="Shop Cables Equipment | Store")
        third = Category.objects.create(title="Fuses", seo_title="Shop Fuses Equipment | Store")
        warnings = [w for w in seo_copy_warnings(third) if "template" in w]
        self.assertEqual(len(warnings), 1)
        self.assertIn("2 other categories", warnings[0])

    def test_erp_save_flashes_warnings_but_still_saves(self):
        Brand.objects.create(title="Acme", seo_title="Buy Acme Online")
        self.client.force_login(make_staff_user("clerk", password="pw"))
        response = self.client.post(
            reverse("erp-brand-create"),
            {"title": "Zenith", "seo_title": "Buy Zenith Online", "sort_order": 0, "visible_on_website": "on"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Brand.objects.filter(title="Zenith").exists())
        flashed = [str(m) for m in response.context["messages"] if m.level == messages.WARNING]
        self.assertTrue(any("template" in m for m in flashed), flashed)

    def test_forms_carry_length_guidance(self):
        from .forms import BrandForm, CategoryForm

        for form in (BrandForm(), CategoryForm()):
            self.assertIn("60 characters", form.fields["seo_title"].help_text)
            self.assertIn("50-160", form.fields["seo_meta_description"].help_text)


class HiddenAncestorTests(TaxonomyFixture):
    """A visible category under a hidden one is hidden for public readers."""

    def setUp(self):
        super().setUp()
        self.secret = Category.objects.create(title="Secret Clearance", visible_on_website=False)
        self.orphan = Category.objects.create(title="Open Bin", parent=self.secret)
        self.orphan_child = Category.objects.create(title="Open Bin Deep", parent=self.orphan)
        self.p_orphan = Product.objects.create(title="Leftover")
        ProductCategory.objects.create(product=self.p_orphan, category=self.orphan)

    def test_list_slug_and_retrieve_hide_the_whole_branch(self):
        body = self.client.get(CATS).content.decode()
        for leaked in ("Secret Clearance", "secret-clearance", str(self.secret.pk), "Open Bin"):
            self.assertNotIn(leaked, body)
        self.assertEqual(_rows(self.client.get(CATS, {"slug": "open-bin"})), [])
        self.assertEqual(self.client.get(f"{CATS}{self.orphan.pk}/").status_code, 404)

    def test_staff_still_see_the_branch(self):
        self.client.force_login(make_staff_user("clerk", password="pw"))
        self.assertEqual(self.client.get(f"{CATS}{self.orphan.pk}/").status_code, 200)

    def test_public_parent_fields_never_name_a_hidden_row(self):
        for row in _rows(self.client.get(CATS)):
            visible_ids = {str(c.pk) for c in Category.objects.filter(visible_on_website=True)}
            if row["parent"] is not None:
                self.assertIn(str(row["parent"]), visible_ids)
            for crumb in row["ancestors"]:
                self.assertIn(crumb["id"], visible_ids)

    def test_hidden_category_slug_lists_no_products(self):
        titles = lambda **p: sorted(r["title"] for r in _rows(self.client.get(PRODUCTS, p)))  # noqa: E731
        self.assertEqual(titles(category_slug="open-bin"), [])
        self.assertEqual(titles(category_slug="secret-clearance"), [])
        # Staff resolve hidden slugs (the back office needs them).
        from .serializers import CategoryIndex

        index = CategoryIndex.build()
        self.assertEqual(index.subtree_ids("open-bin", public_only=True), [])
        self.assertEqual(
            set(index.subtree_ids("open-bin")), {self.orphan.pk, self.orphan_child.pk}
        )

    def test_subtree_walk_skips_hidden_descendants(self):
        p_hidden_branch = Product.objects.create(title="Back Room")
        ProductCategory.objects.create(product=p_hidden_branch, category=self.hidden)  # hidden child of Power
        titles = sorted(r["title"] for r in _rows(self.client.get(PRODUCTS, {"category_slug": "power"})))
        self.assertEqual(titles, ["Cell", "Inverter"])

    def test_hidden_brand_slug_lists_no_products(self):
        secret = Brand.objects.get(title="Secret")
        Product.objects.create(title="Secret Thing", brand=secret)
        self.assertEqual(_rows(self.client.get(PRODUCTS, {"brand_slug": "secret"})), [])


SVC_ROLES = [
    {"name": "catalog-reader", "label": "Catalog reader", "kind": "service",
     "permissions": ["products.view_brand", "products.view_category"]},
]


@override_settings(GOLDEN_ROLES=SVC_ROLES)
class ServiceAccountFullReadTests(TaxonomyFixture):
    """A svc-* account within its contract keeps the staff shape (erp_assist)."""

    def setUp(self):
        super().setUp()
        from core.service_accounts import ensure_service_account, issue_api_key

        svc = ensure_service_account("catalog-reader", ["catalog-reader"])
        _key, raw = issue_api_key(svc, name="catalog-reader")
        self.svc = APIClient(HTTP_AUTHORIZATION=f"Api-Key {raw}")

    def test_service_account_reads_hidden_rows_in_full(self):
        brands = {r["title"]: r for r in _rows(self.svc.get(BRANDS, {"visible_on_website": "false"}))}
        self.assertIn("Secret", brands)
        self.assertIn("erpnext_name", brands["Secret"])
        response = self.svc.get(f"{CATS}{self.hidden.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("bc_id", response.json())

    def test_storefront_identity_still_gets_the_public_shape(self):
        from core.storefront import ensure_storefront_identity

        self.client.force_authenticate(ensure_storefront_identity())
        rows = _rows(self.client.get(BRANDS))
        self.assertEqual([r["slug"] for r in rows], ["acme"])
        self.assertNotIn("erpnext_name", rows[0])


class StaffWriteTests(TaxonomyFixture):
    def setUp(self):
        super().setUp()
        self.client.force_login(make_staff_user("clerk", password="pw"))

    def test_staff_patch_and_delete_brand(self):
        url = f"{BRANDS}{self.brand.pk}/"
        response = self.client.patch(url, {"seo_title": "Acme tools"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.brand.refresh_from_db()
        self.assertEqual(self.brand.seo_title, "Acme tools")
        spare = Brand.objects.create(title="Spare")
        self.assertEqual(self.client.delete(f"{BRANDS}{spare.pk}/").status_code, 204)
        self.assertFalse(Brand.objects.filter(pk=spare.pk).exists())

    def test_staff_patch_and_delete_category(self):
        url = f"{CATS}{self.hidden.pk}/"
        response = self.client.patch(url, {"seo_title": "Back room"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["seo_title"], "Back room")
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertFalse(Category.objects.filter(pk=self.hidden.pk).exists())
