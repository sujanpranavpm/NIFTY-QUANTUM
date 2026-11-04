"""Self-contained HTML research report.

The report is the human-readable wrapper around the same frames the CSVs hold:
twenty strategy profiles, the performance comparison, the per-strategy detail,
and — when a :class:`~nifty_quant.validation.report.ValidationReport` is passed —
the verdicts, checks, holdout, walk-forward, sensitivity and regime evidence.

Three deliberate constraints:

* every performance number is labelled with its validation verdict, or with an
  explicit "not validated" banner when no report is attached;
* nothing is formatted as a recommendation, and the footer repeats that this is
  a research artefact;
* a missing frame renders as nothing at all, never as a zero.

By default the Plotly bundle is loaded from a CDN, which keeps the file small.
Pass ``include_plotlyjs="inline"`` for a single file that works offline.
"""

from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
import plotly
import plotly.graph_objects as go

from nifty_quant.config import PATHS
from nifty_quant.reporting import figures
from nifty_quant.reporting.formatting import format_frame, format_value, header_label
from nifty_quant.research.results import ResearchResults, flatten_metadata
from nifty_quant.strategies.registry import spec_table

LOG = logging.getLogger(__name__)

COMPARISON_COLUMNS = (
    "strategy_id",
    "verdict",
    "family",
    "timeframe",
    "mode",
    "n_trades",
    "total_return",
    "cagr",
    "max_drawdown",
    "sharpe",
    "sortino",
    "calmar",
    "profit_factor",
    "win_rate",
    "expectancy",
    "avg_holding_bars",
    "fees_paid",
    "slippage_paid",
    "net_profit",
    "pct_symbols_positive",
    "sufficient_sample",
)

SYMBOL_COLUMNS = (
    "symbol",
    "n_trades",
    "total_return",
    "cagr",
    "max_drawdown",
    "sharpe",
    "win_rate",
    "profit_factor",
    "expectancy",
    "fees_paid",
    "slippage_paid",
    "net_profit",
)

PROFILE_FIELDS = (
    ("upstream_file", "Upstream file"),
    ("family", "Family"),
    ("timeframe", "Timeframe"),
    ("order_types", "Order types"),
    ("direction", "Direction"),
    ("run_mode", "Run mode"),
    ("required_data", "Required data"),
    ("applicable_to_nse_equity", "Applicable to NSE cash equity"),
    ("entry_logic", "Entry logic"),
    ("exit_logic", "Exit logic"),
    ("risk_logic", "Risk / stops"),
    ("source_summary", "Source summary"),
    ("adaptation_notes", "Adaptation for Indian markets"),
)

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 0 0 64px; background: #f4f6f7; color: #1f2933;
       font-family: Inter, Segoe UI, Helvetica, sans-serif; font-size: 14px; line-height: 1.5; }
