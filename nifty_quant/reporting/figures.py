"""Plotly figure builders for the research dashboard and the HTML report.

Two rules hold for every builder:

1. **No fallback numbers.** If the input is missing or empty the builder returns
   ``None`` rather than an empty or illustrative chart, so a gap in the evidence
   stays visible instead of being papered over with a blank axis.
2. **No recomputation of performance.** Figures read the frames produced by
   ``ResearchResults`` and ``ValidationReport``; nothing here derives a return.

Every figure is a plain ``go.Figure`` so Streamlit, the HTML report and a
notebook all render the same object.
"""

from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from nifty_quant.reporting.formatting import format_value, is_percent

FONT = dict(family="Inter, Segoe UI, Helvetica, sans-serif", size=12, color="#1f2933")
PALETTE = (
    "#1f4e79",
    "#c0392b",
    "#1e8449",
    "#b7791f",
    "#6c3483",
    "#117a65",
    "#a04000",
    "#566573",
    "#2874a6",
    "#922b21",
)
POSITIVE = "#1e8449"
NEGATIVE = "#c0392b"
NEUTRAL = "#566573"

DIVERGING = [
    [0.0, "#a93226"],
    [0.25, "#e6b0aa"],
    [0.5, "#f7f7f7"],
    [0.75, "#a9dfbf"],
    [1.0, "#1e8449"],
]

VERDICT_COLOURS = {
    "supported": "#1e8449",
    "weak": "#b7791f",
    "unsupported": "#c0392b",
    "untested": "#95a5a6",
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _finish(
    fig: go.Figure,
    title: str,
    *,
    height: int = 420,
    hovermode: str | None = "x unified",
    showlegend: bool = True,
) -> go.Figure:
    fig.update_layout(
        title=dict(text=title, x=0.01, xanchor="left", font=dict(size=14)),
        height=height,
        margin=dict(l=64, r=28, t=56, b=48),
        template="plotly_white",
        font=FONT,
        hovermode=hovermode,
        showlegend=showlegend,
        legend=dict(orientation="h", y=-0.18, x=0, font=dict(size=11)),
        colorway=PALETTE,
    )
    fig.update_xaxes(showgrid=False, linecolor="#d5d8dc")
    fig.update_yaxes(gridcolor="#eaecee", zerolinecolor="#bdc3c7")
    return fig


def _select(frame: pd.DataFrame, strategies: Sequence[str] | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    if strategies is None:
        return frame
    keep = [s for s in strategies if s in frame.columns]
    return frame.loc[:, keep]


def _numeric(values) -> np.ndarray:
    return pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(dtype=float)


def _label(value, metric: str) -> str:
    return format_value(value, metric)


# --------------------------------------------------------------------------
# Performance curves
# --------------------------------------------------------------------------
def equity_figure(
    results,
    strategies: Sequence[str] | None = None,
    *,
    indexed: bool = True,
    log_scale: bool = False,
    height: int = 460,
) -> go.Figure | None:
    """One line per strategy. ``indexed`` rebases each curve to 100."""
    curves = _select(results.equity_curves(), strategies)
    if curves.empty:
        return None
    fig = go.Figure()
    for column in curves.columns:
        series = curves[column].astype(float).dropna()
        if series.empty:
            continue
        if indexed and series.iloc[0] != 0:
            series = series / series.iloc[0] * 100.0
        fig.add_trace(
            go.Scatter(
                x=series.index,
                y=series.to_numpy(),
                mode="lines",
                name=str(column),
                line=dict(width=1.5),
            )
        )
    fig.update_yaxes(title_text="indexed equity (start = 100)" if indexed else "equity (₹)")
    if log_scale:
        fig.update_yaxes(type="log")
    return _finish(fig, "Equity curves", height=height)


def drawdown_figure(
    results,
    strategies: Sequence[str] | None = None,
    *,
    height: int = 380,
) -> go.Figure | None:
    """Underwater curve per strategy."""
    curves = _select(results.drawdown_curves(), strategies)
    if curves.empty:
        return None
    fig = go.Figure()
    for column in curves.columns:
        series = curves[column].astype(float).dropna()
        if series.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=series.index,
                y=series.to_numpy(),
                mode="lines",
                name=str(column),
                line=dict(width=1.2),
                fill="tozeroy",
            )
        )
    fig.update_yaxes(title_text="drawdown", tickformat=".0%")
    return _finish(fig, "Drawdown from running peak", height=height)


