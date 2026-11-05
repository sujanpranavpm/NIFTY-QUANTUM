"""P2 stage 8 - data + backtesting readiness gating (directive Parts 10-11).

This module holds *only* the qualification gate model, so the gate that authorises
P2 ingestion is unit-testable and cannot be waved through by enthusiasm.  It makes no
claim about any strategy's profitability and runs nothing: it decides whether each
intraday resolution (1m / 5m / 15m) is *permitted* to enter research, and it fails
closed - a resolution is qualified only when every gate is evidenced by an explicit
``True``.  A missing gate, a gate whose evidence is merely truthy (``1``, ``"yes"``)
rather than the literal ``True``, or a gate an evidencing test did not run at all all
resolve to *not passed*.

The gates mirror directive Part 10 one-for-one.  They are deliberately shared across
resolutions except for ``RESOLUTION_DATA_EVIDENCED``, which is per-resolution: passing
the 1m build says nothing about the 5m or 15m build, so qualifying one resolution never
qualifies another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

# The resolutions P2 produced candle datasets for.  Each is qualified independently.
RESOLUTIONS: tuple[str, ...] = ("1m", "5m", "15m")

# Directive Part 10 qualification gates, in reporting order.
CONTRACT_IDENTITY = "contract_identity"
PREMIUM_DATA_QUALIFIED = "premium_data_qualified"
MISSING_DATA_EXPLICIT = "missing_data_explicit"
TIMESTAMP_CONVENTION = "timestamp_convention"
TRANSACTION_COSTS_VERIFIED = "transaction_costs_verified"
EXECUTION_TESTED = "execution_tested"
ACCOUNTING_VALID = "accounting_valid"
RESULTS_REPRODUCIBLE = "results_reproducible"
WALKFORWARD_READY = "walkforward_ready"
HOLDOUT_PROTECTED = "holdout_protected"
MULTIPLE_TESTING_SAFEGUARDED = "multiple_testing_safeguarded"
RESOLUTION_DATA_EVIDENCED = "resolution_data_evidenced"

GATES: tuple[str, ...] = (
    CONTRACT_IDENTITY,
    PREMIUM_DATA_QUALIFIED,
    MISSING_DATA_EXPLICIT,
    TIMESTAMP_CONVENTION,
    TRANSACTION_COSTS_VERIFIED,
    EXECUTION_TESTED,
    ACCOUNTING_VALID,
    RESULTS_REPRODUCIBLE,
    WALKFORWARD_READY,
    HOLDOUT_PROTECTED,
    MULTIPLE_TESTING_SAFEGUARDED,
    RESOLUTION_DATA_EVIDENCED,
)

GATE_LABELS: dict[str, str] = {
    CONTRACT_IDENTITY: "Reliable, effective-dated contract identity",
    PREMIUM_DATA_QUALIFIED: "Qualified premium (candle) data with integrity verified",
    MISSING_DATA_EXPLICIT: "Missing data treated explicitly, never silently skipped",
    TIMESTAMP_CONVENTION: "Timestamp convention resolved and enforced",
    TRANSACTION_COSTS_VERIFIED: "Transaction costs verified (estimated rates excluded)",
    EXECUTION_TESTED: "Options execution engine tested (next-bar, collisions, gaps)",
    ACCOUNTING_VALID: "Accounting valid (symmetric P&L, no double-counted fees)",
    RESULTS_REPRODUCIBLE: "Reproducible results (locked deps, identical re-runs)",
    WALKFORWARD_READY: "Walk-forward infrastructure resolution-agnostic",
    HOLDOUT_PROTECTED: "Untouched holdout protected (disjoint, purged windows)",
    MULTIPLE_TESTING_SAFEGUARDED: "Multiple-testing safeguards available (BH, DSR)",
    RESOLUTION_DATA_EVIDENCED: "This resolution's candle dataset built and integrity-clean",
}


@dataclass(frozen=True)
class ReadinessAssessment:
    """The gate-by-gate verdict for one resolution.

    ``evidence`` maps gate name -> the value an evidencing check produced.  A gate is
    *passed* only when its evidence value is the literal ``True``; everything else
    (absent, ``False``, ``None``, a truthy non-bool) is a failure.  This strictness is
    the whole point: it makes it impossible to declare readiness on a partial or an
    un-run check.
    """

    resolution: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def passed(self, gate: str) -> bool:
        return self.evidence.get(gate) is True

    def failed_gates(self) -> list[str]:
        return [g for g in GATES if not self.passed(g)]

    @property
    def is_qualified(self) -> bool:
        """Qualified iff every gate is present and its evidence is exactly ``True``."""
        return not self.failed_gates()

    def as_row(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution,
            "qualified": self.is_qualified,
            "gates_passed": sum(1 for g in GATES if self.passed(g)),
            "gates_total": len(GATES),
            "failed_gates": ", ".join(self.failed_gates()),
        }


def assess_readiness(resolution: str, evidence: Mapping[str, Any]) -> ReadinessAssessment:
    """Build a :class:`ReadinessAssessment` for ``resolution``.

    An unrecognised gate name in ``evidence`` is ignored rather than silently counted,
    so a typo can never manufacture a qualification.
    """
    if resolution not in RESOLUTIONS:
        raise ValueError(f"unknown resolution {resolution!r}; expected one of {RESOLUTIONS}")
    clean = {g: bool(evidence.get(g) is True) for g in GATES}
    return ReadinessAssessment(resolution=resolution, evidence=clean)


def holdout_protected(dev_end: Any, purge_start: Any, purge_end: Any, oos_start: Any) -> bool:
    """``True`` only when the development, purge and holdout windows are strictly
    ordered and non-overlapping.  A missing purge band (the embargo that keeps leakage
    from the fit window out of the holdout) is itself a failure to protect the holdout.
    """
    if dev_end is None or purge_start is None or purge_end is None or oos_start is None:
        return False
    return bool(dev_end <= purge_start and purge_start <= purge_end and purge_end < oos_start)


def resolution_dataset_evidenced(resolution: str, derived_dir: Path | str,
                                 *, min_files: int = 1) -> bool:
    """``True`` only when ``derived_dir`` actually holds candle files for exactly this
    resolution (``{resolution}__*.parquet``).  It never infers readiness from another
    resolution having been built.
    """
    derived_dir = Path(derived_dir)
    if not derived_dir.exists():
        return False
    hits = list(derived_dir.glob(f"{resolution}__*.parquet"))
    return len(hits) >= min_files
