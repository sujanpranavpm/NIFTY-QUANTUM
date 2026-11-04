"""Validation orchestrator: evidence, checks and a verdict per strategy.

The single most important design decision here is that a strategy is *not*
ranked by its backtest return. It accumulates evidence from independent tests
(holdout, walk-forward, breadth, concentration, parameter plateau, multiplicity)
and each test either passes or fails against a threshold declared up front in this
module. The verdict that reaches the dashboard and the signal engine is derived
from those thresholds only.

Nothing in this module fetches data or invents numbers; every input comes from a
recorded run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import json

import numpy as np
import pandas as pd

from nifty_quant.analytics.metrics import periods_per_year
from nifty_quant.config import ResearchConfig
from nifty_quant.research.results import ResearchResults
from nifty_quant.validation import regime as regime_mod
from nifty_quant.validation.consistency import dev_oos_table, symbol_consistency
from nifty_quant.validation.sensitivity import run_sensitivity
from nifty_quant.validation.splits import Split, dev_oos_split
from nifty_quant.validation.stats import (
    benjamini_hochberg,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
)
from nifty_quant.validation.walkforward import (
    generate_folds,
    walkforward_summary,
    walkforward_table,
    window_metrics,
)

# ---- thresholds (declared once, applied everywhere) -----------------------
MIN_TRADES = 30
MIN_OOS_RETENTION = 0.0
MIN_WF_POSITIVE_SHARE = 0.50
MAX_SYMBOL_CONCENTRATION = 0.40
MIN_PLATEAU_SHARE = 0.60
MIN_PSR = 0.95
MIN_DSR = 0.95
MIN_REGIME_COVERAGE = 2

CRITICAL_CHECKS = (
    "enough_trades",
    "oos_profitable",
    "breadth",
    "fdr_significant",
)


@dataclass
class ValidationReport:
    """Everything the validation stage produced, ready for export."""

    split: Split
    dev_oos: pd.DataFrame
    walkforward: pd.DataFrame
    walkforward_summary: pd.DataFrame
    sensitivity: pd.DataFrame
    sensitivity_detail: pd.DataFrame
    regime_equity: pd.DataFrame
    regime_trades: pd.DataFrame
    consistency: pd.DataFrame
    robustness: pd.DataFrame
    checks: pd.DataFrame
    verdicts: pd.DataFrame
    metadata: dict[str, Any] = field(default_factory=dict)
    strict_split: pd.DataFrame = field(default_factory=pd.DataFrame)

    def to_frames(self) -> dict[str, pd.DataFrame]:
        return {
            "split": pd.DataFrame([self.split.as_row()]),
            "dev_oos": self.dev_oos,
            "strict_split": self.strict_split,
            "walkforward": self.walkforward,
            "walkforward_summary": self.walkforward_summary,
            "sensitivity": self.sensitivity,
            "sensitivity_detail": self.sensitivity_detail,
            "regime_equity": self.regime_equity,
            "regime_trades": self.regime_trades,
            "consistency": self.consistency,
            "robustness": self.robustness,
            "checks": self.checks,
            "verdicts": self.verdicts,
        }

    def grounded_strategies(self) -> list[str]:
        """Strategies whose verdict is ``supported`` (usable by the signal engine)."""
        if self.verdicts.empty:
            return []
        return sorted(self.verdicts.loc[self.verdicts["verdict"] == "supported", "strategy_id"])

    def summary_markdown(self) -> str:
        if self.verdicts.empty:
            return "_no validation results_"
        frame = self.verdicts[
            ["strategy_id", "verdict", "checks_passed", "checks_failed", "failed_checks"]
        ].copy()
        header = "| " + " | ".join(frame.columns) + " |"
        rule = "|" + "---|" * len(frame.columns)
        lines = [header, rule]
        for _, row in frame.iterrows():
            lines.append("| " + " | ".join(str(row[c]) for c in frame.columns) + " |")
        return "\n".join(lines)


def run_validation(
    results: ResearchResults,
    *,
    benchmark: pd.DataFrame | None = None,
    config: ResearchConfig | None = None,
    split: Split | None = None,
    sensitivity_symbols: list[str] | None = None,
    bars: dict[str, pd.DataFrame] | None = None,
    run_walkforward_checks: bool = True,
    run_sensitivity_checks: bool = True,
    run_strict_split: bool = True,
    strict_split_symbols: list[str] | None = None,
) -> ValidationReport:
    """Build the full validation report for an existing set of runs.

    Args:
        results: completed research runs (one entry per strategy).
        benchmark: index bars used to label market regimes.
        split: pre-computed development/out-of-sample split; derived from the runs
            if omitted.
        sensitivity_symbols / bars: the grid needs bars to re-run perturbed
            parameter sets. Without them the sensitivity stage is skipped and the
            corresponding check is marked untested rather than passed.
        run_walkforward_checks: slicing the runs into folds is cheap; keep on.
        run_sensitivity_checks: re-running the grid is not cheap; cost is
            ``len(sensitivity_symbols) x variants x 1 backtest``.
    """
    config = config or ResearchConfig()
    validation = config.validation
    if not results.runs:
        raise ValueError("no runs to validate")

    reference_run = next(iter(results.runs.values()))
    if split is None:
        index = pd.DatetimeIndex(reference_run.combined_equity().index)
        split = dev_oos_split(
            index,
            dev_fraction=validation.dev_fraction,
            purge_bars=validation.purge_bars,
        )

    dev_oos = dev_oos_table(results, split)

    folds = generate_folds(
        pd.DatetimeIndex(reference_run.combined_equity().index),
        train_bars=validation.walkforward_train_bars,
        test_bars=validation.walkforward_test_bars,
    )
    wf_table = walkforward_table(results, folds) if run_walkforward_checks else pd.DataFrame()
    wf_summary = walkforward_summary(wf_table) if not wf_table.empty else pd.DataFrame()

    consistency = symbol_consistency(results.symbol_metrics())

    regime_equity = pd.DataFrame()
    regime_trades = pd.DataFrame()
    if benchmark is not None and not benchmark.empty:
        regimes = regime_mod.classify(benchmark, ma_bars=validation.regime_ma_bars)
        equity_rows, trade_rows = [], []
        for strategy_id, run in results.runs.items():
            equity = run.combined_equity()
            if equity.empty:
                continue
            labels = regime_mod.align_regimes(regimes, pd.DatetimeIndex(equity.index))
            stats = regime_mod.regime_equity_stats(equity, labels)
            if not stats.empty:
                stats.insert(0, "strategy_id", strategy_id)
                equity_rows.append(stats)
            trade_stats = regime_mod.regime_trade_stats(run.trades(), labels)
            if not trade_stats.empty:
                trade_stats.insert(0, "strategy_id", strategy_id)
                trade_rows.append(trade_stats)
        regime_equity = pd.concat(equity_rows, ignore_index=True) if equity_rows else pd.DataFrame()
        regime_trades = pd.concat(trade_rows, ignore_index=True) if trade_rows else pd.DataFrame()

    sensitivity_detail = pd.DataFrame()
    sensitivity_summary = pd.DataFrame()
    if run_sensitivity_checks and bars:
        symbols = sensitivity_symbols or list(bars)[:5]
        sample = {symbol: bars[symbol] for symbol in symbols if symbol in bars}
        if sample:
            chosen = results.strategy_ids
            details, summaries = [], []
            for strategy_id in chosen:
                outcome = run_sensitivity(
                    strategy_id,
                    sample,
                    steps=validation.sensitivity_steps,
                    config=config,
                    interval="1d",
                )
                details.append(outcome.table)
                if not outcome.summary.empty:
                    summaries.append(outcome.summary)
            sensitivity_detail = pd.concat(details, ignore_index=True) if details else pd.DataFrame()
            sensitivity_summary = pd.concat(summaries, ignore_index=True) if summaries else pd.DataFrame()

    robustness = _robustness(results, consistency, fdr_alpha=validation.fdr_alpha)

    strict_split = pd.DataFrame()
    if run_strict_split and bars:
        strict_split = strict_split_check(
            results,
            bars,
            split,
            config=config,
            symbols=strict_split_symbols,
        )

    checks = _checks(
        results=results,
        dev_oos=dev_oos,
        wf_summary=wf_summary,
        consistency=consistency,
        sensitivity_summary=sensitivity_summary,
        robustness=robustness,
        regime_trades=regime_trades,
        strict_split=strict_split,
    )
    verdicts = _verdicts(checks)

    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "strategies": ",".join(results.strategy_ids),
        "dev_fraction": validation.dev_fraction,
        "purge_bars": validation.purge_bars,
        "fold_count": len(folds),
        "sensitivity_symbols": ",".join(sensitivity_symbols or []),
        "thresholds": {
            "min_trades": MIN_TRADES,
            "min_oos_retention": MIN_OOS_RETENTION,
            "min_wf_positive_share": MIN_WF_POSITIVE_SHARE,
            "max_symbol_concentration": MAX_SYMBOL_CONCENTRATION,
            "min_plateau_share": MIN_PLATEAU_SHARE,
            "min_psr": MIN_PSR,
            "min_dsr": MIN_DSR,
            "fdr_alpha": validation.fdr_alpha,
        },
    }
    return ValidationReport(
        split=split,
        dev_oos=dev_oos,
        walkforward=wf_table,
        walkforward_summary=wf_summary,
        sensitivity=sensitivity_summary,
        sensitivity_detail=sensitivity_detail,
        regime_equity=regime_equity,
        regime_trades=regime_trades,
        consistency=consistency,
        robustness=robustness,
        checks=checks,
        verdicts=verdicts,
        metadata=metadata,
        strict_split=strict_split,
    )


def strict_split_check(
    results: ResearchResults,
    bars: dict[str, pd.DataFrame],
    split: Split,
    *,
    config: ResearchConfig | None = None,
    symbols: list[str] | None = None,
    max_symbols: int = 5,
    tolerance: float = 0.05,
) -> pd.DataFrame:
    """Re-run each strategy from the held-out boundary and compare.

    The report's dev/OOS table measures the held-out window by slicing one
    continuous run, which carries any position that was open at the boundary into
    the window. This function checks that shortcut: it restarts the strategy flat
    at ``split.oos_start`` and reports the difference.

    Args:
        bars: full-history bars (pre-start history is needed for warm-up).
        symbols: which symbols to test; defaults to the first ``max_symbols``.
        tolerance: an absolute return difference larger than this is flagged.
    """
    from nifty_quant.engine.backtest import run_backtest

    if not bars:
        return pd.DataFrame()
    chosen = symbols or list(bars)[:max_symbols]
    chosen = [s for s in chosen if s in bars]
    if not chosen:
        return pd.DataFrame()

    rows = []
    for strategy_id, run in results.runs.items():
        oos_sliced = _sliced_window_returns(run, split)
        for symbol in chosen:
            try:
                restarted = run_backtest(
                    strategy_id,
                    {symbol: bars[symbol]},
                    config=config,
                    start=split.oos_start,
                )
                metrics = restarted.aggregate_metrics()
                restarted_return = float(metrics.get("total_return", np.nan))
            except Exception as exc:  # a strategy may legitimately refuse
                rows.append(
                    {
                        "strategy_id": strategy_id,
                        "symbol": symbol,
                        "sliced_return": np.nan,
                        "restarted_return": np.nan,
                        "difference": np.nan,
                        "ok": False,
                        "note": str(exc),
                    }
                )
                continue
            sliced_return = oos_sliced.get(symbol, np.nan)
            difference = (
                restarted_return - sliced_return
                if np.isfinite(restarted_return) and np.isfinite(sliced_return)
                else np.nan
            )
            if np.isfinite(difference):
                note = ""
            elif np.isfinite(sliced_return):
                note = f"sliced held-out return {sliced_return:.4f}"
            else:
                note = "no comparable held-out slice"
            rows.append(
                {
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "sliced_return": sliced_return,
                    "restarted_return": restarted_return,
                    "difference": difference,
                    "ok": bool(np.isfinite(difference) and abs(difference) <= tolerance),
                    "note": note,
                }
            )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["strategy_id", "symbol"]).reset_index(drop=True)


def _sliced_window_returns(run, split: Split) -> dict[str, float]:
    """Held-out return per sleeve, measured by slicing the continuous run."""
    out: dict[str, float] = {}
    for run_slice in run.slices:
        if run_slice.equity is None or run_slice.equity.empty:
            continue
        metrics = window_metrics(
            run_slice.equity["equity"],
            run_slice.trades,
            split.oos_start,
            split.oos_end,
        )
        if metrics:
            out[run_slice.label] = float(metrics.get("total_return", np.nan))
    return out


def _robustness(
    results: ResearchResults, consistency: pd.DataFrame, *, fdr_alpha: float
) -> pd.DataFrame:
    """Per-strategy multiplicity-aware statistics."""
    symbol_metrics = results.symbol_metrics()
    sleeve_sharpes = (
        symbol_metrics["sharpe"].astype(float).replace([np.inf, -np.inf], np.nan).dropna()
        if not symbol_metrics.empty and "sharpe" in symbol_metrics.columns
        else pd.Series(dtype=float)
    )
    n_trials = int(len(sleeve_sharpes))
    # Per-period dispersion of Sharpe across sleeves, used to calibrate the
    # "best of N trials" hurdle.
    ppy_reference = 252.0
    sr_std_per_period = (
        float(sleeve_sharpes.std(ddof=1)) / np.sqrt(ppy_reference)
        if len(sleeve_sharpes) > 2
        else float("nan")
    )

    rows = []
    for strategy_id, run in results.runs.items():
        equity = run.combined_equity()
        returns = (
            equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
            if not equity.empty
            else pd.Series(dtype=float)
        )
        ppy = periods_per_year(pd.DatetimeIndex(equity.index)) if not equity.empty else float("nan")
        row = {
            "strategy_id": strategy_id,
            "observations": int(len(returns)),
            "psr": probabilistic_sharpe_ratio(returns) if len(returns) > 2 else np.nan,
            "n_trials": n_trials,
            "sharpe_std_per_period": sr_std_per_period,
            "expected_max_sharpe": expected_max_sharpe(n_trials, sr_std_per_period),
            "deflated_sharpe": (
                deflated_sharpe_ratio(returns, n_trials=n_trials, sr_std=sr_std_per_period)
                if len(returns) > 2
                else np.nan
            ),
            "annualised_sharpe": float(returns.mean() / returns.std(ddof=1) * np.sqrt(ppy))
            if len(returns) > 2 and returns.std(ddof=1) > 0
            else np.nan,
        }
        rows.append(row)
    frame = pd.DataFrame(rows).sort_values("strategy_id").reset_index(drop=True)
    if frame.empty:
        return frame

    # Breadth: sign test on the share of profitable symbols, FDR-controlled.
    if not consistency.empty:
        merged = frame.merge(
            consistency[["strategy_id", "pct_symbols_positive", "sign_test_pvalue"]],
            on="strategy_id",
            how="left",
        )
        merged["breadth_qvalue"] = benjamini_hochberg(merged["sign_test_pvalue"].fillna(1.0))
        frame = merged
    else:
        frame["pct_symbols_positive"] = np.nan
        frame["sign_test_pvalue"] = np.nan
        frame["breadth_qvalue"] = np.nan
    frame["fdr_alpha"] = fdr_alpha
    return frame


def _checks(
    *,
    results: ResearchResults,
    dev_oos: pd.DataFrame,
    wf_summary: pd.DataFrame,
    consistency: pd.DataFrame,
    sensitivity_summary: pd.DataFrame,
    robustness: pd.DataFrame,
    regime_trades: pd.DataFrame,
    strict_split: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """One row per (strategy, check) with the observed value and pass flag."""
    rows: list[dict[str, Any]] = []

    def add(strategy_id: str, check: str, passed: bool | None, value, detail: str = "") -> None:
        rows.append(
            {
                "strategy_id": strategy_id,
                "check": check,
                "critical": check in CRITICAL_CHECKS,
                "passed": passed,
                "value": value,
                "detail": detail,
            }
        )

    for strategy_id, run in results.runs.items():
        metrics = run.aggregate_metrics()
        n_trades = float(metrics.get("n_trades", 0.0) or 0.0)
        add(strategy_id, "enough_trades", n_trades >= MIN_TRADES, n_trades, f"minimum {MIN_TRADES}")

        dev_row = dev_oos[dev_oos["strategy_id"] == strategy_id] if not dev_oos.empty else pd.DataFrame()
        if dev_row.empty:
            add(strategy_id, "oos_profitable", None, np.nan, "not enough out-of-sample data")
            add(strategy_id, "oos_retention", None, np.nan, "not enough out-of-sample data")
        else:
            oos_return = float(dev_row["oos_total_return"].iloc[0])
            retention = float(dev_row.get("oos_return_retention", pd.Series([np.nan])).iloc[0])
            add(strategy_id, "oos_profitable", oos_return > 0, oos_return, "held-out mean sleeve return")
            add(
                strategy_id,
                "oos_retention",
                retention > MIN_OOS_RETENTION if np.isfinite(retention) else None,
                retention,
                "held-out return / development return",
            )

        if wf_summary.empty:
            add(strategy_id, "walkforward_positive", None, np.nan, "walk-forward not run")
        else:
            wf = wf_summary[wf_summary["strategy_id"] == strategy_id]
            share = float(wf["oos_positive_fold_share"].iloc[0]) if not wf.empty else np.nan
            add(
                strategy_id,
                "walkforward_positive",
                share >= MIN_WF_POSITIVE_SHARE if np.isfinite(share) else None,
                share,
                f"share of folds with positive OOS return (min {MIN_WF_POSITIVE_SHARE:.0%})",
            )

        cons = consistency[consistency["strategy_id"] == strategy_id] if not consistency.empty else pd.DataFrame()
        if cons.empty:
            add(strategy_id, "breadth", None, np.nan, "no qualifying symbols")
            add(strategy_id, "concentration", None, np.nan, "no qualifying symbols")
        else:
            pct = float(cons["pct_symbols_positive"].iloc[0])
            add(strategy_id, "breadth", pct >= 0.5, pct, "share of profitable symbols (min 50%)")
            share_of_pnl = float(cons["best_symbol_pnl_share"].iloc[0])
            add(
                strategy_id,
                "concentration",
                share_of_pnl <= MAX_SYMBOL_CONCENTRATION if np.isfinite(share_of_pnl) else None,
                share_of_pnl,
                "share of total P&L from the single best symbol",
            )

        if sensitivity_summary.empty:
            add(strategy_id, "sensitivity_plateau", None, np.nan, "sensitivity not run")
        else:
            sub = sensitivity_summary[sensitivity_summary["strategy_id"] == strategy_id]
            usable = sub[sub["usable_variants"] > 0] if not sub.empty else pd.DataFrame()
            if usable.empty:
                add(strategy_id, "sensitivity_plateau", None, np.nan, "no testable perturbation")
            else:
                median_share = float(usable["positive_neighbour_share"].median())
                add(
                    strategy_id,
                    "sensitivity_plateau",
                    median_share >= MIN_PLATEAU_SHARE,
                    median_share,
                    f"median share of profitable perturbations (min {MIN_PLATEAU_SHARE:.0%})",
                )

        rob = robustness[robustness["strategy_id"] == strategy_id] if not robustness.empty else pd.DataFrame()
        if rob.empty:
            add(strategy_id, "statistical_significance", None, np.nan, "not computed")
            add(strategy_id, "fdr_significant", None, np.nan, "not computed")
        else:
            psr = float(rob["psr"].iloc[0])
            dsr = float(rob["deflated_sharpe"].iloc[0])
            add(
                strategy_id,
                "statistical_significance",
                psr >= MIN_PSR if np.isfinite(psr) else None,
                psr,
                f"probabilistic Sharpe (min {MIN_PSR})",
            )
            q = float(rob["breadth_qvalue"].iloc[0]) if "breadth_qvalue" in rob else np.nan
            alpha = float(rob["fdr_alpha"].iloc[0]) if "fdr_alpha" in rob else 0.1
            add(
                strategy_id,
                "fdr_significant",
                q < alpha if np.isfinite(q) else None,
                q,
                f"Benjamini-Hochberg q for breadth (max {alpha})",
            )

        if regime_trades.empty:
            add(strategy_id, "regime_coverage", None, np.nan, "benchmark regime labels unavailable")
        else:
            sub = regime_trades[regime_trades["strategy_id"] == strategy_id]
            profitable = int((sub["total_pnl"] > 0).sum()) if not sub.empty else 0
            add(
                strategy_id,
                "regime_coverage",
                profitable >= MIN_REGIME_COVERAGE,
                float(profitable),
                f"regimes with positive P&L (min {MIN_REGIME_COVERAGE})",
            )

        if strict_split is None or strict_split.empty:
            add(
                strategy_id,
                "oos_restart_agrees",
                None,
                np.nan,
                "restarted holdout not evaluated",
            )
        else:
            sub = strict_split[strict_split["strategy_id"] == strategy_id]
            tested = sub[sub["difference"].notna()] if not sub.empty else pd.DataFrame()
            if tested.empty:
                add(
                    strategy_id,
                    "oos_restart_agrees",
                    None,
                    np.nan,
                    "no comparable sleeve restarted",
                )
            else:
                worst = float(tested["difference"].abs().max())
                add(
                    strategy_id,
                    "oos_restart_agrees",
                    worst <= 0.05,
                    worst,
                    "max |restarted - sliced| held-out return",
                )

    return pd.DataFrame(rows)


def _verdicts(checks: pd.DataFrame) -> pd.DataFrame:
    """Roll the checks up into one verdict per strategy."""
    if checks.empty:
        return pd.DataFrame()
    rows = []
    for strategy_id, group in checks.groupby("strategy_id", sort=True):
        tested = group[group["passed"].notna()]
        passed = tested[tested["passed"].astype(bool)]
        failed = tested[~tested["passed"].astype(bool)]
        critical_failed = failed[failed["critical"]]
        untested = group[group["passed"].isna()]

        if len(tested) == 0:
            verdict = "untested"
        elif critical_failed.empty and len(failed) <= 1:
            verdict = "supported"
        elif len(critical_failed) <= 1 and len(failed) <= 3:
            verdict = "weak"
        else:
            verdict = "unsupported"

        rows.append(
            {
                "strategy_id": strategy_id,
                "verdict": verdict,
                "checks_passed": int(len(passed)),
                "checks_failed": int(len(failed)),
                "checks_untested": int(len(untested)),
                "critical_failures": int(len(critical_failed)),
                "failed_checks": ", ".join(failed["check"]) if len(failed) else "",
                "untested_checks": ", ".join(untested["check"]) if len(untested) else "",
            }
        )
    order = {"supported": 0, "weak": 1, "unsupported": 2, "untested": 3}
    frame = pd.DataFrame(rows)
    frame["_order"] = frame["verdict"].map(order).fillna(9)
    return frame.sort_values(["_order", "strategy_id"]).drop(columns="_order").reset_index(drop=True)


def save_validation(report: ValidationReport, directory: Path | str) -> Path:
    """Write every validation frame as CSV plus the threshold metadata."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    for name, frame in report.to_frames().items():
        if frame is None or frame.empty:
            continue
        frame.to_csv(target / f"{name}.csv", index=False)
    (target / "validation_metadata.json").write_text(
        json.dumps(report.metadata, indent=2, default=str), encoding="utf-8"
    )
    return target


