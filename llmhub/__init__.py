"""llmhub — one home for LLM configuration and routing.

Every LLM call the ERP makes resolves its endpoint, credential and model
("route") here: ``DB row → env → built-in default``. In ``gateway`` mode the
route is a logical *lane* on an OpenAI-compatible gateway (LiteLLM), so a
model swap is a lane re-point in the gateway's own admin UI — no code, no
deploy. In ``direct`` mode calls go straight to a provider.

Public surface:
- ``llmhub.conf``    — resolution (mode, base URL, key + its source, lanes)
- ``llmhub.client``  — ``complete`` / ``complete_async`` / ``embed`` /
  ``available_models`` / ``test_purpose``
- ``/erp/llm/``      — the staff-only LLM Settings page
- ``manage.py llmhub_check`` — headless smoke check
"""