def monthly_heatmap(results, strategy_id: str, *, height: int | None = None) -> go.Figure | None:
    """Year x month return table for one strategy."""
    if strategy_id not in getattr(results, "runs", {}):
        return None
    table = results.monthly_returns(strategy_id)
    if table is None or table.empty:
        return None
    z = table.to_numpy(dtype=float)
    text = np.vectorize(lambda v: format_value(v, "return"))(z)
    rows = max(2, len(table))
    fig = go.Figure(
        go.Heatmap(
            z=z,
            x=[str(c) for c in table.columns],
            y=[str(i) for i in table.index],
            text=text,
            texttemplate="%{text}",
            textfont=dict(size=10),
            colorscale=DIVERGING,
            zmid=0.0,
            colorbar=dict(tickformat=".0%", title="return"),
            hovertemplate="%{y} %{x}: %{z:.2%}<extra></extra>",
        )
    )
    fig.update_yaxes(type="category", autorange="reversed")
    fig.update_layout(yaxis_title="year", xaxis_title="month")
    return _finish(
        fig,
        f"Monthly returns — {strategy_id}",
        height=height or max(300, 22 * rows + 120),
        hovermode="closest",
        showlegend=False,
    )


def yearly_figure(results, strategy_id: str, *, height: int = 320) -> go.Figure | None:
    """Calendar-year returns for one strategy."""
    if strategy_id not in getattr(results, "runs", {}):
        return None
    series = results.yearly_returns(strategy_id)
    if series is None or len(series) == 0:
        return None
    values = np.asarray(series.to_numpy(), dtype=float)
    colours = [POSITIVE if v >= 0 else NEGATIVE for v in values]
    fig = go.Figure(
        go.Bar(
            x=[str(i) for i in series.index],
            y=values,
            marker_color=colours,
            text=[format_value(v, "return") for v in values],
            textposition="outside",
            hovertemplate="%{x}: %{y:.2%}<extra></extra>",
        )
    )
    fig.update_yaxes(title_text="return", tickformat=".0%")
    return _finish(fig, f"Yearly returns — {strategy_id}", height=height, showlegend=False)


def signal_history_figure(
    trades: pd.DataFrame,
    *,
    strategy_id: str = "",
    symbol: str | None = None,
    height: int = 420,
) -> go.Figure | None:
    """Entry and exit markers with one segment per round trip.

    This is the "entry and exit signal history" view: it shows *when* the engine
    traded and at what price, which is the only way to audit a reported win rate.
    """
    if trades is None or trades.empty:
        return None
    frame = trades
    if symbol is not None and "symbol" in frame.columns:
        frame = frame[frame["symbol"] == symbol]
    if frame.empty:
        return None
    frame = frame.dropna(subset=["entry_time", "exit_time", "entry_price", "exit_price"])
    if frame.empty:
        return None

    xs, ys = [], []
    for _, row in frame.iterrows():
        xs.extend([row["entry_time"], row["exit_time"], None])
        ys.extend([float(row["entry_price"]), float(row["exit_price"]), None])

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=xs,
            y=ys,
            mode="lines",
            line=dict(width=0.6, color="#bdc3c7"),
            name="holding period",
            hoverinfo="skip",
        )
    )
    pnl = _numeric(frame.get("net_pnl", pd.Series(np.zeros(len(frame)))))
    colours = [POSITIVE if v >= 0 else NEGATIVE for v in pnl]
    fig.add_trace(
        go.Scatter(
            x=frame["entry_time"],
            y=_numeric(frame["entry_price"]),
            mode="markers",
            marker=dict(symbol="triangle-up", size=7, color=colours, line=dict(width=0.5, color="#ffffff")),
            name="entry",
            text=[_label(v, "net_profit") for v in pnl],
            hovertemplate="entry %{x|%Y-%m-%d} @ %{y:,.2f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=frame["exit_time"],
            y=_numeric(frame["exit_price"]),
            mode="markers",
            marker=dict(symbol="triangle-down", size=7, color=colours, line=dict(width=0.5, color="#ffffff")),
            name="exit",
            hovertemplate="exit %{x|%Y-%m-%d} @ %{y:,.2f}<extra></extra>",
        )
    )
    fig.update_yaxes(title_text="price (₹)")
    suffix = f" — {symbol}" if symbol else ""
    return _finish(fig, f"Entry / exit history — {strategy_id}{suffix}", height=height)


