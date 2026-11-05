"""Persistent expiry-and-contract coverage registry.

Why a registry and not a one-off report
---------------------------------------
Part 3 of the remediation directive requires that *every* NIFTY contract the
exchange listed, and *every* contract the provider actually published, be recorded
with an explicit coverage status and a reason - never silently skipped, never
merged into an ambiguous "missing".  The completeness module (``completeness.py``)
classifies individual cells; this module turns those classifications into a durable,
queryable registry keyed at two levels:

``expiry``
    one row per (exchange-listed or provider-published) NIFTY option expiry:
    ``PRESENT`` / ``MISSING_FILE`` / ``PUBLISHED_NOT_CACHED`` / ``TRUNCATED_TAIL`` /
    ``UNKNOWN``.
``contract-day``
    one row per (trading day x strike x option type) cell inside the cached files:
    ``PRESENT`` / ``NO_TRADE`` / ``MISSING_CONTRACT`` / ``MISSING_CANDLE`` /
    ``TRUNCATED_SESSION`` / ``INACTIVE_CONTRACT`` / ``PRESENT_WITHOUT_OI`` /
    ``UNKNOWN``.

The registry stores the *aggregate* verdicts the directive lists, not per-bar rows
(millions), so the CSV stays a few tens of thousands of cells and reconciles line for
line against the completeness scan it is built from.

Honesty rules encoded here
--------------------------
* A cell is only ever as certain as its arbiter.  2021-2023 days have no official
  F&O report in the free archive, so they are ``UNKNOWN``, never inferred complete.
* Missing candles are *detected* from gaps in the traded-minute sequence; nothing is
  fabricated to fill them and no nearby strike is substituted.
* Incomplete intraday highs/lows are flagged only where an independent official
  ``High``/``Low`` exists to compare against, and only when out of an explicit
  tolerance; absent evidence leaves the flag ``None`` rather than ``True``.
* The maximum observed coverage is reported as measured, never extended.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

from .completeness import (
    DAY_INACTIVE, DAY_MISSING, DAY_MISSING_TRADES, DAY_NO_OI, DAY_NO_TRADES,
    DAY_PRESENT, DAY_TRUNCATED, FILE_ABSENT, FILE_PRESENT, FILE_TRUNCATED,
)
from .timestamps import clock_label

# -- registry vocabulary (the directive's exact labels) ----------------------
PRESENT = "PRESENT"
NO_TRADE = "NO_TRADE"
MISSING_FILE = "MISSING_FILE"
MISSING_CONTRACT = "MISSING_CONTRACT"
MISSING_CANDLE = "MISSING_CANDLE"
INACTIVE_CONTRACT = "INACTIVE_CONTRACT"
TRUNCATED_SESSION = "TRUNCATED_SESSION"
TRUNCATED_TAIL = "TRUNCATED_TAIL"
PRESENT_WITHOUT_OI = "PRESENT_WITHOUT_OI"
UNKNOWN = "UNKNOWN"
PUBLISHED_NOT_CACHED = "PUBLISHED_NOT_CACHED"

#: Provider statuses that mean "the file should exist here but does not".
MISSING_STATUSES = frozenset({MISSING_FILE, MISSING_CONTRACT, MISSING_CANDLE,
                              TRUNCATED_SESSION, TRUNCATED_TAIL})
#: Statuses that are legitimate absences, not defects.
ABSENT_OK_STATUSES = frozenset({NO_TRADE, INACTIVE_CONTRACT, PUBLISHED_NOT_CACHED})

EXPIRY_LEVEL = "expiry"
CONTRACT_DAY_LEVEL = "contract-day"

#: A traded-minute sequence is judged on this fixed session grid; a strike missing
#: minutes that its own first..last span brackets has candle gaps.
SESSION_MINUTES = 376
#: An archive high/low is flagged when it exceeds the official range by this many
#: rupees - larger than the 0.05 tick so ordinary rounding is never a flag.
HIGH_LOW_TOLERANCE = 0.05

# Map the completeness verdicts onto the registry vocabulary so the two never drift.
_DAY_TO_REGISTRY = {
    DAY_PRESENT: PRESENT,
    DAY_NO_TRADES: NO_TRADE,
    DAY_MISSING: MISSING_CONTRACT,
    DAY_MISSING_TRADES: MISSING_CANDLE,
    DAY_TRUNCATED: TRUNCATED_SESSION,
    DAY_INACTIVE: INACTIVE_CONTRACT,
    DAY_NO_OI: PRESENT_WITHOUT_OI,
}
_FILE_TO_REGISTRY = {
    FILE_PRESENT: PRESENT,
    FILE_ABSENT: MISSING_FILE,
    FILE_TRUNCATED: TRUNCATED_TAIL,
}


def registry_status_for_day(status: str) -> str:
    """The registry label for a ``completeness`` day verdict (raises on unknown)."""
    try:
        return _DAY_TO_REGISTRY[status]
    except KeyError:
        raise ValueError(f"no registry mapping for completeness day status {status!r}")


def registry_status_for_file(status: str) -> str:
    """The registry label for a ``completeness`` file verdict (raises on unknown)."""
    try:
        return _FILE_TO_REGISTRY[status]
    except KeyError:
        raise ValueError(f"no registry mapping for completeness file status {status!r}")


def high_low_within_official(archive_high, archive_low,
                             official_high, official_low,
                             tolerance: float = HIGH_LOW_TOLERANCE):
    """Is the archive intraday range contained in the official range?

    Returns ``True``/``False`` only when an independent official range exists to
    compare against; otherwise ``None`` - never a default ``True``.  A single
    out-of-range extreme (archive print above the official high, or below the
    official low, past the tolerance) is a suspected incomplete-high/low.
    """
    vals = (archive_high, archive_low, official_high, official_low)
    if any(v is None or v != v for v in vals):        # None or NaN
        return None
    ah, al, oh, ol = (float(v) for v in vals)
    return bool(ah <= oh + tolerance and al >= ol - tolerance)


def missing_candle_minutes(labels: Iterable[str]) -> int:
    """Gaps in a strike's own traded-minute sequence within its first..last span.

    Only the contract's own bracket is examined, so a quietly-closing contract is not
    counted as having gaps; a strike that traded every minute scores 0.
    """
    labels = sorted({clock_label(x) for x in labels})
    if len(labels) < 2:
        return 0
    first = pd.Timestamp("1900-01-01 " + labels[0])
    last = pd.Timestamp("1900-01-01 " + labels[-1])
    span = int((last - first).total_seconds() // 60) + 1
    return max(span - len(labels), 0)


def minute_gap_from_span(first_clock: str, last_clock: str, n_labels: int) -> int:
    """Vectorised form of :func:`missing_candle_minutes` from pre-computed clock bounds.

    The builder derives ``n_labels`` and the ``HH:MM`` first/last label with one
    grouped pass over each file (not a per-stamp ``strftime``), so a full-archive scan
    stays linear in the number of cells rather than the number of bars.
    """
    if not first_clock or not last_clock or n_labels is None or n_labels < 2:
        return 0
    h1, m1 = (int(x) for x in str(first_clock).split(":")[:2])
    h2, m2 = (int(x) for x in str(last_clock).split(":")[:2])
    span = (h2 * 60 + m2) - (h1 * 60 + m1) + 1
    return max(span - int(n_labels), 0)


@dataclass(frozen=True)
class CoverageEntry:
    """One registry cell - an expiry or a contract-day, with its verdict and evidence."""

    level: str
    expiry: str
    status: str
    trading_day: str = "*"
    strike: int = 0
    option_type: str = "*"
    expected: bool = True          # the exchange listed this contract/day
    observed: bool = False         # the provider carries bars for it
    reason: str = ""
    evidence: str = ""
    arbiter: bool = False          # an official report exists for this day
    high_low_flag: bool | None = None   # True=in-range, False=suspect, None=no evidence
    candle_gaps: int = 0

    def key(self) -> tuple:
        return (self.level, self.trading_day, self.expiry, self.strike, self.option_type)


class CoverageRegistry:
    """The accumulated coverage entries, with the guards the directive demands."""

    def __init__(self, entries: Iterable[CoverageEntry] = ()) -> None:
        self.entries: list[CoverageEntry] = []
        for entry in entries:
            self.add(entry)

    def add(self, entry: CoverageEntry) -> None:
        self.entries.append(entry)

    def __len__(self) -> int:
        return len(self.entries)

    def frame(self) -> pd.DataFrame:
        rows = [asdict(e) for e in self.entries]
        columns = ["level", "trading_day", "expiry", "strike", "option_type", "status",
                   "expected", "observed", "arbiter", "candle_gaps", "high_low_flag",
                   "reason", "evidence"]
        if not rows:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame(rows)[columns]

    def counts(self, level: str | None = None,
               expected: bool | None = None) -> dict[str, int]:
        """Per-status counts, optionally filtered by level and by whether the exchange
        expected the cell.  The completeness report enumerates only exchange-listed
        (``expected=True``) cells, so reconciliation is done on that subset."""
        out: dict[str, int] = {}
        for e in self.entries:
            if level is not None and e.level != level:
                continue
            if expected is not None and e.expected != expected:
                continue
            out[e.status] = out.get(e.status, 0) + 1
        return out

    def write_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.frame().to_csv(path, index=False, lineterminator="\n")

    @classmethod
    def from_csv(cls, path: Path) -> "CoverageRegistry":
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        entries = []
        for row in frame.itertuples(index=False):
            entries.append(CoverageEntry(
                level=row.level, trading_day=row.trading_day, expiry=row.expiry,
                strike=int(row.strike), option_type=row.option_type, status=row.status,
                expected=_truthy(row.expected), observed=_truthy(row.observed),
                arbiter=_truthy(row.arbiter), candle_gaps=int(row.candle_gaps or 0),
                high_low_flag=_parse_flag(row.high_low_flag),
                reason=row.reason, evidence=row.evidence))
        return cls(entries)

    def unclassified_missing(self) -> list[CoverageEntry]:
        """Every expected-but-not-observed contract-day that lacks a reason.

        The directive forbids silently skipping a contract, so a missing entry that
        carries no explanation is the defect this guard is built to catch.
        """
        out = []
        for e in self.entries:
            if e.level != CONTRACT_DAY_LEVEL:
                continue
            if e.expected and not e.observed and e.status in MISSING_STATUSES \
                    and not e.reason.strip():
                out.append(e)
        return out

    def assert_no_silent_skip(self) -> None:
        bad = self.unclassified_missing()
        if bad:
            sample = bad[:5]
            raise AssertionError(
                f"{len(bad)} expected contracts are classified missing with no reason; "
                f"nothing may be skipped silently. e.g. "
                + "; ".join(f"{e.trading_day} {e.expiry} {e.strike}{e.option_type}"
                            for e in sample))

    def max_observed_coverage(self) -> dict:
        """The honest ceiling of usable contract-day coverage, measured not extended.

        ``present_share`` is the fraction of arbitrated contract-day cells that are
        genuinely present; only cells with an official arbiter are counted, because a
        2021-2023 cell can neither be confirmed present nor meaningfully called missing.
        """
        arbitrated = [e for e in self.entries
                      if e.level == CONTRACT_DAY_LEVEL and e.arbiter]
        present = [e for e in arbitrated if e.status in (PRESENT, PRESENT_WITHOUT_OI)]
        total = len(arbitrated)
        return {
            "arbitrated_contract_days": total,
            "present_contract_days": len(present),
            "present_share": round(100.0 * len(present) / total, 2) if total else None,
            "expiries": len({e.expiry for e in self.entries
                             if e.level == EXPIRY_LEVEL}),
            "expiries_present": len({e.expiry for e in self.entries
                                     if e.level == EXPIRY_LEVEL and e.status == PRESENT}),
        }

    def reconcile(self, expected_counts: dict[str, int], level: str) -> dict:
        """Compare this registry's per-status counts against another scan's counts.

        Returns the per-status difference plus an ``ok`` flag; the builder fails closed
        if ``ok`` is False so the registry can never drift from the completeness report
        it is derived from.
        """
        mine = self.counts(level)
        diffs = {}
        for status in set(mine) | set(expected_counts):
            if mine.get(status, 0) != expected_counts.get(status, 0):
                diffs[status] = {"registry": mine.get(status, 0),
                                 "report": expected_counts.get(status, 0)}
        return {"ok": not diffs, "registry": mine, "report": expected_counts,
                "differences": diffs}


def _truthy(text) -> bool:
    if isinstance(text, bool):
        return text
    return str(text).strip().lower() in {"true", "1", "yes"}


def _parse_flag(text):
    """Three-state high/low flag from CSV text: ''/nan -> None, else bool."""
    s = str(text).strip().lower()
    if s in ("", "nan", "none"):
        return None
    return s in {"true", "1", "yes"}
