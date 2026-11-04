"""Parameter sensitivity.

A published parameter set is a single point in a space nobody explored. If a
strategy only works at ``fast=20, slow=50`` and collapses at 18/50 or 22/50, that
is evidence of a knife-edge fit rather than an edge, even when the headline number
is attractive.

Each numeric parameter is perturbed one at a time by a relative step, the whole
backtest is re-run, and the resulting performance surface is summarised. Sizing
parameters are excluded on purpose: changing ``weight`` scales every P&L, so it
measures position size, not signal quality.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from nifty_quant.config import ResearchConfig
from nifty_quant.engine.backtest import run_backtest
from nifty_quant.strategies.registry import default_params

# Parameters that only scale exposure or select instruments; perturbing them does
# not test the signal, so they are held fixed.
_EXCLUDED = {
    "weight",
    "top_n",
    "contract_specs",
    "risk_fraction",
    "notional_cap",
    "margin_cap",
}

METRIC_KEYS = ("total_return", "cagr", "sharpe", "max_drawdown", "n_trades", "win_rate", "profit_factor", "expectancy", "net_profit")


def perturbable_params(strategy_id: str) -> dict[str, float]:
    """Numeric parameters of one strategy that the sensitivity test may move."""
    params = default_params(strategy_id)
    out: dict[str, float] = {}
    for name, value in params.items():
        if name in _EXCLUDED:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not np.isfinite(value):
            continue
        out[name] = value
    return out


def perturb_grid(
    strategy_id: str,
    steps: tuple[float, ...] = (-0.2, -0.1, 0.1, 0.2),
    *,
    min_ratio: float = 0.5,
    max_ratio: float = 2.0,
) -> list[dict]:
    """One perturbed parameter set per (parameter, step), plus the base case.

    Integer parameters stay integral and are floored at 2; fractional parameters
    move proportionally. Values outside ``[min_ratio, max_ratio]`` of the base are
    dropped so a 20% step cannot turn a 3-bar expiry into 4.5 bars.
    """
    base = default_params(strategy_id)
    numeric = perturbable_params(strategy_id)
    rows: list[dict] = [
        {"param": "(base)", "factor": 0.0, "value": np.nan, "params": dict(base), "valid": True, "note": "published setting"}
    ]
    for name, value in numeric.items():
        for step in steps:
            factor = 1.0 + float(step)
            if factor <= 0:
                continue
            new_value = _apply(value, factor)
            note = ""
            valid = True
            if new_value is None:
                valid = False
                note = "value would fall below the minimum"
            else:
                ratio = new_value / value if value else np.nan
                if not (min_ratio <= ratio <= max_ratio):
                    valid = False
                    note = "outside the allowed ratio band"
                elif _breaks_constraints(name, new_value, base):
                    valid = False
                    note = "violates a structural constraint (e.g. fast >= slow)"
            params = dict(base)
            if valid:
                params[name] = new_value
            rows.append(
                {
                    "param": name,
                    "factor": factor,
                    "value": new_value if new_value is not None else np.nan,
                    "params": params,
                    "valid": valid,
                    "note": note,
                }
            )
    return rows


def _apply(value: float, factor: float):
    if isinstance(value, int):
        new_value = int(round(value * factor))
        return new_value if new_value >= 2 else None
    new_value = value * factor
    return new_value if new_value > 0 else None


def _breaks_constraints(name: str, value: float, base: dict) -> bool:
    if name == "fast" and "slow" in base and value >= base["slow"]:
        return True
    if name == "slow" and "fast" in base and value <= base["fast"]:
        return True
    return False


@dataclass
class SensitivityResult:
    table: pd.DataFrame
    summary: pd.DataFrame


def run_sensitivity(
    strategy_id: str,
    bars: dict,
    *,
    steps: tuple[float, ...] = (-0.2, -0.1, 0.1, 0.2),
    config: ResearchConfig | None = None,
    interval: str = "1d",
    rsi_method: str = "wilder",
) -> SensitivityResult:
    """Re-run one strategy across the perturbation grid.

    ``bars`` should be a small, representative symbol set: every row is a full
    backtest, so cost scales linearly with the number of symbols.
    """
    config = config or ResearchConfig()
    rows = []
    for entry in perturb_grid(strategy_id, steps):
        row = {
            "strategy_id": strategy_id,
            "param": entry["param"],
            "factor": entry["factor"],
            "value": entry["value"],
            "valid": entry["valid"],
            "note": entry["note"],
        }
        if entry["valid"]:
            try:
                run = run_backtest(
                    strategy_id,
                    bars,
                    config=config,
                    params=entry["params"],
                    interval=interval,
                    rsi_method=rsi_method,
                )
                metrics = run.aggregate_metrics()
                row.update({key: metrics.get(key) for key in METRIC_KEYS})
                row["ok"] = True
            except Exception as exc:  # a perturbation may be structurally invalid
                row["ok"] = False
                row["note"] = f"run failed: {exc}"
        else:
            row["ok"] = False
        rows.append(row)
    table = pd.DataFrame(rows)
    return SensitivityResult(table=table, summary=plateau_summary(table))


def plateau_summary(table: pd.DataFrame, *, min_trades: int = 10) -> pd.DataFrame:
    """Per-parameter robustness summary.

    ``positive_neighbour_share`` is the share of *valid, traded* perturbations that
    still made money. ``spread`` is the range of total return across the grid: a
    strategy whose return swings from +80% to -20% when a window moves by 10% is
    not describable as robust, whatever the base case shows.
    """
    if table.empty:
        return pd.DataFrame()
    rows = []
    for strategy_id, group in table.groupby("strategy_id", sort=True):
        base = group[group["param"] == "(base)"]
        base_return = float(base["total_return"].iloc[0]) if len(base) and pd.notna(base["total_return"].iloc[0]) else np.nan
        for param, sub in group[group["param"] != "(base)"].groupby("param", sort=True):
            usable = sub[(sub["valid"]) & (sub["ok"]) & (sub["n_trades"].fillna(0) >= min_trades)]
            if usable.empty:
                rows.append(
                    {
                        "strategy_id": strategy_id,
                        "param": param,
                        "variants": int(len(sub)),
                        "usable_variants": 0,
                        "base_total_return": base_return,
                        "positive_neighbour_share": np.nan,
                        "spread": np.nan,
                        "worst_total_return": np.nan,
                        "verdict": "untestable",
                    }
                )
                continue
            returns = usable["total_return"].astype(float)
            share = float((returns > 0).mean())
            spread = float(returns.max() - returns.min())
            verdict = "robust" if share >= 0.75 else ("mixed" if share >= 0.4 else "fragile")
            rows.append(
                {
                    "strategy_id": strategy_id,
                    "param": param,
                    "variants": int(len(sub)),
                    "usable_variants": int(len(usable)),
                    "base_total_return": base_return,
                    "positive_neighbour_share": share,
                    "spread": spread,
                    "worst_total_return": float(returns.min()),
                    "verdict": verdict,
                }
            )
    return pd.DataFrame(rows).sort_values(["strategy_id", "param"]).reset_index(drop=True)