# --------------------------------------------------------------------------
# Strategy comparison
# --------------------------------------------------------------------------
def metric_bar(
    metrics: pd.DataFrame,
    metric: str,
    *,
    title: str | None = None,
    label_column: str = "strategy_id",
    top_n: int | None = None,
    height: int | None = None,
) -> go.Figure | None:
    """Horizontal ranking of one metric across strategies."""
    if metrics is None or metrics.empty or metric not in metrics.columns:
        return None
    frame = metrics[[label_column, metric]].copy()
    frame[metric] = pd.to_numeric(frame[metric], errors="coerce")
    frame = frame.dropna(subset=[metric])
    if frame.empty:
        return None
    frame = frame.sort_values(metric, ascending=False)
    if top_n is not None:
        frame = frame.head(top_n)
    frame = frame.iloc[::-1]
    values = frame[metric].to_numpy(dtype=float)
    colours = [POSITIVE if v >= 0 else NEGATIVE for v in values]
    fig = go.Figure(
        go.Bar(
            x=values,
            y=[str(v) for v in frame[label_column]],
            orientation="h",
            marker_color=colours,
            text=[format_value(v, metric) for v in values],
            textposition="outside",
            hovertemplate="%{y}: %{x}<extra></extra>",
        )
    )
    if is_percent(metric):
        fig.update_xaxes(tickformat=".0%")
    fig.update_xaxes(title_text=str(metric).replace("_", " "))
    return _finish(
        fig,
        title or str(metric).replace("_", " ").title(),
        height=height or max(280, 24 * len(frame) + 120),
        hovermode="closest",
        showlegend=False,
    )


def risk_return_figure(metrics: pd.DataFrame, *, height: int = 440) -> go.Figure | None:
    """CAGR against maximum drawdown, sized by trade count and coloured by Sharpe."""
    if metrics is None or metrics.empty:
        return None
    needed = {"cagr", "max_drawdown", "sharpe"}
    if not needed.issubset(metrics.columns):
        return None
    frame = metrics.dropna(subset=["cagr", "max_drawdown"]).copy()
    if frame.empty:
        return None
    trades = pd.to_numeric(frame.get("n_trades", pd.Series(0, index=frame.index)), errors="coerce").fillna(0.0)
    sizes = 10 + 26 * np.sqrt(trades / max(float(trades.max()), 1.0))
    fig = go.Figure(
        go.Scatter(
            x=pd.to_numeric(frame["max_drawdown"], errors="coerce"),
            y=pd.to_numeric(frame["cagr"], errors="coerce"),
            mode="markers+text",
            text=[str(s) for s in frame["strategy_id"]],
            textposition="top center",
            textfont=dict(size=9, color="#566573"),
            marker=dict(
                size=sizes,
                color=pd.to_numeric(frame["sharpe"], errors="coerce"),
                colorscale="Viridis",
                showscale=True,
                colorbar=dict(title="Sharpe"),
                line=dict(width=0.6, color="#ffffff"),
            ),
            customdata=np.column_stack([trades.to_numpy(), frame.get("n_symbols_traded", pd.Series(np.nan, index=frame.index)).to_numpy()]),
            hovertemplate=(
                "<b>%{text}</b><br>max drawdown %{x:.1%}<br>CAGR %{y:.1%}"
                "<br>trades %{customdata[0]:,.0f}<br>symbols traded %{customdata[1]:,.0f}<extra></extra>"
            ),
            name="strategies",
        )
    )
    fig.update_xaxes(title_text="maximum drawdown", tickformat=".0%", autorange="reversed")
    fig.update_yaxes(title_text="CAGR", tickformat=".0%")
    return _finish(fig, "Risk vs return (bubble = trade count)", height=height, hovermode="closest", showlegend=False)


