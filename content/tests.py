"""Tests for the content app's ERP article views — delete flow.

ArticleDeleteView is POST-only and guarded by ERPViewMixin (login required,
same guard as every other content view). storefront.blog_posts is a plain
live SQL view over content_article, so a row delete is all that's needed —
no MV refresh to assert on.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Article

PASSWORD = "unit-test-Str0ng-pass!"


class ArticleDeleteViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staffer", password=PASSWORD, is_staff=True
        )
        self.article = Article.objects.create(
            title="Solar 101",
            slug="solar-101",
            content="<p>Hello sun</p>",
        )
        self.delete_url = reverse("erp-article-delete", kwargs={"pk": self.article.pk})

    def test_staff_post_deletes_and_redirects_to_list(self):
        self.client.force_login(self.staff)
        response = self.client.post(self.delete_url)
        self.assertRedirects(response, reverse("erp-article-list"))
        self.assertFalse(Article.objects.filter(pk=self.article.pk).exists())

    def test_staff_delete_shows_success_message(self):
        self.client.force_login(self.staff)
        response = self.client.post(self.delete_url, follow=True)
        messages = [str(m) for m in response.context["messages"]]
        self.assertIn('Article "Solar 101" deleted.', messages)

    def test_anonymous_post_redirects_to_login_and_keeps_row(self):
        response = self.client.post(self.delete_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/erp/login/", response.url)
        self.assertTrue(Article.objects.filter(pk=self.article.pk).exists())

    def test_get_does_not_delete(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.delete_url)
        self.assertEqual(response.status_code, 405)  # POST-only view
        self.assertTrue(Article.objects.filter(pk=self.article.pk).exists())


class ArticleViewOnSiteLinkTests(TestCase):
    """``storefront_url`` reaches the template context (core.context_processors.erp_nav)."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username="staffer", password=PASSWORD, is_staff=True
        )
        self.article = Article.objects.create(
            title="Solar 101",
            slug="solar-101",
            content="<p>Hello sun</p>",
            status=Article.Status.PUBLISHED,
        )
        self.url = reverse("erp-article-detail", kwargs={"pk": self.article.pk})
        self.client.force_login(self.staff)

    def test_view_on_site_links_to_the_configured_storefront(self):
        with self.settings(STOREFRONT_URL="https://shop.example.test/"):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["storefront_url"], "https://shop.example.test")
        self.assertContains(response, 'href="https://shop.example.test/blog/solar-101"')

    def test_no_storefront_url_hides_the_link(self):
        with self.settings(STOREFRONT_URL=""):
            response = self.client.get(self.url)
        self.assertEqual(response.context["storefront_url"], "")
        self.assertNotContains(response, "View on site")
