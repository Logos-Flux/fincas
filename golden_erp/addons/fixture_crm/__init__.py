"""The ``fixture`` CRM adapters: invented providers that exercise the live adapter contract.

**Test and pattern only — never install it in a deployment.** It talks to
``*.fixture.example.com``, which does not exist. It is what golden's own
tests run the adapter machinery against (docs/crm/phase6-adapters.md
section 8, step 1), and the layout and code every real adapter (ClickUp,
Klaviyo, Gmail, Aircall, Google Ads) copies:

- ``fixture`` — API key, webhook (HMAC over timestamp + body), cursor poll,
  and an idempotent outbound contact push keyed by ``ExternalRef``.
- ``fixture_oauth`` — per-user OAuth grants (Gmail's shape): connect,
  refresh, ``invalid_grant`` revocation, and a poll per grant.
"""
