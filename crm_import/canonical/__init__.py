"""The canonical import format (docs/crm/importing.md, phase5-importer.md 1.2-1.3).

Export adapters write it, the importer reads it. Public API::

    from crm_import.canonical import validate_canonical, iter_records, write_records
"""

from .io import (
    CanonicalFormatError,
    count_rows,
    dump_record,
    file_digest,
    iter_records,
    make_record,
    read_manifest,
    write_manifest,
    write_records,
)
from .schema import (
    COVERAGE_REASONS,
    COVERAGE_VALUES,
    ISSUE_CODES,
    KNOWN_MAX_VERSION,
    MANIFEST_FILE,
    OBJECT_FILES,
    OBJECT_TYPES,
    SCHEMA_VERSION,
)
from .validate import Issue, ValidationResult, validate_canonical

__all__ = [
    "COVERAGE_REASONS",
    "COVERAGE_VALUES",
    "CanonicalFormatError",
    "ISSUE_CODES",
    "Issue",
    "KNOWN_MAX_VERSION",
    "MANIFEST_FILE",
    "OBJECT_FILES",
    "OBJECT_TYPES",
    "SCHEMA_VERSION",
    "ValidationResult",
    "count_rows",
    "dump_record",
    "file_digest",
    "iter_records",
    "make_record",
    "read_manifest",
    "validate_canonical",
    "write_manifest",
    "write_records",
]
