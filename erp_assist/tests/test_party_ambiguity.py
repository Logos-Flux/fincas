"""erp_assist never guesses a party or customer (docs/crm/phase2-party.md 2.7).

Drives the real ``erp_assist.core`` functions against the in-process DRF
stack (same harness as ``test_api_e2e``): the assistant authenticates with
the service API key, which is cross-tenant, so the same email in two tenants
is ``ambiguous``. Covers ``find_customer``, ``find_party``,
``create_customer``, ``create_party`` and every write tool that takes a
free-text customer reference (``create_customer_address``, ``create_quote``),
plus the MCP adapter's rendering of ``ERPAmbiguousError``.
"""

from __future__ import annotations

import asyncio
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from config.asgi import application
from core.models import Company
from customers.models import Customer, CustomerAddress
from erp_assist import mcp_adapter
from erp_assist.core import ERPAmbiguousError, ERPClient, ERPConfig, ERPNotFoundError
from erp_assist.core import customers as customers_core
from erp_assist.core import orders as orders_core
from erp_assist.core import parties as parties_core
from orders.models import Quote
from parties.models import Party, PartyEmail

API_KEY = "erp-assist-party-key"


def run(coro):
    return asyncio.run(coro)


def asgi_client() -> ERPClient:
    client = ERPClient.__new__(ERPClient)
    client._config = ERPConfig(base_url="http://testserver", api_key=API_KEY)
    client._http = httpx.AsyncClient(
        transport=httpx.ASGITransport(application),
        base_url="http://testserver",
        follow_redirects=True,
        headers={"Authorization": f"Api-Key {API_KEY}", "Content-Type": "application/json",
                 "Accept": "application/json"},
    )
    from erp_assist.core._cache import TTLCache

    client.lookup_cache = TTLCache()
    return client


