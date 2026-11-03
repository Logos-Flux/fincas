"""Every system key golden code writes is registered in ``core.sidecar.SYSTEM_KEYS``
(phase4-fields.md 3.1, step 2 "done when").

The grep below finds literal ``metadata`` keys in source: ``metadata={"k": …}``,
``metadata["k"]``, ``metadata.get("k")``, ``metadata__k=`` lookups and the
``*_KEY = "k"`` constants the backfills use. A new un-dotted key that is not
registered fails here; register it on the sidecar(s) it is written to, or, for
a ``metadata`` column that is not a sidecar, in ``NOT_SIDECAR`` below.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from core import provenance, sidecar

ROOT = Path(settings.BASE_DIR)
# Apps whose code writes the CRM sidecars.
SCANNED = ("parties", "crm", "communications", "core", "customers", "unify", "crm_import",
           "reporting", "erp_assist", "webhooks")

# ``UserIdentity.metadata`` and the login session: not CRM sidecars.
SKIPPED = ("core/identity",)
# Keys on other ``metadata`` columns that are not CRM sidecars.
NOT_SIDECAR = {"erased", "erased_at", "legacy_column"}  # core.ExternalRef
# Keys nested inside a registered key's value (``{"backfill": {…}}``).
NESTED = {"assigned_company"}
# Django JSON lookups, not keys.
LOOKUPS = {"contains", "contained_by", "has_key", "has_keys", "has_any_keys", "isnull", "in",
           "exact", "iexact", "icontains", "startswith", "lt", "lte", "gt", "gte"}

# ``metadata={…}`` and the ``meta = {…}`` dicts the backfills build first.
_DICT_START = re.compile(r"\bmeta(?:data)?\s*=\s*\{")
_DICT_KEY = re.compile(r"""["']([a-z_][a-z0-9_]*)["']\s*:""")
_SUBSCRIPT = re.compile(
    r"""\bmetadata\s*(?:\[\s*|\.(?:get|setdefault|pop)\(\s*)["']([a-z_][a-z0-9_]*)["']""")
# ``meta["k"] = …``: a key added to a ``meta`` dict before it is written.
_META_SET = re.compile(r"""\bmeta\[\s*["']([a-z_][a-z0-9_]*)["']\s*\]\s*=""")
_LOOKUP = re.compile(r"metadata__([a-z_][a-z0-9_]*?)(?=__|\s*=|\")")
_CONSTANT = re.compile(r"""^[A-Z_]*(?:KEY|MARKER|LINKS)\s*=\s*["']([a-z_][a-z0-9_]*)["']""", re.M)


def _top_level_keys(text: str) -> set[str]:
    """Keys at the top level of every ``metadata={…}`` / ``meta = {…}`` dict."""
    keys = set()
    for match in _DICT_START.finditer(text):
        depth, i = 1, match.end()
        chunk = []
        while i < len(text) and depth:
            ch = text[i]
            depth += {"{": 1, "}": -1}.get(ch, 0)
            chunk.append(ch if depth == 1 else " ")
            i += 1
        keys |= set(_DICT_KEY.findall("".join(chunk)))
    return keys


def _sources():
    for app in SCANNED:
        for path in sorted((ROOT / app).rglob("*.py")):
            parts = set(path.parts)
            if "tests" in parts or "migrations" in parts or path.name.startswith("test_"):
                continue
            if str(path.relative_to(ROOT)).startswith(SKIPPED):
                continue
            yield path


def literal_keys() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        keys = _top_level_keys(text)
        keys |= set(_SUBSCRIPT.findall(text)) | set(_META_SET.findall(text))
        keys |= {k for k in _LOOKUP.findall(text) if k not in LOOKUPS}
        if "metadata" in text:
            keys |= set(_CONSTANT.findall(text))
        for key in keys:
            found.setdefault(key, set()).add(str(path.relative_to(ROOT)))
    return found


class SystemKeyRegistryTests(SimpleTestCase):
    def test_every_literal_metadata_key_is_registered(self):
        known = sidecar.all_system_keys() | provenance.NON_SIDECAR_SYSTEM_KEYS | NOT_SIDECAR | NESTED
        found = literal_keys()
        self.assertIn("name_inferred", found, "the grep found nothing: is BASE_DIR right?")
        unregistered = {key: sorted(paths) for key, paths in found.items()
                        if sidecar.namespace(key) == sidecar.SYSTEM and key not in known}
        self.assertEqual(unregistered, {}, "register these in core.sidecar.SYSTEM_KEYS")

    def test_the_grep_catches_an_unregistered_key(self):
        text = ('Party.objects.create(metadata={"sneaky": {"inner": 1}, "after": 2})\n'
                'x.metadata.get("other")\nq(metadata__third=1)\nmeta["fourth"] = 1')
        keys = (_top_level_keys(text) | set(_SUBSCRIPT.findall(text)) | set(_META_SET.findall(text))
                | set(_LOOKUP.findall(text)))
        self.assertEqual(keys, {"sneaky", "after", "other", "third", "fourth"})

    def test_registered_keys_follow_the_system_key_rule(self):
        self.assertEqual(set(sidecar.SYSTEM_KEYS), set(sidecar.SIDECAR_MODELS))
        for object_name, keys in sidecar.SYSTEM_KEYS.items():
            for key in keys:
                self.assertEqual(sidecar.namespace(key), sidecar.SYSTEM, f"{object_name}: {key}")

    def test_provenance_still_protects_every_key_it_did(self):
        before_step_2 = {
            "backfill", "bc_import", "contact", "customer", "erased", "erased_at", "lead",
            "legacy_column", "name_inferred", "seed", "seed_index", "from_activitylog",
            "activitylog", "backfill_step", "lead_number", "lead_source", "lead_direction",
            "request_type", "lead_legacy", "unmapped_lead_status",
        }
        self.assertLessEqual(before_step_2, provenance.SYSTEM_METADATA_KEYS)
        for key in ("_private", "provenance", "provenance.source"):
            self.assertTrue(provenance.is_system_metadata_key(key))
        for key in ("custom.package", "hubspot.x", "note"):
            self.assertFalse(provenance.is_system_metadata_key(key))

    def test_an_unregistered_undotted_key_is_system_namespace_but_not_kept(self):
        # Erasure keeps registered system keys only; "note" is neither.
        self.assertEqual(sidecar.namespace("note"), sidecar.SYSTEM)
        self.assertFalse(sidecar.is_system_key("note"))
        self.assertTrue(sidecar.is_system_key("lead", "task"))
        self.assertFalse(sidecar.is_system_key("name_inferred", "task"))