# Frames written by ``save_validation`` and restored by ``load_validation``. The
# split is stored separately because it is a dataclass, not a table.
_RELOADABLE_FRAMES = (
    "dev_oos",
    "strict_split",
    "walkforward",
    "walkforward_summary",
    "sensitivity",
    "sensitivity_detail",
    "regime_equity",
    "regime_trades",
    "consistency",
    "robustness",
    "checks",
    "verdicts",
)


def load_validation(directory: Path | str) -> ValidationReport:
    """Reload a directory produced by :func:`save_validation`.

    Rebuilding a report must not require re-running the tests: the numbers were
    computed once, and re-deriving them would risk showing a different answer
    than the one that was reviewed. Frames that are absent come back empty, so a
    partially-run validation reloads as partially-run.
    """
    root = Path(directory)
    split_path = root / "split.csv"
    if not split_path.exists():
        raise FileNotFoundError(f"{root} does not look like a validation directory (no split.csv)")

    split_frame = pd.read_csv(split_path)
    if split_frame.empty:
        raise ValueError(f"{split_path} is empty")
    split = _split_from_frame(split_frame)

    frames: dict[str, pd.DataFrame] = {}
    for name in _RELOADABLE_FRAMES:
        path = root / f"{name}.csv"
        frames[name] = _read_optional(path)

    metadata: dict[str, Any] = {}
    meta_path = root / "validation_metadata.json"
    if meta_path.exists():
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata.setdefault("reloaded_from", str(root))

    return ValidationReport(split=split, metadata=metadata, **frames)


def _read_optional(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _split_from_frame(frame: pd.DataFrame) -> Split:
    row = frame.iloc[0]

    def stamp(name: str) -> pd.Timestamp | None:
        value = row.get(name)
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return None
        if pd.isna(value):
            return None
        return pd.Timestamp(value)

    def bars(name: str) -> int:
        value = row.get(name)
        return int(value) if value is not None and pd.notna(value) else 0

    return Split(
        dev_start=stamp("dev_start"),
        dev_end=stamp("dev_end"),
        purge_start=stamp("purge_start"),
        purge_end=stamp("purge_end"),
        oos_start=stamp("oos_start"),
        oos_end=stamp("oos_end"),
        dev_bars=bars("dev_bars"),
        purge_bars=bars("purge_bars"),
        oos_bars=bars("oos_bars"),
    )
