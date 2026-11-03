"""Unit tests for OpenAPI 3.0 schema generation and documentation views."""

from django.test import TestCase
from django.urls import reverse


class TestOpenAPISchema(TestCase):
    # TestCase: the docs pages render the ERP nav, which reads label
    # overrides from the database when the label cache is cold. As a
    # SimpleTestCase it passed only when an earlier test warmed the cache.
    def test_schema_endpoint_returns_valid_json_or_yaml(self):
        url = reverse("schema")
        resp = self.client.get(url)
        assert resp.status_code == 200
        # Check content contains OpenAPI 3 version and Fincas title
        content = resp.content.decode("utf-8")
        assert "openapi" in content.lower()
        assert "Fincas API" in content

    def test_swagger_ui_endpoint(self):
        url = reverse("swagger-ui")
        resp = self.client.get(url)
        assert resp.status_code == 200
        content = resp.content.decode("utf-8")
        assert "swagger-ui" in content.lower()

    def test_redoc_ui_endpoint(self):
        url = reverse("redoc")
        resp = self.client.get(url)
        assert resp.status_code == 200
        content = resp.content.decode("utf-8")
        assert "redoc" in content.lower()
