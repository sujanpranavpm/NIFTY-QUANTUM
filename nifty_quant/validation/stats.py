"""Statistical robustness helpers.

These functions exist because a backtest grid of 18 strategies x 50 stocks is
roughly 900 candidate tests. Some will look excellent by chance alone. The tools
here quantify that instead of ignoring it:

* bootstrap confidence intervals for a mean trade P&L and for a Sharpe ratio;
* the probabilistic Sharpe ratio (PSR), which converts a Sharpe estimate and its
  sampling distribution into a probability;
* the expected maximum Sharpe of N independent trials, i.e. the hurdle that a
  strategy must clear before it can be called more than the best of a lottery;
* Benjamini-Hochberg false-discovery control across the whole grid.

All randomness is seeded, so a validation report reproduces exactly.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import stats as scipy_stats

EULER_MASCHERONI = 0.5772156649015329


def bootstrap_mean_ci(
    values,
    *,
    n_boot: int = 5000,
    alpha: float = 0.05,
    seed: int = 20260101,
) -> dict[str, float]:
    """Percentile bootstrap CI for the mean of ``values`` (trade P&L, returns...)."""
    data = np.asarray(list(values), dtype=float)
    data = data[np.isfinite(data)]
    if data.size < 2:
        return {"n": float(data.size), "mean": float("nan"), "ci_low": float("nan"), "ci_high": float("nan")}
    rng = np.random.default_rng(seed)
    draws = rng.choice(data, size=(n_boot, data.size), replace=True)
    means = draws.mean(axis=1)
    lo, hi = np.quantile(means, [alpha / 2.0, 1.0 - alpha / 2.0])
    return {
        "n": float(data.size),
        "mean": float(data.mean()),
        "ci_low": float(lo),
        "ci_high": float(hi),
    }


def _moments(returns: np.ndarray) -> tuple[int, float, float, float]:
    n = returns.size
    sr = float(returns.mean() / returns.std(ddof=1)) if n > 2 and returns.std(ddof=1) > 0 else float("nan")
    skew = float(scipy_stats.skew(returns, bias=False)) if n > 2 else float("nan")
    kurt = float(scipy_stats.kurtosis(returns, fisher=False, bias=False)) if n > 3 else float("nan")
    return n, sr, skew, kurt


def probabilistic_sharpe_ratio(
    returns, benchmark_sr: float = 0.0
) -> float:
    """P(true Sharpe > ``benchmark_sr``) given the observed sample.

    ``returns`` are per-period (not annualised) and ``benchmark_sr`` must use the
    same frequency. Formula from Bailey & Lopez de Prado (2012); it accounts for
    skewness and kurtosis, which is why fat-tailed trade returns get a lower
    probability than the same Sharpe under a normal assumption.
    """
    data = np.asarray(list(returns), dtype=float)
    data = data[np.isfinite(data)]
    n, sr, skew, kurt = _moments(data)
    if not math.isfinite(sr) or n < 3:
        return float("nan")
    denominator = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr**2
    if not math.isfinite(denominator) or denominator <= 0:
        return float("nan")
    z = (sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(denominator)
    return float(scipy_stats.norm.cdf(z))


def expected_max_sharpe(n_trials: int, sr_std: float) -> float:
    """Expected best per-period Sharpe among ``n_trials`` independent tries.

    ``sr_std`` is the cross-sectional standard deviation of the per-period Sharpe
    ratios actually observed in the grid, so the hurdle is calibrated to this
    study rather than to a textbook assumption.
    """
    if n_trials < 2 or not math.isfinite(sr_std) or sr_std <= 0:
        return float("nan")
    first = scipy_stats.norm.ppf(1.0 - 1.0 / n_trials)
    second = scipy_stats.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(sr_std * ((1.0 - EULER_MASCHERONI) * first + EULER_MASCHERONI * second))


def deflated_sharpe_ratio(
    returns,
    *,
    n_trials: int,
    sr_std: float,
) -> float:
    """PSR evaluated against the expected best-of-``n_trials`` hurdle.

    A value below ~0.95 means the strategy's Sharpe is not distinguishable from
    the best result a search over ``n_trials`` would produce from noise.
    """
    data = np.asarray(list(returns), dtype=float)
    data = data[np.isfinite(data)]
    hurdle = expected_max_sharpe(n_trials, sr_std)
    if not math.isfinite(hurdle):
        return float("nan")
    return probabilistic_sharpe_ratio(data, benchmark_sr=hurdle)


def binomial_sign_test(positive: int, negative: int) -> float:
    """Two-sided p-value for "wins and losses are equally likely"."""
    positive, negative = int(positive), int(negative)
    total = positive + negative
    if total == 0:
        return float("nan")
    result = scipy_stats.binomtest(positive, total, 0.5, alternative="two-sided")
    return float(result.pvalue)


def benjamini_hochberg(pvalues) -> np.ndarray:
    """Benjamini-Hochberg adjusted q-values (same order as the input)."""
    p = np.asarray(list(pvalues), dtype=float)
    out = np.full(p.shape, np.nan)
    finite = np.isfinite(p)
    if not finite.any():
        return out
    pv = p[finite]
    n = pv.size
    order = np.argsort(pv)
    ranked = pv[order]
    scaled = ranked * n / (np.arange(1, n + 1))
    # Enforce monotonicity: q_i = min over j >= i.
    adjusted = np.minimum.accumulate(scaled[::-1])[::-1]
    adjusted = np.clip(adjusted, 0.0, 1.0)
    result = np.empty(n)
    result[order] = adjusted
    out[finite] = result
    return out


def bootstrap_sharpe_ci(
    returns,
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 20260101,
) -> dict[str, float]:
    """Bootstrap CI for the per-period Sharpe ratio."""
    data = np.asarray(list(returns), dtype=float)
    data = data[np.isfinite(data)]
    if data.size < 10:
        return {"n": float(data.size), "sharpe": float("nan"), "ci_low": float("nan"), "ci_high": float("nan")}
    rng = np.random.default_rng(seed)
    draws = rng.choice(data, size=(n_boot, data.size), replace=True)
    means = draws.mean(axis=1)
    stds = draws.std(axis=1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sharpes = np.where(stds > 0, means / stds, np.nan)
    sharpes = sharpes[np.isfinite(sharpes)]
    if sharpes.size == 0:
        return {"n": float(data.size), "sharpe": float("nan"), "ci_low": float("nan"), "ci_high": float("nan")}
    lo, hi = np.quantile(sharpes, [alpha / 2.0, 1.0 - alpha / 2.0])
    return {
        "n": float(data.size),
        "sharpe": float(data.mean() / data.std(ddof=1)),
        "ci_low": float(lo),
        "ci_high": float(hi),
    }
