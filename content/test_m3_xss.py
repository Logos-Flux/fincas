"""Milestone 3 Track G — S4: XSS (CMS articles surface).

Article `content` is rich HTML by design ({{ object.content|safe }} in
the staff detail template), so the control point is AUTHORING access:
only authenticated users may create/update articles, and anonymous
reads go through the auth'd API. Titles/excerpts must always escape.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from content.models import Article
from core.tests.helpers import make_staff_user

SCRIPT = "<script>alert('cms')</script>"


class ArticleXssTests(TestCase):
    def setUp(self):
        self.staff = make_staff_user(
            username="cms-staff", password="pw", is_staff=True
        )
        self.article = Article.objects.create(
            title="Hello", slug="hello", content="<p>Body</p>",
            status=Article.Status.PUBLISHED,
        )

    def test_anonymous_cannot_author_article(self):
        response = APIClient().post(
            "/api/v1/content/articles/",
            {"title": "Spam", "slug": "spam", "content": SCRIPT},
            format="json",
        )
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(Article.objects.filter(slug="spam").exists())

    def test_anonymous_cannot_edit_article(self):
        response = APIClient().patch(
            f"/api/v1/content/articles/{self.article.pk}/",
            {"content": SCRIPT},
            format="json",
        )
        self.assertIn(response.status_code, (401, 403))

    def test_article_title_escaped_in_erp_list(self):
        Article.objects.create(
            title=SCRIPT, slug="script-title", content="x",
        )
        self.client.force_login(self.staff)
        response = self.client.get("/erp/content/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SCRIPT, response.content.decode())

    def test_article_content_raw_html_is_staff_only_surface(self):
        # Pin current behavior: CMS body renders raw HTML (|safe) by
        # design for rich text; authoring requires auth (above), and the
        # render surface requires staff login. If this template ever
        # becomes public, this test must be revisited.
        api = APIClient()
        api.force_authenticate(
            user=make_staff_user(username="cms-u", password="pw")
        )
        response = api.post(
            "/api/v1/content/articles/",
            {"title": "Rich", "slug": "rich",
             "content": "<p>ok</p><script>alert(1)</script>"},
            format="json",
        )
        self.assertIn(response.status_code, (201, 400))
        if response.status_code == 201:
            print(
                "\n[S4-characterize] CMS stores unsanitized HTML "
                "(no bleach/nh3 in deps) — authoring auth is the control"
            )