def symbol_heatmap(
    symbol_metrics: pd.DataFrame,
    metric: str = "total_return",
    *,
    strategies: Iterable[str] | None = None,
    height: int | None = None,
) -> go.Figure | None:
    """Strategy x symbol matrix of one metric (the stock-wise view)."""
    if symbol_metrics is None or symbol_metrics.empty or metric not in symbol_metrics.columns:
        return None
    frame = symbol_metrics.copy()
    if strategies is not None:
        wanted = set(strategies)
        frame = frame[frame["strategy_id"].isin(wanted)]
    if frame.empty:
        return None
    pivot = frame.pivot_table(index="strategy_id", columns="symbol", values=metric, aggfunc="mean")
    if pivot.empty:
        return None
    z = pivot.to_numpy(dtype=float)
    text = np.vectorize(lambda v: format_value(v, metric))(z)
    colours = DIVERGING if is_percent(metric) else "Blues"
    fig = go.Figure(
        go.Heatmap(
            z=z,
            x=[str(c) for c in pivot.columns],
            y=[str(i) for i in pivot.index],
            text=text,
            texttemplate="%{text}",
            textfont=dict(size=9),
            colorscale=colours,
            zmid=0.0 if is_percent(metric) else None,
            colorbar=dict(title=str(metric).replace("_", " ")),
            hovertemplate="%{y} / %{x}: %{z}<extra></extra>",
        )
    )
    fig.update_yaxes(type="category", autorange="reversed")
    fig.update_layout(xaxis_title="symbol", yaxis_title="strategy")
    return _finish(
        fig,
        f"{str(metric).replace('_', ' ').title()} by strategy and stock",
        height=height or max(320, 22 * len(pivot.index) + 140),
        hovermode="closest",
        showlegend=False,
    )


def ranking_figure(ranking: pd.DataFrame, verdicts: pd.DataFrame | None = None, *, height: int | None = None) -> go.Figure | None:
    """Ranking bars, coloured by validation verdict when one is available."""
    if ranking is None or ranking.empty:
        return None
    score = "rank_score" if "rank_score" in ranking.columns else "median_symbol_sharpe"
    if score not in ranking.columns:
        return None
    frame = ranking.copy()
    frame[score] = pd.to_numeric(frame[score], errors="coerce")
    finite = frame[np.isfinite(frame[score])]
    frame = finite if not finite.empty else frame
    if frame.empty:
        return None
    # -inf marks "insufficient sample"; keep such rows visible but at the bottom.
    frame["_plot"] = frame[score].replace([-np.inf], -1.0)
    frame = frame.sort_values("_plot", ascending=True)

    lookup: dict[str, str] = {}
    if verdicts is not None and not verdicts.empty and "verdict" in verdicts.columns:
        lookup = dict(zip(verdicts["strategy_id"], verdicts["verdict"]))
    colours = [VERDICT_COLOURS.get(lookup.get(sid, ""), PALETTE[0]) for sid in frame["strategy_id"]]

    fig = go.Figure(
        go.Bar(
            x=frame["_plot"],
            y=[str(v) for v in frame["strategy_id"]],
            orientation="h",
            marker_color=colours,
            text=[format_value(v, score) for v in frame["_plot"]],
            textposition="outside",
            customdata=[lookup.get(sid, "not validated") for sid in frame["strategy_id"]],
            hovertemplate="%{y}: %{x:.2f}<br>verdict: %{customdata}<extra></extra>",
        )
    )
    fig.update_xaxes(title_text="median per-symbol Sharpe (no positive sample = −1)")
    title = "Strategy ranking — descriptive, not a profitability claim"
    if lookup:
        title += " (colour = validation verdict)"
    return _finish(fig, title, height=height or max(300, 24 * len(frame) + 130), hovermode="closest", showlegend=False)


