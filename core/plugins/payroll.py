"""Payroll/source adapter contract (Step 5, XD-1…XD-5).

Payroll is a sync, not a module: adapters pull provider runs, the ERP books
the cost lines through ``post_balanced_entry``. Provider payload shapes are
unknown until an adapter exists (ED-9) — so this module pins the *contract*
(base classes, report shape, fixture layout, error shape) while the
PayrollRun/CostLine models wait for the first real adapter.

Conventions every adapter follows:
- ``schema_version`` lives on the raw-payload envelope
  ``{schema_version, provider, payload}`` (XD-4). Readers reject
  unknown-newer versions with a named error + docs link.
- Fixtures live at ``golden_erp/addons/<id>/fixtures/`` as ``*.csv`` (or provider
  native) + ``*.expected.json``; CI fails on live sockets (XD-5).
- Every error message states problem + cause + fix + docs link (XD-9).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Type


class AdapterError(Exception):
    """A provider call failed. Message = problem + cause + fix + docs link."""


@dataclass(frozen=True)
class PayrollCostLine:
    """One bookable cost line: account key resolved against the company chart."""

    account_key: str  # e.g. "wages", "employer_tax", "benefits", "fees"
    amount: Decimal
    memo: str = ""


@dataclass(frozen=True)
class PayrollRunDraft:
    """A provider pay run, verbatim payload attached for replay-from-raw."""

    provider_run_id: str
    period_start: str  # ISO date
    period_end: str  # ISO date
    gross: Decimal = Decimal("0")
    net: Decimal = Decimal("0")
    raw_envelope: Dict[str, Any] = field(default_factory=dict)
    cost_lines: List[PayrollCostLine] = field(default_factory=list)

    def __post_init__(self):
        envelope = self.raw_envelope or {}
        if envelope.get("schema_version") is None:
            raise AdapterError(
                "Provider run arrived without schema_version on its raw "
                "envelope — cause: adapter omitted the envelope. Fix: wrap "
                "payloads as {schema_version, provider, payload}. "
                "See docs/plugins.md#payroll-adapters."
            )


class BasePayrollProvider(ABC):
    """Interface every payroll adapter implements. Copy the stub, not the docs."""

    #: Stable id, e.g. "gusto", "rippling", "csv_stub".
    provider_id: str = ""
    #: Bump when the provider's payload shape changes; readers reject
    #: unknown-newer versions instead of misreading them.
    schema_version: int = 1

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.provider_id:
            raise TypeError(
                f"{cls.__name__} must set provider_id (e.g. 'gusto')."
            )

    @abstractmethod
    def pull_runs(self, since: str) -> List[PayrollRunDraft]:
        """Runs with period_end >= since (ISO date). Verbatim raw attached."""

    @abstractmethod
    def fetch_raw(self, provider_run_id: str) -> Dict[str, Any]:
        """Re-fetch one run's raw envelope (replay-from-raw)."""


class BaseSourceAdapter(ABC):
    """Interface for shop-platform / export-file source adapters (Step 3+)."""

    source_id: str = ""
    schema_version: int = 1

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.source_id:
            raise TypeError(
                f"{cls.__name__} must set source_id (e.g. 'shopify')."
            )

    @abstractmethod
    def pull_rows(self, since: str) -> List[Dict[str, Any]]:
        """Raw rows (headers + values) with period >= since."""


@dataclass
class RowFate:
    """One row's fate in an adapter run — the dry-run report line item."""

    row_ref: str
    fate: str  # created | updated | skipped_closed | skipped_unbalanced | error
    detail: str = ""


@dataclass
class AdapterReport:
    """Mirrors ImportReport: what the run did, row by row (XD-2)."""

    created: int = 0
    updated: int = 0
    skipped_closed: int = 0
    skipped_unbalanced: int = 0
    errors: int = 0
    rows: List[RowFate] = field(default_factory=list)

    def record(self, row_ref: str, fate: str, detail: str = ""):
        self.rows.append(RowFate(row_ref, fate, detail))
        if fate == "created":
            self.created += 1
        elif fate == "updated":
            self.updated += 1
        elif fate == "skipped_closed":
            self.skipped_closed += 1
        elif fate == "skipped_unbalanced":
            self.skipped_unbalanced += 1
        else:
            self.errors += 1

def require_known_version(envelope: Dict[str, Any], known_max: int) -> int:
    """Reader-side guard (XD-4): unknown-newer payloads never parse.

    Adapters call this before reading any provider payload, so provider
    drift fails loudly with a named error instead of misreading fields.
    """
    version = envelope.get("schema_version")
    if not isinstance(version, int) or version > known_max:
        raise AdapterError(
            f"Payload schema_version {version!r} is newer than the reader "
            f"(max {known_max}) — cause: provider drifted. Fix: upgrade "
            "the adapter before importing. "
            "See docs/plugins.md#payroll-adapters."
        )
    return version


def assert_provider_contract(
    provider_cls: Type[BasePayrollProvider], fixture_runs: List[Dict[str, Any]]
) -> None:
    """Contract test every adapter ships (XD-5). Raises AssertionError naming
    the breach. Fixture runs are recorded provider payloads (never live)."""
    assert isinstance(provider_cls.provider_id, str) and provider_cls.provider_id, (
        "provider_id must be a non-empty string"
    )
    assert isinstance(provider_cls.schema_version, int) and (
        provider_cls.schema_version >= 1
    ), "schema_version must be a positive int"
    provider = provider_cls()
    runs = provider.pull_runs("2000-01-01")
    assert isinstance(runs, list), "pull_runs must return a list"
    for run in runs:
        assert isinstance(run, PayrollRunDraft), (
            f"pull_runs must yield PayrollRunDraft, got {type(run).__name__}"
        )
        assert run.provider_run_id, "provider_run_id must be non-empty"
        raw = provider.fetch_raw(run.provider_run_id)
        assert isinstance(raw, dict) and raw.get("schema_version") == (
            provider_cls.schema_version
        ), "fetch_raw must return the versioned envelope"
    assert fixture_runs, "ship at least one recorded fixture run"
    for fixture in fixture_runs:
        assert "provider_run_id" in fixture, (
            "fixture runs need provider_run_id"
        )
