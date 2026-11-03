"""Klaviyo marketing-email adapter (docs/crm/phase6-adapters.md 3.2, steps 3 and 3b).

Golden owns consent (A12). This add-on tells Klaviyo who may receive
marketing — only what ``parties.consent.can_contact(..., "marketing", ...)``
allows — and pulls back who stopped wanting it: unsubscribes, hard bounces
and spam complaints become golden withdrawals and suppressions. A
Klaviyo-side subscribe is never golden consent.

Install with ``GOLDEN_PLUGINS = [..., "golden_erp.addons.klaviyo.plugin.KlaviyoPlugin"]``
and connect an account under Admin → Integrations → Connected accounts.
Operator notes: docs/crm/adapters.md, "Klaviyo".
"""