# --------------------------------------------------------------------------
# Validation views
# --------------------------------------------------------------------------
def regime_figure(regime_equity: pd.DataFrame, *, metric: str = "compounded_return", height: int = 420) -> go.Figure | None:
    """Per-regime return attribution, grouped by strategy."""
    if regime_equity is None or regime_equity.empty or metric not in regime_equity.columns:
        return None
    if "regime" not in regime_equity.columns:
        return None
    fig = go.Figure()
    for strategy_id, group in regime_equity.groupby("strategy_id", sort=True):
        group = group.sort_values("regime")
        fig.add_trace(
            go.Bar(
                x=[str(r) for r in group["regime"]],
                y=pd.to_numeric(group[metric], errors="coerce"),
                name=str(strategy_id),
                hovertemplate="%{x}: %{y:.2%}<extra></extra>" if is_percent(metric) else "%{x}: %{y}<extra></extra>",
            )
        )
    fig.update_layout(barmode="group", xaxis_title="market regime (benchmark trend + volatility)")
    if is_percent(metric):
        fig.update_yaxes(tickformat=".0%")
    return _finish(fig, "Return by market regime", height=height)


def walkforward_figure(walkforward_summary: pd.DataFrame, *, height: int = 400) -> go.Figure | None:
    """In-sample vs out-of-sample median Sharpe per strategy."""
    if walkforward_summary is None or walkforward_summary.empty:
        return None
    frame = walkforward_summary.sort_values("strategy_id")
    fig = go.Figure()
    for column, colour in (("is_median_sharpe", "#95a5a6"), ("oos_median_sharpe", PALETTE[0])):
        if column in frame.columns:
            fig.add_trace(
                go.Bar(
                    x=[str(s) for s in frame["strategy_id"]],
                    y=pd.to_numeric(frame[column], errors="coerce"),
                    name=column.replace("_", " "),
                    marker_color=colour,
                    hovertemplate="%{x}: %{y:.2f}<extra></extra>",
                )
            )
    fig.update_layout(barmode="group", xaxis_title="strategy", yaxis_title="median Sharpe across folds")
    return _finish(fig, "Walk-forward: Sharpe in sample vs out of sample", height=height)


def walkforward_fold_figure(walkforward: pd.DataFrame, strategy_id: str, *, height: int = 360) -> go.Figure | None:
    """Fold-by-fold out-of-sample return for one strategy (sleeves averaged)."""
    if walkforward is None or walkforward.empty:
        return None
    from nifty_quant.validation.walkforward import fold_aggregate

    table = walkforward[walkforward["strategy_id"] == strategy_id]
    if table.empty:
        return None
    aggregated = fold_aggregate(table)
    if aggregated.empty:
        return None
    oos = _numeric(aggregated["oos_total_return"])
    colours = [POSITIVE if v >= 0 else NEGATIVE for v in oos]
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=[f"F{int(f)}" for f in aggregated["fold"]],
            y=oos,
            name="out of sample",
            marker_color=colours,
            customdata=np.column_stack(
                [
                    aggregated["test_start"].astype(str).to_numpy(),
                    aggregated["test_end"].astype(str).to_numpy(),
                    aggregated["oos_n_trades"].to_numpy(),
                ]
            ),
            hovertemplate=(
                "%{customdata[0]} → %{customdata[1]}<br>return %{y:.2%}"
                "<br>trades %{customdata[2]:,.0f}<extra></extra>"
            ),
        )
    )
    if "is_total_return" in aggregated.columns:
        fig.add_trace(
            go.Scatter(
                x=[f"F{int(f)}" for f in aggregated["fold"]],
                y=_numeric(aggregated["is_total_return"]),
                mode="lines+markers",
                name="in sample",
                line=dict(color="#566573", width=1.2, dash="dot"),
            )
        )
    fig.update_yaxes(title_text="return within the window", tickformat=".0%")
    fig.update_xaxes(title_text="walk-forward fold")
    return _finish(fig, f"Walk-forward folds — {strategy_id}", height=height)


