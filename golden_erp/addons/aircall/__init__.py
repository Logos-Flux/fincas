"""The Aircall CRM adapter (docs/crm/phase6-adapters.md 3.4, step 5).

Inbound only: every Aircall call becomes one ``Communication`` of kind
``call`` (direction, duration, outcome, the agent as ``performed_by``), by
webhook (``call.ended``, ``call.commented``) or by the poll backstop, both
through one ``ingest``. **Recordings stay at the provider**: the call's
``recording`` / ``voicemail`` / ``asset`` links live verbatim in the
``CommunicationRaw`` payload as opaque provider references, and golden never
requests them.

Install: ``GOLDEN_PLUGINS = [..., "golden_erp.addons.aircall.plugin.AircallPlugin"]``.
Operator notes: docs/crm/adapters.md, "Aircall".
"""