main { max-width: 1240px; margin: 0 auto; padding: 0 24px; }
header { background: #1f2933; color: #fff; padding: 28px 0 22px; margin-bottom: 24px; }
header .inner { max-width: 1240px; margin: 0 auto; padding: 0 24px; }
header h1 { margin: 0 0 6px; font-size: 24px; font-weight: 600; letter-spacing: .2px; }
header p { margin: 0; color: #cbd2d9; font-size: 13px; }
header table { margin-top: 14px; border-collapse: collapse; font-size: 12px; color: #cbd2d9; }
header table td { padding: 2px 18px 2px 0; vertical-align: top; }
header table td.k { color: #8d9aa5; }
h2 { font-size: 18px; margin: 34px 0 10px; padding-bottom: 6px; border-bottom: 2px solid #d5d8dc; }
h3 { font-size: 15px; margin: 22px 0 8px; }
h4 { font-size: 13px; margin: 16px 0 6px; color: #52606d; text-transform: uppercase; letter-spacing: .5px; }
p.lead { color: #52606d; margin: 0 0 12px; }
.banner { padding: 14px 16px; border-radius: 6px; margin: 0 0 18px; border-left: 5px solid; }
.banner strong { display: block; margin-bottom: 4px; }
.banner.warn { background: #fdf3e7; border-color: #b7791f; }
.banner.bad { background: #fdecea; border-color: #c0392b; }
.banner.ok { background: #eaf5ee; border-color: #1e8449; }
.banner.info { background: #eef3f8; border-color: #1f4e79; }
table.data { width: 100%; border-collapse: collapse; font-size: 12px; background: #fff;
             margin: 8px 0 16px; box-shadow: 0 1px 2px rgba(31,41,51,.08); }
table.data th { background: #eef1f4; text-align: right; padding: 7px 8px; font-weight: 600;
                color: #33424f; border-bottom: 1px solid #d5d8dc; white-space: nowrap; }
table.data th:first-child, table.data td:first-child { text-align: left; }
table.data td { padding: 6px 8px; text-align: right; border-bottom: 1px solid #f0f2f4;
                font-variant-numeric: tabular-nums; white-space: nowrap; }
table.data tbody tr:hover { background: #f8fafb; }
table.data td.text, table.data th.text { text-align: left; white-space: normal; }
.scroll { overflow-x: auto; }
figure { margin: 0 0 18px; background: #fff; border-radius: 6px; padding: 6px 6px 0;
         box-shadow: 0 1px 2px rgba(31,41,51,.08); }
details.strat { background: #fff; border-radius: 6px; margin: 0 0 12px; padding: 10px 14px;
                box-shadow: 0 1px 2px rgba(31,41,51,.08); }
details.strat > summary { cursor: pointer; font-weight: 600; font-size: 14px; padding: 4px 0; }
details.strat > summary .meta { font-weight: 400; color: #6b7a86; margin-left: 8px; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px;
        font-weight: 600; color: #fff; margin-left: 8px; vertical-align: 1px; }
.pill.supported { background: #1e8449; }
.pill.weak { background: #b7791f; }
.pill.unsupported { background: #c0392b; }
.pill.untested { background: #95a5a6; }
.pill.none { background: #7f8c8d; }
dl.profile { display: grid; grid-template-columns: 190px 1fr; gap: 4px 14px; margin: 10px 0 16px;
             font-size: 12.5px; }
dl.profile dt { color: #52606d; font-weight: 600; }
dl.profile dd { margin: 0; }
pre { background: #f4f6f7; border: 1px solid #e1e5e8; border-radius: 4px; padding: 8px 10px;
      font-size: 11.5px; overflow-x: auto; }
footer { max-width: 1240px; margin: 40px auto 0; padding: 18px 24px 0; border-top: 1px solid #d5d8dc;
         color: #6b7a86; font-size: 12px; }
.note { color: #6b7a86; font-size: 12px; margin: -6px 0 14px; }
"""


@dataclass
class ReportBundle:
    """Where the report went and what it claims."""

    path: Path
    title: str
    n_strategies: int
    n_figures: int
    verdicts: dict[str, str] = field(default_factory=dict)
    validation_attached: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "title": self.title,
            "n_strategies": self.n_strategies,
            "n_figures": self.n_figures,
            "validation_attached": self.validation_attached,
            "verdicts": self.verdicts,
        }


# --------------------------------------------------------------------------
# Small HTML helpers
# --------------------------------------------------------------------------
def _escape(text: Any) -> str:
    return html.escape(str(text), quote=False)


def _table(
    frame: pd.DataFrame,
    *,
    columns: Sequence[str] | None = None,
    classes: str = "data",
    max_rows: int | None = None,
    text_columns: Sequence[str] = (),
) -> str:
    """Render a frame as a table with display formatting applied."""
    rendered = format_frame(frame, columns)
    if rendered.empty:
        return ""
    truncated = False
    if max_rows is not None and len(rendered) > max_rows:
        rendered = rendered.head(max_rows)
        truncated = True
    head = "".join(f"<th>{_escape(c)}</th>" for c in rendered.columns)
    # Text columns are matched on the rendered header, because that is what the
    # rows are keyed on after formatting.
    left = {header_label(c).lower() for c in text_columns}
    rows = []
    for _, row in rendered.iterrows():
        cells = []
        for column in rendered.columns:
            value = row[column]
            css = ' class="text"' if str(column).lower() in left else ""
            cells.append(f"<td{css}>{_escape(value)}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    note = (
        f'<p class="note">showing the first {max_rows} of {len(frame)} rows — the CSV export holds all of them.</p>'
        if truncated
        else ""
    )
    return (
        f'<div class="scroll"><table class="{classes}"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>{note}"
    )


def _figure(fig: go.Figure | None, div_id: str) -> str:
    if fig is None:
        return ""
    return "<figure>" + fig.to_html(
        full_html=False,
        include_plotlyjs=False,
        div_id=div_id,
        config={"displaylogo": False, "responsive": True},
    ) + "</figure>"


def _plotly_bundle(mode: str) -> str:
    if mode == "inline":
        from plotly.offline import get_plotlyjs

        return f"<script type='text/javascript'>{get_plotlyjs()}</script>"
    version = getattr(plotly, "__version__", "")
    src = f"https://cdn.plot.ly/plotly-{version}.min.js" if version else "https://cdn.plot.ly/plotly-latest.min.js"
    return f'<script src="{src}" charset="utf-8"></script>'


def _banner(kind: str, title: str, body: str) -> str:
    return f'<div class="banner {kind}"><strong>{_escape(title)}</strong>{body}</div>'


def _validation_banner(validation) -> str:
    disclaimer = " Performance figures in this report are descriptive statistics of a backtest, not evidence of an edge."
    if validation is None:
        return _banner(
            "bad",
            "No validation report attached",
            "Every number below comes from a single in-sample backtest and has not been checked against a "
            "holdout, walk-forward, parameter sensitivity or multiplicity controls." + disclaimer,
        )
    verdicts = getattr(validation, "verdicts", pd.DataFrame())
    if verdicts is None or verdicts.empty:
        return _banner("warn", "Validation produced no verdicts", "No strategy was assessed." + disclaimer)
    counts = verdicts["verdict"].value_counts().to_dict()
    summary = ", ".join(f"{counts.get(k, 0)} {k}" for k in ("supported", "weak", "unsupported", "untested"))
    supported = sorted(verdicts.loc[verdicts["verdict"] == "supported", "strategy_id"])
    if supported:
        return _banner(
            "ok",
            f"{len(supported)} strategy(ies) passed every critical check",
            f"Verdicts: {summary}. Supported: {_escape(', '.join(supported))}. Support means the strategy "
            "survived the declared tests on this data and cost model; it is not a forecast." + disclaimer,
        )
    return _banner(
        "warn",
        "No strategy cleared the validation thresholds",
        f"Verdicts: {summary}. Read the check matrix before looking at returns: a high backtest return "
        "that fails the holdout, breadth or multiplicity checks is a fitted artefact, not an edge." + disclaimer,
    )


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------
def _header_section(results: ResearchResults, title: str) -> str:
    meta = results.metadata
    cells = [
        ("Universe", meta.get("universe", "n/a")),
        ("Interval", meta.get("interval", "n/a")),
        ("Period", f"{meta.get('start', '?')} → {meta.get('end', '?')}"),
        ("Symbols run", meta.get("symbols_run", len({s.label for r in results.runs.values() for s in r.slices}))),
        ("Strategies", len(results.runs)),
        ("Sessions", meta.get("sessions", "n/a")),
    ]
    cell_html = "".join(f'<td class="k">{_escape(k)}</td><td>{_escape(v)}</td>' for k, v in cells)
    return (
        "<header><div class='inner'>"
        f"<h1>{_escape(title)}</h1>"
        "<p>Research artefact produced by the NIFTY 50 quantitative strategy research engine. "
        "No order was routed, no position was taken, and nothing here is investment advice.</p>"
        f"<table><tr>{cell_html}</tr></table>"
        "</div></header>"
    )


def _validation_section(validation) -> str:
    if validation is None:
        return ""
    parts = ["<h2>Validation</h2>"]
    split = validation.split.as_row()
    parts.append(
        "<p class='lead'>Development "
        f"{_escape(split['dev_start'].date())} → {_escape(split['dev_end'].date())} "
        f"({split['dev_bars']} bars), embargo {split['purge_bars']} bars, held out "
        f"{_escape(split['oos_start'].date())} → {_escape(split['oos_end'].date())} "
        f"({split['oos_bars']} bars). Thresholds are declared in "
        "<code>nifty_quant/validation/report.py</code> and applied identically to every strategy.</p>"
    )
    parts.append("<h3>Verdicts</h3>")
    parts.append(
        _table(
            validation.verdicts,
            text_columns=("strategy_id", "verdict", "failed_checks", "untested_checks"),
        )
    )
    parts.append("<h3>Check matrix</h3>")
    parts.append(_figure(figures.checks_heatmap(validation.checks), "fig_checks"))
    details = validation.checks.pivot_table(
        index=["strategy_id", "check"], values="value", aggfunc="first"
    ).reset_index()
    if not details.empty and "value" in details.columns:
        parts.append("<h4>Observed values behind each check</h4>")
        parts.append(_table(details, text_columns=("strategy_id", "check")))

    parts.append("<h3>Holdout</h3>")
    parts.append(_table(validation.dev_oos, text_columns=("strategy_id",)))
    if not validation.strict_split.empty:
        parts.append("<h4>Held-out window: continuous run vs restart from the boundary</h4>")
        parts.append(_table(validation.strict_split, text_columns=("strategy_id", "symbol", "note")))

    if not validation.walkforward_summary.empty:
        parts.append("<h3>Walk-forward</h3>")
        parts.append(_figure(figures.walkforward_figure(validation.walkforward_summary), "fig_wf"))
        parts.append(_table(validation.walkforward_summary, text_columns=("strategy_id",)))

    if not validation.sensitivity.empty:
        parts.append("<h3>Parameter sensitivity</h3>")
        parts.append(
            "<p class='lead'>Each numeric parameter is moved one step at a time and the whole backtest is "
            "re-run. A strategy that only earns at the published value is a knife-edge fit.</p>"
        )
        parts.append(
            _table(validation.sensitivity, text_columns=("strategy_id", "param", "verdict"))
        )

    if not validation.robustness.empty:
        parts.append("<h3>Multiplicity-aware statistics</h3>")
        parts.append(
            "<p class='lead'>Probabilistic and deflated Sharpe account for the number of sleeves tried; the "
            "Benjamini–Hochberg q-value controls the false-discovery rate across the breadth sign tests.</p>"
        )
        parts.append(_table(validation.robustness, text_columns=("strategy_id",)))

    if not validation.regime_equity.empty:
        parts.append("<h3>Market regimes</h3>")
        parts.append(_figure(figures.regime_figure(validation.regime_equity), "fig_regime"))
        parts.append(_table(validation.regime_equity, text_columns=("strategy_id", "regime")))
    if not validation.regime_trades.empty:
        parts.append("<h4>Trades by regime</h4>")
        parts.append(_table(validation.regime_trades, text_columns=("strategy_id", "regime")))

    if not validation.consistency.empty:
        parts.append("<h3>Stock-by-stock consistency</h3>")
        parts.append(_table(validation.consistency, text_columns=("strategy_id",)))
    return "".join(parts)


def _comparison_section(results: ResearchResults, validation) -> str:
    metrics = results.strategy_metrics()
    if metrics.empty:
        return "<h2>Performance</h2><p class='lead'>No strategy produced an equity curve.</p>"
    if validation is not None and not getattr(validation, "verdicts", pd.DataFrame()).empty:
        metrics = metrics.merge(
            validation.verdicts[["strategy_id", "verdict"]], on="strategy_id", how="left"
        )

    parts = ["<h2>Performance comparison</h2>"]
    parts.append(
        "<p class='lead'>Every figure below is computed from the recorded equity curve and fills, after "
        "NSE delivery costs and the configured slippage, on the same capital per sleeve.</p>"
    )
    parts.append(_table(metrics, columns=COMPARISON_COLUMNS, text_columns=("strategy_id", "verdict")))

    parts.append("<h3>Equity curves</h3>")
    parts.append(_figure(figures.equity_figure(results), "fig_equity"))
    parts.append("<h3>Drawdowns</h3>")
    parts.append(_figure(figures.drawdown_figure(results), "fig_drawdown"))
    parts.append("<h3>Ranking</h3>")
    parts.append(
        "<p class='lead'>Ranked on the median per-symbol Sharpe with a minimum-trade gate, never on total "
        "return, and coloured by validation verdict where one exists.</p>"
    )
    ranking = results.ranking()
    verdicts = getattr(validation, "verdicts", None)
    parts.append(_figure(figures.ranking_figure(ranking, verdicts), "fig_ranking"))
    parts.append("<h3>Risk versus return</h3>")
    parts.append(_figure(figures.risk_return_figure(metrics), "fig_risk"))
    parts.append("<h3>Stock-wise performance</h3>")
    parts.append(_figure(figures.symbol_heatmap(results.symbol_metrics(), "total_return"), "fig_symbols"))
    parts.append("<h4>Per-symbol detail</h4>")
    parts.append(
        _table(
            results.symbol_metrics(),
            columns=SYMBOL_COLUMNS,
            text_columns=("strategy_id", "symbol"),
        )
    )
    return "".join(parts)


def _profile_html(spec) -> str:
    rows = []
    for field_name, label in PROFILE_FIELDS:
        value = getattr(spec, field_name, "")
        if isinstance(value, (tuple, list)):
            value = ", ".join(str(v) for v in value)
        if isinstance(value, bool):
            value = "yes" if value else "no"
        if value in ("", None):
            continue
        rows.append(f"<dt>{_escape(label)}</dt><dd>{_escape(value)}</dd>")
    return "<dl class='profile'>" + "".join(rows) + "</dl>"


def _strategy_section(results: ResearchResults, validation, *, signal_history: bool = True) -> str:
    if not results.runs:
        return ""
    verdict_lookup: dict[str, str] = {}
    if validation is not None and not getattr(validation, "verdicts", pd.DataFrame()).empty:
        verdict_lookup = dict(zip(validation.verdicts["strategy_id"], validation.verdicts["verdict"]))

    parts = ["<h2>Strategy detail</h2>"]
    parts.append(
        "<p class='lead'>The profile of each strategy is reproduced from its registry metadata — the same text "
        "that documents the port — so the logic shown here is the logic that was backtested.</p>"
    )
    for strategy_id, run in results.runs.items():
        verdict = verdict_lookup.get(strategy_id)
        pill = (
            f"<span class='pill {verdict}'>{_escape(verdict)}</span>"
            if verdict
            else "<span class='pill none'>not validated</span>"
        )
        metrics = run.aggregate_metrics()
        summary = (
            f"return {format_value(metrics.get('total_return'), 'total_return')} &middot; "
            f"sharpe {format_value(metrics.get('sharpe'), 'sharpe')} &middot; "
            f"max dd {format_value(metrics.get('max_drawdown'), 'max_drawdown')} &middot; "
            f"{format_value(metrics.get('n_trades'), 'n_trades')} trades"
        )
        body = []
        body.append(_profile_html(run.spec))
        body.append(
            f"<h4>Run parameters</h4><pre>{_escape(json.dumps(run.params, indent=2, sort_keys=True, default=str))}</pre>"
        )
        body.append(
            f"<p class='note'>mode {_escape(run.mode)} &middot; interval {_escape(run.interval)} &middot; "
            f"fingerprint <code>{_escape(run.fingerprint)}</code> &middot; sleeves {len(run.slices)}</p>"
        )
        aggregate = pd.DataFrame([metrics])
        body.append(_table(aggregate, text_columns=("strategy_id",)))
        body.append(_figure(figures.monthly_heatmap(results, strategy_id), f"fig_month_{strategy_id}"))
        body.append(_figure(figures.yearly_figure(results, strategy_id), f"fig_year_{strategy_id}"))
        body.append(
            _figure(
                figures.signal_history_figure(run.trades(), strategy_id=strategy_id),
                f"fig_signals_{strategy_id}",
            )
        )
        body.append("<h4>Per-symbol results</h4>")
        body.append(_table(run.metrics_by_symbol(), columns=SYMBOL_COLUMNS, text_columns=("symbol",)))

        if validation is not None and not getattr(validation, "dev_oos", pd.DataFrame()).empty:
            dev = validation.dev_oos[validation.dev_oos["strategy_id"] == strategy_id]
            if not dev.empty:
                body.append("<h4>Holdout result</h4>")
                body.append(_table(dev, text_columns=("strategy_id",)))
        if validation is not None and not getattr(validation, "walkforward", pd.DataFrame()).empty:
            body.append(
                _figure(
                    figures.walkforward_fold_figure(validation.walkforward, strategy_id),
                    f"fig_wf_{strategy_id}",
                )
            )
            body.append(
                _figure(
                    figures.sensitivity_figure(validation.sensitivity_detail, strategy_id),
                    f"fig_sens_{strategy_id}",
                )
            )

        parts.append(
            f"<details class='strat'><summary>{_escape(strategy_id)} — {_escape(run.spec.name)}"
            f"<span class='meta'>{summary}</span>{pill}</summary>{''.join(body)}</details>"
        )
    return "".join(parts)


def _profiles_section(results: ResearchResults) -> str:
    """All twenty profiles, including the two that are not NSE-cash applicable."""
    table = spec_table()
    if table.empty:
        return ""
    columns = (
        "strategy_id",
        "name",
        "family",
        "timeframe",
        "order_types",
        "run_mode",
        "applicable_to_nse_equity",
        "entry_logic",
        "exit_logic",
    )
    ran = set(results.runs)
    table = table.copy()
    table["backtested"] = table["strategy_id"].isin(ran).map({True: "yes", False: "no"})
    columns = columns + ("backtested",)
    return (
        "<h2>Strategy inventory</h2>"
        "<p class='lead'>All twenty order-based strategies, in upstream file order. "
        "Entry and exit summaries are the registry's own description of the ported logic.</p>"
        + _table(table, columns=columns, text_columns=("strategy_id", "name", "family", "order_types", "run_mode", "entry_logic", "exit_logic"))
    )


def _footer(results: ResearchResults, validation, *, plotly_mode: str) -> str:
    flat = flatten_metadata(results.metadata)
    lines = [f"<strong>{_escape(k)}</strong>: {_escape(v)}" for k, v in sorted(flat.items()) if not isinstance(v, (dict, list))]
    config_lines = " &middot; ".join(lines[:12])
    thresholds = ""
    if validation is not None:
        payload = json.dumps(validation.metadata.get("thresholds", {}), indent=2, sort_keys=True)
        thresholds = f"<h4>Validation thresholds</h4><pre>{_escape(payload)}</pre>"
    network = (
        "<p class='note'>Plotly is loaded from a CDN in this file; open it with an internet connection or "
        "regenerate with include_plotlyjs='inline'.</p>"
        if plotly_mode != "inline"
        else ""
    )
    return (
        "<footer>"
        "<p>Every figure above is derived from the recorded run in the artifact directory listed in the run "
        "metadata. Nothing is hardcoded, no result was selected because it looked good, and the same call "
        "reproduces the same numbers. Backtests are not forecasts: survivorship bias is only removed where "
        "historical index membership was available, and free OHLCV data is not exchange-grade.</p>"
        f"{thresholds}"
        f"<p class='note'>{config_lines}</p>"
        f"<p class='note'>Generated {_escape(datetime.now(timezone.utc).isoformat(timespec='seconds'))}.</p>"
        f"{network}"
        "</footer>"
    )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def build_html(
    results: ResearchResults,
    *,
    validation: Any | None = None,
    title: str | None = None,
    audit: pd.DataFrame | None = None,
    include_plotlyjs: str = "cdn",
    include_profiles: bool = True,
    include_strategy_detail: bool = True,
) -> str:
    """Assemble the full report document."""
    resolved_title = title or "NIFTY 50 strategy research report"
    body = [
        _header_section(results, resolved_title),
        "<main>",
        _validation_banner(validation),
    ]
    if results.metadata.get("survivorship_bias_exposed", True):
        body.append(
            _banner(
                "info",
                "Survivorship caveat",
                "The universe is the current NIFTY 50 snapshot. Historical constituent membership was not "
                "available for this run, so names that left the index are absent and the results are "
                "optimistic by an unknown amount.",
            )
        )
    body.append(_validation_section(validation))
    body.append(_comparison_section(results, validation))
    if audit is not None and not audit.empty:
        body.append("<h3>Data coverage</h3>")
        body.append(_figure(figures.coverage_figure(audit), "fig_coverage"))
        body.append(_table(audit, max_rows=60, text_columns=("symbol",)))
    if include_profiles:
        body.append(_profiles_section(results))
    if include_strategy_detail:
        body.append(_strategy_section(results, validation))
    body.append(_footer(results, validation, plotly_mode=include_plotlyjs))
    body.append("</main>")

    document = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_escape(resolved_title)}</title>"
        f"<style>{CSS}</style>{_plotly_bundle(include_plotlyjs)}</head>"
        f"<body>{''.join(body)}</body></html>"
    )
    return document


def write_html_report(
    results: ResearchResults,
    path: Path | str | None = None,
    *,
    validation: Any | None = None,
    title: str | None = None,
    audit: pd.DataFrame | None = None,
    include_plotlyjs: str = "cdn",
    directory: Path | str | None = None,
) -> ReportBundle:
    """Render and write the report; returns where it landed."""
    resolved_title = title or "NIFTY 50 strategy research report"
    if path is None:
        root = Path(directory) if directory is not None else PATHS.reports
        root.mkdir(parents=True, exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = root / f"research_report_{name}.html"
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    document = build_html(
        results,
        validation=validation,
        title=resolved_title,
        audit=audit,
        include_plotlyjs=include_plotlyjs,
    )
    target.write_text(document, encoding="utf-8")

    verdicts: dict[str, str] = {}
    if validation is not None and not getattr(validation, "verdicts", pd.DataFrame()).empty:
        verdicts = dict(zip(validation.verdicts["strategy_id"], validation.verdicts["verdict"]))
    bundle = ReportBundle(
        path=target,
        title=resolved_title,
        n_strategies=len(results.runs),
        n_figures=document.count("<div id="),
        verdicts=verdicts,
        validation_attached=validation is not None,
    )
    LOG.info("wrote report %s (%d strategies, %d figures)", target, bundle.n_strategies, bundle.n_figures)
    return bundle