@override_settings(API_KEY=API_KEY)
class AssistAmbiguityTests(TransactionTestCase):
    def setUp(self):
        User.objects.create_superuser(username="api-service-party", password=None)
        self.a = Company.objects.create(name="Tenant A")
        self.b = Company.objects.create(name="Tenant B")
        self.client = asgi_client()

    def tearDown(self):
        run(self.client._http.aclose())

    def customer(self, company, email, **kw):
        kw.setdefault("first_name", "Ada")
        kw.setdefault("last_name", "Lovelace")
        return Customer.objects.create(email=email, company=company, **kw)

    # ---- find_customer (2.7 rows) ------------------------------------------

    def test_find_customer_exact(self):
        c = self.customer(self.a, "Ada@Example.com")
        env = run(customers_core.find_customer(self.client, "ada@example.com"))
        self.assertEqual((env["match_quality"], env["item"]["id"]), ("exact", str(c.pk)))

    def test_find_customer_exact_in_two_tenants_is_ambiguous(self):
        self.customer(self.a, "shared@example.com")
        self.customer(self.b, "Shared@example.com")
        with self.assertRaises(ERPAmbiguousError) as ctx:
            run(customers_core.find_customer(self.client, "shared@example.com"))
        env = ctx.exception.envelope
        self.assertEqual((env["match_quality"], env["item"]), ("ambiguous", None))
        self.assertEqual(len(ctx.exception.candidates), 2)

    def test_find_customer_one_fuzzy_hit(self):
        c = self.customer(self.a, "g@example.com", first_name="Grace", last_name="Hopper")
        env = run(customers_core.find_customer(self.client, "Hopper"))
        self.assertEqual((env["match_quality"], env["item"]["id"]), ("fuzzy", str(c.pk)))

    def test_find_customer_ten_fuzzy_hits_never_return_an_item(self):
        for i in range(10):
            self.customer(self.a, f"sam{i}@example.com", first_name="Sam", last_name=f"Smith{i}")
        with self.assertRaises(ERPAmbiguousError) as ctx:
            run(customers_core.find_customer(self.client, "Sam"))
        self.assertIsNone(ctx.exception.envelope["item"])
        self.assertEqual(len(ctx.exception.candidates), 10)

    def test_find_customer_none(self):
        env = run(customers_core.find_customer(self.client, "nobody@example.com"))
        self.assertEqual((env["match_quality"], env["item"]), ("none", None))

    # ---- find_party ----------------------------------------------------------

    def test_find_party_exact_and_ambiguous(self):
        c = self.customer(self.a, "ada@example.com")
        env = run(parties_core.find_party(self.client, "ada@example.com"))
        self.assertEqual((env["match_quality"], env["item"]["id"]),
                         ("exact", str(Customer.objects.get(pk=c.pk).party_id)))
        self.customer(self.b, "ADA@example.com")
        with self.assertRaises(ERPAmbiguousError):
            run(parties_core.find_party(self.client, "ada@example.com"))
        # Narrowed to one tenant, it is exact again.
        env = run(parties_core.find_party(self.client, "ada@example.com", company=str(self.b.pk)))
        self.assertEqual(env["match_quality"], "exact")

    # ---- create_customer / create_party -------------------------------------

    def test_create_customer_refuses_a_known_email_unless_confirmed(self):
        self.customer(self.a, "ada@example.com")
        result = run(customers_core.create_customer(
            self.client, first_name="Ada", last_name="L", email="ADA@example.com"))
        self.assertFalse(result["created"])
        self.assertEqual(result["match_quality"], "exact")
        self.assertIn("already exists", result["message"])
        self.assertEqual(Customer.objects.count(), 1)

    def test_create_customer_with_new_email_creates(self):
        result = run(customers_core.create_customer(
            self.client, first_name="New", last_name="Person", email="new@example.com",
            customer_type="individual", extra_fields={"company": str(self.a.pk)}))
        self.assertIn("id", result)
        self.assertTrue(Customer.objects.filter(email="new@example.com").exists())

    def test_create_party_duplicate_then_confirm_new(self):
        existing = Party.objects.create(company=self.a, kind="person", given_name="Ada",
                                        family_name="L", display_name="Ada L", origin="erp")
        PartyEmail.objects.create(party=existing, email="ada@example.com", purpose="work", is_primary=True)
        result = run(parties_core.create_party(
            self.client, kind="person", given_name="Ada", family_name="Other",
            email="ada@example.com", company=str(self.a.pk)))
        self.assertFalse(result["created"])
        self.assertEqual(result["candidates"][0]["id"], str(existing.pk))
        self.assertEqual(Party.objects.count(), 1)
        result = run(parties_core.create_party(
            self.client, kind="person", given_name="Ada", family_name="Other",
            email="ada@example.com", company=str(self.a.pk), confirm_new=True))
        self.assertEqual(result["origin"], "api")
        self.assertEqual(Party.objects.count(), 2)

    def test_create_party_requires_tenant_when_several_exist(self):
        from erp_assist.core import ERPValidationError

        with self.assertRaises(ERPValidationError):
            run(parties_core.create_party(self.client, kind="organisation", display_name="Acme"))
        result = run(parties_core.create_party(
            self.client, kind="organisation", display_name="Acme", company=str(self.b.pk)))
        self.assertEqual(result["company"], str(self.b.pk))

    # ---- write tools taking free text ---------------------------------------

    def _address(self, customer_ref):
        return customers_core.create_customer_address(
            self.client, customer=customer_ref, address_line_1="1 Main", city="X",
            state_province="NY", postal_code="10001", country="US")

    def test_create_customer_address_resolves_exact_free_text(self):
        c = self.customer(self.a, "ada@example.com")
        result = run(self._address("Ada@Example.com"))
        self.assertEqual(result["customer"], str(c.pk))

    def test_create_customer_address_refuses_ambiguous(self):
        self.customer(self.a, "shared@example.com")
        self.customer(self.b, "SHARED@example.com")
        with self.assertRaises(ERPAmbiguousError) as ctx:
            run(self._address("shared@example.com"))
        self.assertEqual(ctx.exception.envelope["match_quality"], "ambiguous")
        self.assertFalse(CustomerAddress.objects.exists())

    def test_create_customer_address_refuses_fuzzy_without_confirmation(self):
        self.customer(self.a, "g@example.com", first_name="Grace", last_name="Hopper")
        with self.assertRaises(ERPAmbiguousError) as ctx:
            run(self._address("Grace Hopper"))
        self.assertEqual(ctx.exception.envelope["match_quality"], "fuzzy")
        self.assertIn("Confirm", ctx.exception.envelope["message"])
        self.assertFalse(CustomerAddress.objects.exists())

    def test_create_customer_address_none_is_not_found(self):
        with self.assertRaises(ERPNotFoundError):
            run(self._address("nobody@example.com"))

    def test_create_customer_address_uuid_passes_through(self):
        c = self.customer(self.a, "ada@example.com")
        result = run(self._address(str(c.pk)))
        self.assertEqual(result["customer"], str(c.pk))

    def test_create_quote_refuses_ambiguous_customer(self):
        self.customer(self.a, "g1@example.com", first_name="Grace", last_name="Hopper")
        self.customer(self.a, "g2@example.com", first_name="Grace", last_name="Kelly")
        with self.assertRaises(ERPAmbiguousError):
            run(orders_core.create_quote(
                self.client, customer="Grace", extra_fields={"company": str(self.a.pk)}))
        self.assertFalse(Quote.objects.exists())

    def test_create_quote_with_exact_free_text(self):
        c = self.customer(self.a, "ada@example.com")
        result = run(orders_core.create_quote(
            self.client, customer="ada@example.com", extra_fields={"company": str(self.a.pk)}))
        self.assertEqual(result["customer"], str(c.pk))


class McpRendersAmbiguityTests(SimpleTestCase):
    """The MCP adapter returns the envelope, not a bare error string."""

    def test_ambiguous_error_rendered_as_envelope(self):
        envelope = {
            "match_quality": "ambiguous", "item": None, "message": "Several records match.",
            "candidates": [{"id": "1", "display_name": "Ada One"}, {"id": "2", "display_name": "Ada Two"}],
        }

        async def boom(client, **kwargs):
            raise ERPAmbiguousError(envelope)

        with mock.patch.object(mcp_adapter, "_get_client", new=mock.AsyncMock(return_value=object())):
            result = run(mcp_adapter._run(boom))
        self.assertEqual(result["error"], "ERPAmbiguousError")
        self.assertEqual(result["match_quality"], "ambiguous")
        self.assertIsNone(result["item"])
        self.assertEqual([c["id"] for c in result["candidates"]], ["1", "2"])

    def test_fuzzy_refusal_never_carries_an_item(self):
        envelope = {"match_quality": "fuzzy", "item": {"id": "1"}, "candidates": [{"id": "1"}],
                    "message": "x"}

        async def boom(client, **kwargs):
            raise ERPAmbiguousError(envelope, "Confirm first.")

        with mock.patch.object(mcp_adapter, "_get_client", new=mock.AsyncMock(return_value=object())):
            result = run(mcp_adapter._run(boom))
        self.assertIsNone(result["item"])
        self.assertEqual(result["message"], "Confirm first.")

    def test_party_tools_registered(self):
        names = {tool.name for tool in run(mcp_adapter.mcp.list_tools())}
        self.assertTrue({"find_party", "create_party", "get_party", "list_parties", "find_customer"} <= names)
