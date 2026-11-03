"""LLMHUB_PURPOSES validation: every bad entry stops the boot."""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from llmhub.purposes import DEFAULT_PURPOSES, load_external_purposes, load_purposes

CHAT = {"label": "Chat", "kind": "chat", "tools": True}


class PurposesRegistryTests(SimpleTestCase):
    def _raises(self, value, fragment: str = ""):
        with override_settings(LLMHUB_PURPOSES=value):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                load_purposes()
        if fragment:
            assert fragment in str(ctx.exception)

    def test_valid_registry(self):
        with override_settings(
            LLMHUB_PURPOSES={"chat_default": CHAT, "embed": {"label": "Embed", "kind": "embedding", "dimensions": 768}}
        ):
            purposes = load_purposes()
        assert list(purposes) == ["chat_default", "embed"]
        assert purposes["embed"].dimensions == 768

    def test_unset_falls_back_to_defaults(self):
        with override_settings(LLMHUB_PURPOSES=None):
            assert list(load_purposes()) == list(DEFAULT_PURPOSES)

    def test_bad_name(self):
        self._raises({"chat_default": CHAT, "Bad-Name": CHAT}, "purpose names must match")

    def test_missing_chat_default(self):
        self._raises({"research": CHAT}, "chat_default")

    def test_chat_default_must_be_chat(self):
        self._raises({"chat_default": {"label": "x", "kind": "embedding"}}, "kind='chat'")

    def test_bad_kind(self):
        self._raises({"chat_default": CHAT, "x": {"label": "X", "kind": "vision"}}, "kind='vision'")

    def test_embedding_with_tools(self):
        self._raises({"chat_default": CHAT, "e": {"label": "E", "kind": "embedding", "tools": True}}, "tool-call probe")

    def test_non_dict_spec(self):
        self._raises({"chat_default": CHAT, "x": ["label"]}, "must be a dict")

    def test_non_dict_registry(self):
        self._raises([("chat_default", CHAT)], "must be a dict")

    def test_unknown_spec_key(self):
        self._raises({"chat_default": {**CHAT, "colour": "blue"}}, "not a valid purpose spec")

    def test_missing_label(self):
        self._raises({"chat_default": {"kind": "chat", "label": ""}}, "label")

    def test_external_purposes(self):
        ok = [{"name": "search", "label": "Search", "route": "embed-lane", "owner": "worker"}]
        with override_settings(LLMHUB_EXTERNAL_PURPOSES=ok):
            assert load_external_purposes()[0].owner == "worker"
        with override_settings(LLMHUB_EXTERNAL_PURPOSES=[{"name": "x", "label": "X", "route": "", "owner": "w"}]):
            with self.assertRaises(ImproperlyConfigured):
                load_external_purposes()
