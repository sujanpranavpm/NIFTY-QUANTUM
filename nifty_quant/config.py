"""Central configuration objects.

Everything that can change a backtest result is explicit here. Nothing is
inherited implicitly from environment state, so a run is reproducible from its
serialised config.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Paths:
    """Filesystem layout. All paths are absolute and created on demand."""

    root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)

    @property
    def raw(self) -> Path:
        return self.root / "data" / "raw"

    @property
    def cache(self) -> Path:
        return self.root / "data" / "cache"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def universe_dir(self) -> Path:
        return Path(__file__).resolve().parent / "data" / "assets"

    def ensure(self) -> "Paths":
        for path in (self.raw, self.cache, self.artifacts, self.reports, self.logs):
            path.mkdir(parents=True, exist_ok=True)
        return self


PATHS = Paths()


# --------------------------------------------------------------------------
# Execution costs
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class IndiaEquityCosts:
    """Cash-equity transaction cost model for NSE delivery trades.

    Defaults reflect the published NSE/SEBI schedule for *delivery* equity
    trades on the cash segment. They are indicative: brokers differ, and the
    schedule changes. Every component is configurable and every component is
    reported separately in the exports, so no fee is hidden.

    Percentages are expressed as fractions (0.001 = 10 bps).
    """

    mode: str = "delivery"  # "delivery" | "intraday"
    brokerage_pct: float = 0.0003  # 0.03% or the flat cap, whichever is lower
    brokerage_flat: float = 20.0  # ₹20 per executed order
    stt_delivery_pct: float = 0.001  # 0.1% on both legs (delivery)
    stt_intraday_sell_pct: float = 0.00025  # 0.025% on sell leg only (intraday)
    exchange_txn_pct: float = 0.0000297  # NSE transaction charge
    sebi_turnover_pct: float = 0.000001  # ₹10 per crore
    stamp_duty_buy_pct: float = 0.00015  # 0.015% on the buy leg (delivery)
    stamp_duty_intraday_buy_pct: float = 0.00003  # 0.003% on the buy leg (intraday)
    gst_pct: float = 0.18  # on brokerage + exchange + SEBI
    dp_charge_per_sell: float = 13.5  # depository participant charge, sell leg

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SlippageConfig:
    """Slippage assumptions applied to the theoretical fill price."""

    model: str = "fixed_bps"  # "none" | "fixed_bps" | "volatility"
    bps: float = 5.0  # one-way half-spread in basis points
    vol_factor: float = 0.1  # used by the volatility model

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Options execution costs (effective-dated statutory schedule)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class BrokerageConfig:
    """Configurable brokerage - no broker is assumed unless the user says so.

    The neutral default is *zero* brokerage: leaving it unconfigured must never
    silently attribute a cost to a provider the research has not chosen.  When set,
    brokerage is the lesser of a flat per-order amount and a percentage cap of the
    premium turnover.
    """

    model: str = "none"  # "none" | "flat" | "percentage"
    flat_per_order: float = 0.0
    percentage: float = 0.0  # fraction of premium turnover (0.0003 = 3 bps)
    cap_percentage: float = 0.0  # optional ceiling as a fraction of turnover; 0 disables

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OptionsChargeRegime:
    """One effective-dated rate for one statutory component of an option trade.

    ``estimated`` is the honesty flag the directive demands: a rate that could not be
    read from a cached circular/spec is marked ``True`` and every trade that depends on
    it is excluded from strict financial validation (fail-closed).  ``provenance`` names
    the source (a circular reference) or records that no cached source carries it.
    """

    component: str
    rate_type: str  # "premium_pct" | "per_lot"
    value: float
    effective_from: str  # ISO date, inclusive
    effective_to: str | None = None  # ISO date, inclusive; None = open-ended
    side: str = "both"  # "both" | "buy" | "sell"
    provenance: str = ""
    estimated: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_options_charge_regimes() -> tuple[OptionsChargeRegime, ...]:
    """The offline statutory schedule for NSE index options.

    No cached circular or contract spec in ``data/raw/p1_nse`` carries these numbers -
    the audit script proves that - so *every* statutory rate is flagged ``estimated``
    with a provenance that says so.  The effective dates encode the direction of the
    published changes (STT on option premium rose from 0.05% to 0.1% on 2024-10-01) so
    that effective-date resolution is exercised, not assumed; a trade that uses any
    estimated rate is not strict-verifiable.
    """
    not_in_cache = "not present in cached NSE circulars/contract specs (offline audit); public statutory value"
    return (
        OptionsChargeRegime("stt", "premium_pct", 0.0005, "2015-04-01", "2024-09-30",
                            side="sell", provenance=not_in_cache, estimated=True),
        OptionsChargeRegime("stt", "premium_pct", 0.001, "2024-10-01", None,
                            side="sell", provenance=not_in_cache, estimated=True),
        OptionsChargeRegime("exchange_txn", "per_lot", 20.0, "2015-04-01", None,
                            provenance=not_in_cache, estimated=True),
        OptionsChargeRegime("sebi", "premium_pct", 0.000001, "2015-04-01", None,
                            provenance=not_in_cache, estimated=True),
        OptionsChargeRegime("stamp_duty", "premium_pct", 0.00003, "2015-04-01", None,
                            side="buy", provenance=not_in_cache, estimated=True),
    )


@dataclass(frozen=True)
class IndiaOptionsCosts:
    """Index-option (long CE/PE) transaction cost model, effective-dated.

    Charges are computed on the premium turnover ``premium x lot_size x lots`` and are
    applied to the entry and exit legs separately, with partial exits supported.  GST is
    18% on brokerage + exchange + SEBI, matching the equity model's taxable base.
    """

    brokerage: BrokerageConfig = field(default_factory=BrokerageConfig)
    gst_pct: float = 0.18
    slippage_pct: float = 0.0  # optional extra premium haircut; default neutral
    regimes: tuple[OptionsChargeRegime, ...] = field(
        default_factory=default_options_charge_regimes)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["regimes"] = [r.as_dict() for r in self.regimes]
        return d


# --------------------------------------------------------------------------
# Options execution scenarios (directive Part 7)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ExecutionScenario:
    """One set of execution assumptions for an option trade.

    A strategy must not qualify on the optimistic scenario alone: the three named
    presets (:data:"OPTIMISTIC", :data:"BASELINE", :data:"CONSERVATIVE") differ only in
    how generously a fill is priced and how strict the liquidity gate is, never in the
    decision logic (the stop-wins collision rule is fixed across every scenario).  The
    profitability delta reported between scenarios is exactly the difference these knobs
    make, so a result that survives only under :data:"OPTIMISTIC" is visibly fragile.

    ``adverse_slippage_bps`` is applied *against* the trader on every fill: a buy pays
    more, a sell receives less.  ``min_exit_volume`` is a liquidity floor below which an
    exit cannot be assumed to execute (a fill is rejected, never silently skipped).
    """

    name: str
    adverse_slippage_bps: float = 0.0
    min_exit_volume: float = 0.0  # 0 disables the liquidity gate

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


#: The three explicit, configurable scenarios.  Slippage and liquidity tighten monotonically
#: from A to C, so a well-behaved trade earns the most under A and the least under C.
OPTIMISTIC = ExecutionScenario("A_OPTIMISTIC", adverse_slippage_bps=0.0, min_exit_volume=0.0)
BASELINE = ExecutionScenario("B_BASELINE", adverse_slippage_bps=2.0, min_exit_volume=0.0)
CONSERVATIVE = ExecutionScenario("C_CONSERVATIVE", adverse_slippage_bps=5.0, min_exit_volume=1.0)

DEFAULT_EXECUTION_SCENARIOS: tuple[ExecutionScenario, ...] = (OPTIMISTIC, BASELINE, CONSERVATIVE)


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class EngineConfig:
    """Backtest engine assumptions.

    ``fill_at`` mirrors the upstream engine: orders submitted after a bar close
    are eligible from the *next* bar, filled at that bar's open. This is the
    single most important anti-look-ahead property of the whole system.
    """

    initial_capital: float = 100_000.0
    fill_at: str = "open"  # "open" | "close"
    # "as_published": keep the upstream sizing fraction (e.g. 18% of NAV per name)
    # "full_sleeve": allocate the entire sleeve to a signal (explicit adaptation)
    capital_utilisation: str = "as_published"
    # Hard cap on gross exposure as a fraction of NAV (portfolio mode only).
    max_gross_exposure: float = 1.0
    # Reject/clip buys that would overdraw cash instead of allowing leverage.
    cash_guard: bool = True
    # Optional participation cap; None disables the liquidity constraint.
    max_volume_participation: float | None = None
    # Extra bars skipped after the warm-up indicator window (listing gaps).
    warmup_bars: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationConfig:
    """Split and robustness settings."""

    dev_fraction: float = 0.70  # share of history used for development
    purge_bars: int = 25  # embargo between dev and out-of-sample
    walkforward_train_bars: int = 750
    walkforward_test_bars: int = 250
    sensitivity_steps: tuple[float, ...] = (-0.2, -0.1, 0.1, 0.2)
    regime_ma_bars: int = 200
    fdr_alpha: float = 0.10

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResearchConfig:
    """Top-level bundle persisted next to every result set."""

    engine: EngineConfig = field(default_factory=EngineConfig)
    costs: IndiaEquityCosts = field(default_factory=IndiaEquityCosts)
    slippage: SlippageConfig = field(default_factory=SlippageConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    intervals: tuple[str, ...] = ("1d",)
    history_start: str = "2010-01-01"
    benchmark_symbol: str = "^NSEI"
    universe: str = "NIFTY50"
    seed: int = 20260101

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def to_json(self, path: Path) -> None:
        path.write_text(json.dumps(self.as_dict(), indent=2), encoding="utf-8")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ResearchConfig":
        return cls(
            engine=EngineConfig(**payload.get("engine", {})),
            costs=IndiaEquityCosts(**payload.get("costs", {})),
            slippage=SlippageConfig(**payload.get("slippage", {})),
            validation=ValidationConfig(**payload.get("validation", {})),
            intervals=tuple(payload.get("intervals", ("1d",))),
            history_start=payload.get("history_start", "2010-01-01"),
            benchmark_symbol=payload.get("benchmark_symbol", "^NSEI"),
            universe=payload.get("universe", "NIFTY50"),
            seed=int(payload.get("seed", 20260101)),
        )


def load_config(path: str | os.PathLike[str] | None = None) -> ResearchConfig:
    """Load a config override file, falling back to defaults."""
    if path is None:
        return ResearchConfig()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return ResearchConfig.from_dict(payload)
