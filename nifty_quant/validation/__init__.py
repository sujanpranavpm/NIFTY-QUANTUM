"""Validation: holdout, walk-forward, sensitivity, regimes, statistics.

The rule this package enforces is that no strategy is described as robust because
of a backtest number. Every claim has to come from a named test with a threshold
declared in ``report.py``, and every test is reproducible from the recorded runs.
"""

from nifty_quant.validation.consistency import dev_oos_table, symbol_consistency
from nifty_quant.validation.regime import align_regimes, classify, regime_equity_stats, regime_trade_stats
from nifty_quant.validation.report import (
    MIN_TRADES,
    ValidationReport,
    load_validation,
    run_validation,
    save_validation,
)
from nifty_quant.validation.sensitivity import plateau_summary, perturb_grid, run_sensitivity
from nifty_quant.validation.splits import Split, dev_oos_split
from nifty_quant.validation.stats import (
    benjamini_hochberg,
    binomial_sign_test,
    bootstrap_mean_ci,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
)
from nifty_quant.validation.walkforward import (
    walkforward_summary,
    walkforward_table,
    window_metrics,
)

__all__ = [
    "MIN_TRADES",
    "Split",
    "ValidationReport",
    "align_regimes",
    "benjamini_hochberg",
    "binomial_sign_test",
    "bootstrap_mean_ci",
    "classify",
    "dev_oos_table",
    "deflated_sharpe_ratio",
    "dev_oos_split",
    "expected_max_sharpe",
    "load_validation",
    "plateau_summary",
    "perturb_grid",
    "probabilistic_sharpe_ratio",
    "regime_equity_stats",
    "regime_trade_stats",
    "run_sensitivity",
    "run_validation",
    "save_validation",
    "symbol_consistency",
    "walkforward_summary",
    "walkforward_table",
    "window_metrics",
]
