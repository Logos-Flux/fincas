"""The quote tools drop the pipeline fields that moved to the engagement
(CRM Phase 3 step 5, docs/crm/phase3-engagement.md 1.6)."""

from __future__ import annotations

import asyncio
import inspect
from unittest import mock

from django.test import SimpleTestCase

from erp_assist import mcp_adapter
from erp_assist.core import ERPClient, ERPConfig, ERPValidationError
from erp_assist.core import orders as orders_core
from erp_assist.core._writable_fields import QUOTE_WRITABLE
from orders.models import Quote

PIPELINE_PARAMS = {"sales_stage", "estimated_close_date", "win_probability"}


class QuotePipelineMovedTests(SimpleTestCase):
    def test_create_quote_takes_no_pipeline_parameters(self):
        for func in (orders_core.create_quote, mcp_adapter.create_quote):
            params = set(inspect.signature(inspect.unwrap(func)).parameters)
            with self.subTest(func=func.__module__):
                self.assertFalse(params & PIPELINE_PARAMS)

    def test_moved_fields_are_not_writable(self):
        self.assertFalse(set(Quote.MOVED_TO_ENGAGEMENT) & QUOTE_WRITABLE)

    def test_update_quote_refuses_a_moved_field_before_any_request(self):
        client = ERPClient(ERPConfig(base_url="http://testserver", api_key="k"))
        with mock.patch.object(ERPClient, "patch", new_callable=mock.AsyncMock) as patch:
            with self.assertRaises(ERPValidationError) as ctx:
                asyncio.run(orders_core.update_quote(
                    client, "00000000-0000-0000-0000-000000000001", {"sales_stage": "won"},
                ))
        self.assertIn("sales_stage", str(ctx.exception))
        patch.assert_not_called()

    def test_list_quotes_docstring_names_real_stages(self):
        doc = orders_core.list_quotes.__doc__
        self.assertNotIn("reviewed", doc)
        self.assertIn("closed_won", doc)
