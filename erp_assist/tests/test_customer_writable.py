"""supabase_user_id must not be writable by the LLM assistant.

It links a customer to a portal login; re-pointing it is an account-takeover
path. The allow-list rejects it client-side, before any HTTP request is made.
"""

from __future__ import annotations

import asyncio
from unittest import mock

from django.test import SimpleTestCase

from erp_assist.core import ERPClient, ERPConfig, ERPValidationError
from erp_assist.core import customers as customers_core
from erp_assist.core._writable_fields import CUSTOMER_WRITABLE


class CustomerSupabaseUserIdNotWritableTests(SimpleTestCase):
    def test_supabase_user_id_not_in_customer_writable(self):
        self.assertNotIn("supabase_user_id", CUSTOMER_WRITABLE)

    def test_update_customer_rejects_supabase_user_id_without_request(self):
        client = ERPClient(ERPConfig(base_url="http://testserver", api_key="k"))
        with mock.patch.object(ERPClient, "patch", new_callable=mock.AsyncMock) as patch:
            with self.assertRaises(ERPValidationError) as ctx:
                asyncio.run(
                    customers_core.update_customer(
                        client,
                        "00000000-0000-0000-0000-000000000001",
                        {"supabase_user_id": "00000000-0000-0000-0000-0000000000aa"},
                    )
                )
        self.assertIn("supabase_user_id", str(ctx.exception))
        patch.assert_not_called()
