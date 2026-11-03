"""Gmail mapping: pure functions over invented message resources."""

from __future__ import annotations

from django.test import SimpleTestCase

from .. import mapping, values
from .helpers import message


class KeyTests(SimpleTestCase):
    def test_message_id_is_normalised_and_hashed(self):
        a = message(mid="<CAF-X@Mail.Example.com>")
        b = message(gid="other", mid="  <caf-x@mail.example.com> ")
        self.assertEqual(mapping.message_id(a), "caf-x@mail.example.com")
        self.assertEqual(mapping.external_id(a, "rep@example.org"), mapping.external_id(b, "boss@example.org"))
        self.assertTrue(mapping.external_id(a, "x").startswith("mid:"))
        self.assertNotIn("example", mapping.external_id(a, "x"))

    def test_thread_root_prefers_references_then_in_reply_to(self):
        root = message(mid="<root@x>")
        reply = message(mid="<r1@x>", irt="<root@x>")
        deep = message(mid="<r2@x>", irt="<r1@x>", refs="<root@x> <r1@x>")
        keys = {mapping.thread_key(m, "rep@example.org") for m in (root, reply, deep)}
        self.assertEqual(len(keys), 1)
        self.assertEqual(len(keys.pop()), len("mid:") + 40)

    def test_without_message_id_keys_fall_back_to_the_mailbox(self):
        bare = message(mid="")
        self.assertEqual(mapping.external_id(bare, "Rep@Example.org"),
                         f"gmail:{mapping.mailbox_hash('rep@example.org')}:19b00000000000f1")
        self.assertTrue(mapping.thread_key(bare, "rep@example.org").startswith("gmail:"))


class AddressTests(SimpleTestCase):
    def test_roles_display_names_and_dedupe(self):
        m = message(frm='"Lovelace, Ada" <ada@example.com>', to=["rep@example.org", "REP@example.org"],
                    cc=["Grace <grace@example.net>"], bcc=["boss@example.org"])
        self.assertEqual(mapping.addresses(m), [
            ("from", "ada@example.com", "Lovelace, Ada"), ("to", "rep@example.org", ""),
            ("cc", "grace@example.net", "Grace"), ("bcc", "boss@example.org", ""),
        ])
        self.assertNotIn("bcc", mapping.QUALIFYING_ROLES)


class BodyTests(SimpleTestCase):
    def test_plain_text_wins_over_html(self):
        self.assertEqual(mapping.body_text(message(text="plain\r\nbody", html="<p>html</p>")), "plain\nbody")

    def test_html_only_is_converted(self):
        body = mapping.body_text(message(text=None, html="<style>x{}</style><p>A&amp;B</p><p>next<br>line</p>"))
        self.assertEqual(body, "A&B\n\nnext\nline")

    def test_body_is_capped(self):
        body = mapping.body_text(message(text="é" * 80_000))  # 160 kB of UTF-8
        self.assertLessEqual(len(body.encode()), values.BODY_CAP_BYTES)
        self.assertTrue(body.startswith("é"))

    def test_attachments_are_never_bodies_and_raw_keeps_only_their_ids(self):
        m = message(text=None)
        m["payload"]["parts"] = [{"mimeType": "text/plain", "filename": "notes.txt",
                                  "body": {"size": 4, "data": mapping.base64.urlsafe_b64encode(b"data").decode(),
                                           "attachmentId": "att-1"}}]
        self.assertEqual(mapping.body_text(m), "")
        raw = mapping.raw_message(m)
        self.assertEqual(raw["payload"]["parts"][0]["body"], {"attachmentId": "att-1", "size": 4})
        self.assertIn("data", m["payload"]["parts"][0]["body"], "the input is not mutated")
        self.assertEqual(mapping.attachments(m), [{"filename": "notes.txt", "size": 4}])

    def test_occurred_at_and_labels(self):
        m = message(at="2026-03-02T14:00:00+00:00", labels=("INBOX", 7))
        self.assertEqual(mapping.occurred_at(m).isoformat(), "2026-03-02T14:00:00+00:00")
        self.assertEqual(mapping.label_ids(m), {"INBOX"})


class HardeningTests(SimpleTestCase):
    def test_charset_is_honoured(self):
        m = message(text=None)
        raw = "Grüße".encode("iso-8859-1")
        m["payload"]["parts"] = [{"mimeType": "text/plain", "filename": "",
                                  "headers": [{"name": "Content-Type", "value": 'text/plain; charset="ISO-8859-1"'}],
                                  "body": {"size": len(raw), "data": mapping.base64.urlsafe_b64encode(raw).decode()}}]
        self.assertEqual(mapping.body_text(m), "Grüße")

    def test_html_conversion_is_linear(self):
        import time

        hostile = "<style>" * 200_000 + "x"
        started = time.monotonic()
        mapping.html_to_text(hostile)
        self.assertLess(time.monotonic() - started, 5)

    def test_raw_drops_binary_parts_and_caps_text(self):
        m = message(text="t" * (values.BODY_CAP_BYTES + 10))
        m["payload"]["parts"].append({"mimeType": "image/png", "filename": "",
                                      "body": {"size": 3, "data": "AAAA"}})
        raw = mapping.raw_message(m)
        self.assertEqual(raw["payload"]["parts"][1]["body"], {"size": 3})
        text_body = raw["payload"]["parts"][0]["body"]
        self.assertTrue(text_body["truncated"])
        self.assertEqual(len(mapping._raw_bytes(text_body["data"])), values.BODY_CAP_BYTES)
