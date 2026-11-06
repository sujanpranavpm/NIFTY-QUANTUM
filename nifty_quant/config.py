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

    ``verified`` is the P2.1 honesty flag for broker-specific fees: brokerage is *not*
    a statutory rate, so strict financial validation requires either that the chosen
    broker configuration be marked ``verified=True`` (the user attests it matches their
    broker's published tariff) or that brokerage be excluded (``model='none'``).  An
    unverified non-zero brokerage is charged for modelling but never strict-verifiable.
    """

    model: str = "none"  # "none" | "flat" | "percentage"
    flat_per_order: float = 0.0
    percentage: float = 0.0  # fraction of premium turnover (0.0003 = 3 bps)
    cap_percentage: float = 0.0  # optional ceiling as a fraction of turnover; 0 disables
    verified: bool = False  # user attests this matches their broker's published tariff

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OptionsChargeRegime:
    """One effective-dated rate for one statutory component of an option trade.

    ``estimated`` is the honesty flag the directive demands: a rate that could not be
    read from an official source is marked ``True`` and every trade that depends on it
    is excluded from strict financial validation (fail-closed).  ``provenance`` records
    what the source actually says; ``source_url`` and ``circular_ref`` name the official
    document the value was read from; ``basis`` is the calculation basis (premium
    turnover / intrinsic value / taxable fee base); ``verification`` is the human status
    (``verified`` | ``estimated`` | ``reference-only`` | ``config``).  A rate is only
    ``estimated=False`` when its value was read from the cited official document.
    """

    component: str
    rate_type: str  # "premium_pct" | "intrinsic_pct" | "fee_pct" | "per_lot" (legacy)
    value: float
    effective_from: str  # ISO date, inclusive
    effective_to: str | None = None  # ISO date, inclusive; None = open-ended
    side: str = "both"  # "both" | "buy" | "sell"
    basis: str = "premium_turnover"  # "premium_turnover" | "intrinsic_value" | "fee_base"
    source_url: str = ""
    circular_ref: str = ""
    verification: str = "estimated"  # "verified" | "estimated" | "reference-only" | "config"
    provenance: str = ""
    estimated: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_options_charge_regimes() -> tuple[OptionsChargeRegime, ...]:
    """The effective-dated statutory schedule for NSE index options, sourced at Rs 0.

    P2.1 replaced the earlier fabricated ``Rs 20 per lot`` exchange fee with charges on
    *premium turnover*, each rate read from an official document cached under
    ``data/raw/p21_charges`` (fetched by ``scripts/_p21_charge_sources.py``).  P2.1.2
    resolved the September-2024 uniform-rate reference: FA73061 cites
    ``NSE/FA/64323`` (which returns HTTP 404); that is a circular-reference mismatch,
    likely a typographical error.  The official primary document located is
    ``NSE/FA/64232`` (dated September 27, 2024), matching the subject and effective
    charges referenced by FA73061; verbatim: *"Equity Options: Rs. 35.03 each side
    per lakh of premium value"*, *"applicable with effect from October 1, 2024"*.
    The 2024-10-01 row is now sourced from that circular (``verification=verified``),
    with the mismatch recorded in provenance.  The pre-Apr-2023 slab VALUES and the
    2021-01-01 start date are also sourced from NSE/FA/46730, but the slab-tier applied
    to a research book is an assumption, so the two slab-era rows honestly stay
    ``estimated`` and preserve the full slab table in their provenance.

    Sourcing notes (see ``logs/p21_excerpts_*.txt`` for the verbatim bytes):
      * Exchange transaction charge + NSE IPFT are levied *each side* on the traded
        premium.  The current flat regime (Rs 3,552.99 txn + Rs 0.01 IPFT per crore,
        wef 2026-03-01) is read straight from NSE circular FA73061.  The prior flat
        regime (Rs 3,503 + Rs 50 per crore, wef 2024-10-01) is read from NSE circular
        FA64232 (Sept-27-2024); FA73061 quotes it as ``64323``, a circular-reference
        mismatch now documented.  The 2023-04-01 slab revision comes from NSE circular FA56129,
        and the 2021-01-01 slab schedule comes from NSE circular FA46730.
      * STT on the option *seller* (premium) is verified across three eras: 0.0625%
        (Finance (No.2) Bill 2024 cl.155 "existing"), 0.10% wef 2024-10-01 (same clause +
        NSE STT page), 0.15% wef 2026-04-01 (NSE STT page, Finance Act 2026).
      * ``stt_exercised`` rows are reference-only: the execution engine exits at the
        premium sale and has no exercise path, so intrinsic-value STT is recorded for
        auditability but never applied (``basis='intrinsic_value'``).
      * SEBI turnover fee (0.0001% = Rs 10/crore) and stamp duty (0.003%, buyer) are read
        from the NSE published levies/stamp pages.
      * GST (18% on the taxable fee base) is a *config* tax knob, not a strictness-gating
        statutory charge: the row is documented ``estimated`` and the engine applies
        ``IndiaOptionsCosts.gst_pct``.
    """
    FA73061 = "https://nsearchives.nseindia.com/content/circulars/FA73061.pdf"
    FA64232 = "https://nsearchives.nseindia.com/content/circulars/FA64232.pdf"
    FA56129 = "https://nsearchives.nseindia.com/content/circulars/FA56129.pdf"
    FA46730 = "https://nsearchives.nseindia.com/content/circulars/FA46730.pdf"
    FB2425 = "https://www.indiabudget.gov.in/budget2024-25/doc/Finance_Bill.pdf"
    NSE_STT = ("https://www.nseindia.com/static/products-services/"
               "equity-derivatives-securities-transaction-tax")
    NSE_LEVIES = ("https://www.nseindia.com/static/invest/"
                  "first-time-investor-sebi-turnover-fees-stt-other-levies")
    NSE_STAMP = ("https://www.nseindia.com/static/invest/"
                 "first-time-investor-stamp-duty-charges-taxes")
    return (
        # ---- STT on the option premium, seller side (three verified eras) ----
        OptionsChargeRegime(
            "stt", "premium_pct", 0.000625, "2019-09-01", "2024-09-30", side="sell",
            basis="premium_turnover", source_url=FB2425,
            circular_ref="Finance (No.2) Bill 2024, cl.155 (existing rate)",
            verification="verified", estimated=False,
            provenance="cl.155: 'securities transaction tax on sale of an option in "
                       "securities is 0.0625 per cent. of the option premium' (existing)."),
        OptionsChargeRegime(
            "stt", "premium_pct", 0.001, "2024-10-01", "2026-03-31", side="sell",
            basis="premium_turnover", source_url=FB2425,
            circular_ref="Finance (No.2) Bill 2024 cl.155; NSE STT page (upto 2026-03-31)",
            verification="verified", estimated=False,
            provenance="cl.155 increases option-sale STT to 0.1 per cent of premium wef "
                       "1st October 2024; NSE STT page confirms 0.10% upto 2026-03-31."),
        OptionsChargeRegime(
            "stt", "premium_pct", 0.0015, "2026-04-01", None, side="sell",
            basis="premium_turnover", source_url=NSE_STT,
            circular_ref="NSE STT schedule, Finance Act 2026 (wef 2026-04-01)",
            verification="verified", estimated=False,
            provenance="NSE STT page: 'Sale of an option in securities 0.15 per cent "
                       "(w.e.f. April 1, 2026) / 0.10 per cent (upto March 31, 2026), "
                       "Seller', on the option premium."),
        # ---- STT on an exercised option, purchaser side, intrinsic value ----
        # Reference-only: the engine exits at the premium sale (no exercise path).
        OptionsChargeRegime(
            "stt_exercised", "intrinsic_pct", 0.00125, "2019-09-01", "2026-03-31",
            side="buy", basis="intrinsic_value", source_url=NSE_STT,
            circular_ref="NSE STT schedule row (b), wef 2019-09-01",
            verification="reference-only", estimated=False,
            provenance="NSE STT page: 'Sale of an option ... where option is exercised "
                       "0.125 per cent (upto March 31, 2026), Purchaser', on intrinsic "
                       "value. Recorded for auditability; NOT applied (no exercise path)."),
        OptionsChargeRegime(
            "stt_exercised", "intrinsic_pct", 0.0015, "2026-04-01", None,
            side="buy", basis="intrinsic_value", source_url=NSE_STT,
            circular_ref="NSE STT schedule row (b), Finance Act 2026",
            verification="reference-only", estimated=False,
            provenance="NSE STT page: exercised-option STT 0.15 per cent wef April 1 2026, "
                       "Purchaser, on intrinsic value. Reference-only; not applied."),
        # ---- NSE exchange transaction charge, each side, on traded premium ----
        OptionsChargeRegime(
            "exchange_txn", "premium_pct", 0.00053, "2021-01-01", "2023-03-31",
            basis="premium_turnover", source_url=FA46730,
            circular_ref="NSE/FA/46730, wef 2021-01-01 ('Revised' slab column)",
            verification="estimated", estimated=True,
            provenance="FA46730 slab schedule (wef Jan 1, 2021) - Equity Options, Rs per "
                       "lakh of premium: up to Rs 3 Cr Rs 2,500 flat/month; Rs 3-100 Cr "
                       "Rs 53.00; Rs 100-750 Cr Rs 50.50; Rs 750-1500 Cr Rs 45.50; Rs "
                       "1500-2000 Cr Rs 40.50; >Rs 2000 Cr Rs 33.00. Value + date sourced, "
                       "but the marginal slab for a research book is an assumption (no "
                       "member turnover), so the model uses the first-marginal slab "
                       "(Rs 53.00/lakh = 0.053%) as a conservative convention; estimated."),
        OptionsChargeRegime(
            "exchange_txn", "premium_pct", 0.0005, "2023-04-01", "2024-09-30",
            basis="premium_turnover", source_url=FA56129,
            circular_ref="NSE/FA/56129 (2023-03-24) 'Revised' slab column, wef 2023-04-01",
            verification="estimated", estimated=True,
            provenance="FA56129 slab schedule (wef Apr 1, 2023) - Equity Options, Rs per "
                       "lakh of premium: up to Rs 3 Cr Rs 2,500 flat/month; Rs 3-100 Cr "
                       "Rs 50.00; Rs 100-750 Cr Rs 47.50; Rs 750-1500 Cr Rs 42.50; Rs "
                       "1500-2000 Cr Rs 37.50; >Rs 2000 Cr Rs 30.00. Value + date sourced, "
                       "but the marginal slab for a research book is an assumption; model "
                       "uses first-marginal slab (Rs 50.00/lakh = 0.05%) as a conservative "
                       "convention; estimated."),
        OptionsChargeRegime(
            "exchange_txn", "premium_pct", 0.0003503, "2024-10-01", "2026-02-28",
            basis="premium_turnover", source_url=FA64232,
            circular_ref="NSE/FA/64232 (2024-09-27), wef 2024-10-01",
            verification="verified", estimated=False,
            provenance="FA64232 verbatim: 'Equity Options: Rs. 35.03 each side per lakh "
                       "of premium value' and 'The above revision in transaction charges "
                       "will be applicable with effect from October 1, 2024' = Rs "
                       "3,503/cr uniform (no slab) = 0.03503%. P2.1.2 note: FA73061 "
                       "cites this as 'NSE/FA/64323', a circular-reference mismatch, "
                       "likely a typographical error; the official primary document "
                       "located is 'NSE/FA/64232' (dated September 27, 2024), matching "
                       "the subject and effective charges referenced by FA73061."),
        OptionsChargeRegime(
            "exchange_txn", "premium_pct", 0.000355299, "2026-03-01", None,
            basis="premium_turnover", source_url=FA73061,
            circular_ref="NSE/FA/73061 (2026-02-27), wef 2026-03-01",
            verification="verified", estimated=False,
            provenance="FA73061 'Revised': Equity Options (on premium) Rs 3,552.99 per "
                       "crore each side, applicable wef March 1, 2026 = 0.0355299%."),
        # ---- NSE IPFT contribution, each side, on traded premium ----
        OptionsChargeRegime(
            "ipft", "premium_pct", 0.000000001, "2021-01-01", "2023-03-31",
            basis="premium_turnover", source_url=FA56129,
            circular_ref="NSE/FA/56129 (2023-03-24) 'existing' IPFT level",
            verification="verified", estimated=False,
            provenance="FA56129: IPFT 'existing Rs.0.01 per crore of the traded value "
                       "(premium value)' before the 2023-04-01 recalibration."),
        OptionsChargeRegime(
            "ipft", "premium_pct", 0.000005, "2023-04-01", "2026-02-28",
            basis="premium_turnover", source_url=FA56129,
            circular_ref="NSE/FA/56129 (2023-03-24), wef 2023-04-01",
            verification="verified", estimated=False,
            provenance="FA56129 recalibrated IPFT to Rs.50 per crore of traded premium "
                       "wef 2023-04-01; FA73061 confirms 'existing Rs 50 each side'."),
        OptionsChargeRegime(
            "ipft", "premium_pct", 0.000000001, "2026-03-01", None,
            basis="premium_turnover", source_url=FA73061,
            circular_ref="NSE/FA/73061 (2026-02-27), wef 2026-03-01",
            verification="verified", estimated=False,
            provenance="FA73061 rolled IPFT back to Rs.0.01 per crore (pre-April-2023 "
                       "level) wef 2026-03-01; 'Revised' column Rs 0.01 each side."),
        # ---- SEBI turnover fee (non-debt securities), each side ----
        OptionsChargeRegime(
            "sebi", "premium_pct", 0.000001, "2015-04-01", None,
            basis="premium_turnover", source_url=NSE_LEVIES,
            circular_ref="SEBI turnover fee, NSE published levies schedule",
            verification="verified", estimated=False,
            provenance="NSE levies page: SEBI turnover fee for securities other than debt "
                       "'0.0001 per cent of the price ... (Rs.10 per crore)' = 1e-6."),
        # ---- Stamp duty on a derivatives trade, buyer side ----
        OptionsChargeRegime(
            "stamp_duty", "premium_pct", 0.00003, "2020-07-01", None, side="buy",
            basis="premium_turnover", source_url=NSE_STAMP,
            circular_ref="Finance Act 2019 / Indian Stamp Act, wef 2020-07-01",
            verification="verified", estimated=False,
            provenance="NSE stamp page: 'Derivatives - Equity Options 0.003% Buyer' "
                       "(Finance Act 2019, levied wef July 1, 2020) = 3e-5."),
        # ---- GST on the taxable fee base (config tax knob, not strictness-gating) ----
        OptionsChargeRegime(
            "gst", "fee_pct", 0.18, "2015-04-01", None, basis="fee_base",
            circular_ref="GST on financial services (18%); no charge-circular carries it",
            verification="config", estimated=True,
            provenance="18% GST on the taxable fee base (brokerage + exchange txn + IPFT "
                       "+ SEBI). Standard rate applied from IndiaOptionsCosts.gst_pct; not "
                       "sourced from a charge circular this pass, so documented as a "
                       "config knob and excluded from the strict statutory gate."),
    )


@dataclass(frozen=True)
class IndiaOptionsCosts:
    """Index-option (long CE/PE) transaction cost model, effective-dated.

    Charges are computed on the premium turnover ``premium x lot_size x lots`` and are
    applied to the entry and exit legs separately, with partial exits supported.  GST is
    18% on the taxable fee base (brokerage + exchange transaction charge + NSE IPFT +
    SEBI turnover fee); it is a configurable tax knob, so ``gst_pct`` is applied by the
    engine and is not a strictness-gating statutory rate.
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
