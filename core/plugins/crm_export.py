"""CRM export adapter contract (docs/crm/phase5-importer.md 1.4, 5.1).

An export adapter turns a provider's files (a file export or a recorded API
crawl) into the canonical import format (``crm_import.canonical``). It is a
pure function of its input files: no database, no network, no Django state
beyond the canonical schema, so it can run on an operator's laptop.

Reuses the payroll contract's conventions (docs/plugins.md section 6):
``AdapterError`` for failures (problem + cause + fix + docs link) and
``require_known_version`` on every versioned envelope it reads. Every adapter
ships :func:`assert_crm_export_contract` as a test over its fixture
directories.
"""

from __future__ import annotations

import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable, Mapping, Type

from core.plugins.payroll import AdapterError, require_known_version

__all__ = [
    "AdapterError",
    "BaseCrmExportAdapter",
    "assert_crm_export_contract",
    "require_known_version",
]

Manifest = dict


class BaseCrmExportAdapter(ABC):
    """Interface every CRM export adapter implements."""

    #: Stable provider id, e.g. "hubspot". It is the prefix of the canonical
    #: ``source`` ("hubspot:<account id>").
    provider_id: str = ""
    #: The canonical schema version this adapter writes.
    schema_version: int = 1
    #: The newest ``raw.schema_version`` of the provider payloads it wraps
    #: (communications' raw envelope). The importer passes it to
    #: ``communications.ingest.record`` as ``known_max_version``.
    raw_schema_version: int | None = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.provider_id:
            raise TypeError(f"{cls.__name__} must set provider_id (e.g. 'hubspot').")

    @abstractmethod
    def export(self, input_dir: Path, out_dir: Path, *, mapping: Mapping[str, Any] | None,
               time_zone: str | None) -> Manifest:
        """Write the canonical files into ``out_dir`` (manifest last) and
        return the manifest. Raise ``AdapterError`` when the input cannot be
        converted honestly (e.g. CSV date-times without ``time_zone``)."""


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in sorted(directory.rglob("*")) if p.is_file()
    }


def assert_crm_export_contract(
    adapter_cls: Type[BaseCrmExportAdapter],
    fixture_dirs: Iterable[Path | tuple[Path, Mapping[str, Any]]],
) -> None:
    """Contract test every adapter ships. Raises AssertionError naming the breach.

    ``fixture_dirs`` are recorded provider inputs (never live), each a path or
    ``(path, export kwargs)``. For each: the export validates with no errors
    (envelopes, sorted output, manifest present and hashed, coverage reasons
    from the closed list), the manifest names this provider and schema
    version, the returned manifest is the one on disk, and a second run is
    byte-identical.
    """
    from crm_import.canonical import KNOWN_MAX_VERSION, read_manifest, validate_canonical

    assert isinstance(adapter_cls.provider_id, str) and adapter_cls.provider_id, (
        "provider_id must be a non-empty string"
    )
    assert isinstance(adapter_cls.schema_version, int) and not isinstance(adapter_cls.schema_version, bool) \
        and 1 <= adapter_cls.schema_version <= KNOWN_MAX_VERSION, (
            f"schema_version must be an int between 1 and {KNOWN_MAX_VERSION}"
        )
    fixtures = list(fixture_dirs)
    assert fixtures, "ship at least one recorded fixture directory"
    for fixture in fixtures:
        input_dir, kwargs = (fixture if isinstance(fixture, tuple) else (fixture, {}))
        input_dir = Path(input_dir)
        kwargs = {"mapping": None, "time_zone": None, **dict(kwargs)}
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            returned = adapter_cls().export(input_dir, Path(first), **kwargs)
            result = validate_canonical(first)
            assert result.ok, (
                f"{adapter_cls.__name__} on {input_dir.name}: canonical output fails validation: "
                + "; ".join(str(i) for i in result.errors[:10])
            )
            manifest = read_manifest(first)
            assert returned == manifest, "export() must return the manifest it wrote"
            assert manifest["provider"] == adapter_cls.provider_id, (
                f"manifest provider {manifest['provider']!r} is not {adapter_cls.provider_id!r}"
            )
            assert manifest["schema_version"] == adapter_cls.schema_version, (
                "manifest schema_version differs from the adapter's"
            )
            assert manifest["adapter"]["schema_version"] == adapter_cls.schema_version, (
                "manifest adapter.schema_version differs from the adapter's"
            )
            adapter_cls().export(input_dir, Path(second), **kwargs)
            assert _tree(Path(first)) == _tree(Path(second)), (
                f"{adapter_cls.__name__} on {input_dir.name}: a second run is not byte-identical"
            )