def sensitivity_figure(sensitivity_detail: pd.DataFrame, strategy_id: str, *, height: int = 400) -> go.Figure | None:
    """Total return as each parameter is perturbed around the published value."""
    if sensitivity_detail is None or sensitivity_detail.empty:
        return None
    if "strategy_id" not in sensitivity_detail.columns or "param" not in sensitivity_detail.columns:
        return None
    frame = sensitivity_detail[sensitivity_detail["strategy_id"] == strategy_id]
    if frame.empty:
        return None
    base = frame[frame["param"] == "(base)"]
    base_return = float(base["total_return"].iloc[0]) if len(base) and pd.notna(base["total_return"].iloc[0]) else None
    variants = frame[(frame["param"] != "(base)") & frame["ok"].fillna(False)]
    if variants.empty:
        return None

    fig = go.Figure()
    for param, group in variants.groupby("param", sort=True):
        group = group.sort_values("factor")
        fig.add_trace(
            go.Scatter(
                x=pd.to_numeric(group["factor"], errors="coerce"),
                y=pd.to_numeric(group["total_return"], errors="coerce"),
                mode="lines+markers",
                name=str(param),
                hovertemplate=f"{param} x%{{x:.2f}}: %{{y:.2%}}<extra></extra>",
            )
        )
    if base_return is not None and np.isfinite(base_return):
        fig.add_hline(
            y=base_return,
            line_dash="dash",
            line_color="#34495e",
            annotation_text="published setting",
            annotation_position="top left",
        )
        fig.add_trace(
            go.Scatter(
                x=[1.0],
                y=[base_return],
                mode="markers",
                marker=dict(symbol="diamond", size=11, color="#34495e"),
                name="base case",
                hovertemplate="base: %{y:.2%}<extra></extra>",
            )
        )
    fig.update_xaxes(title_text="parameter multiplier applied to the published value", tickformat=".2f")
    fig.update_yaxes(title_text="total return", tickformat=".0%")
    return _finish(fig, f"Parameter sensitivity — {strategy_id}", height=height)


def checks_heatmap(checks: pd.DataFrame, *, height: int | None = None) -> go.Figure | None:
    """Pass / fail / untested matrix of every validation check."""
    if checks is None or checks.empty:
        return None
    frame = checks.pivot_table(index="strategy_id", columns="check", values="passed", aggfunc="first")
    if frame.empty:
        return None
    z = frame.map(lambda v: np.nan if pd.isna(v) else (1.0 if bool(v) else 0.0)).to_numpy(dtype=float)
    text = np.vectorize(lambda v: "n/a" if pd.isna(v) else ("pass" if v == 1.0 else "fail"))(z)

    fig = go.Figure(
        go.Heatmap(
            z=z,
            x=[str(c) for c in frame.columns],
            y=[str(i) for i in frame.index],
            text=text,
            texttemplate="%{text}",
            textfont=dict(size=9),
            colorscale=[[0.0, "#c0392b"], [0.5, "#c0392b"], [0.5, "#1e8449"], [1.0, "#1e8449"]],
            zmin=0,
            zmax=1,
            showscale=False,
            hovertemplate="%{y} / %{x}: %{text}<extra></extra>",
        )
    )
    fig.update_yaxes(type="category", autorange="reversed")
    fig.update_layout(xaxis_title=None, yaxis_title="strategy")
    return _finish(
        fig,
        "Validation checks (red = failed, green = passed, n/a = not tested)",
        height=height or max(320, 22 * len(frame.index) + 150),
        hovermode="closest",
        showlegend=False,
    )


def coverage_figure(audit: pd.DataFrame, *, height: int = 420) -> go.Figure | None:
    """Per-symbol data coverage from the integrity audit, when one is supplied."""
    if audit is None or audit.empty or "symbol" not in audit.columns:
        return None
    column = next((c for c in ("rows", "bars", "n_rows") if c in audit.columns), None)
    if column is None:
        return None
    frame = audit.sort_values(column, ascending=True)
    fig = go.Figure(
        go.Bar(
            x=pd.to_numeric(frame[column], errors="coerce"),
            y=[str(s) for s in frame["symbol"]],
            orientation="h",
            marker_color=PALETTE[0],
            hovertemplate="%{y}: %{x} bars<extra></extra>",
        )
    )
    fig.update_xaxes(title_text="usable bars")
    return _finish(fig, "Data coverage by symbol", height=height, hovermode="closest", showlegend=False)
