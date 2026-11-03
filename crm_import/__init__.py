"""CRM data-migration importer (docs/crm/phase5-importer.md).

Step 1 ships the canonical import format only (``crm_import.canonical``):
the source-agnostic JSONL files an export adapter writes and the importer
reads. Jobs, staging, matching and appliers arrive in later steps.
"""
