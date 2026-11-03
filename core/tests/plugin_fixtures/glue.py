"""BasePlugin fixtures contributing URLs."""

from django.http import HttpResponse
from django.urls import include, path

from core.plugins import BasePlugin


def ok_view(request, **kwargs):
    return HttpResponse("plugin ok")


class AlphaPlugin(BasePlugin):
    name = "alpha_fixture"

    def get_urls(self):
        return [
            path("plugin-alpha/", ok_view, name="plugin-alpha"),
            path("plugin-alpha/nested/", include([path("deep/", ok_view)])),
        ]


class BetaPlugin(BasePlugin):
    """Claims the same path as AlphaPlugin."""

    name = "beta_fixture"

    def get_urls(self):
        return [path("plugin-alpha/", ok_view)]


class PublicPlugin(BasePlugin):
    name = "public_fixture"
    url_auth = "public"

    def get_urls(self):
        return [path("plugin-public/", ok_view, name="plugin-public")]


class TypoAuthPlugin(BasePlugin):
    name = "typo_auth_fixture"
    url_auth = "none"

    def get_urls(self):
        return [path("plugin-typo/", ok_view)]
