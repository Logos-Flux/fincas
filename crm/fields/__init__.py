"""CRM field audit tooling (docs/crm/phase4-fields.md section 1).

``inventory`` parses and validates a provider field inventory CSV;
``counts`` fills its usage counts from local export files. Neither calls a
provider API (the Phase 5 importer owns credentials and rate limits).
"""
