"""Reporting: exports, figures and the HTML research report.

Everything in this package is a *view*. Nothing recomputes a performance
statistic, so a number in a CSV, a chart and the HTML report cannot disagree —
they are the same frame rendered three ways.
"""

from nifty_quant.reporting.exports import (
    ExportBundle,
    export_all,
    export_comparison,
    export_strategy,
    write_frame,
)
from nifty_quant.reporting.figures import (
    checks_heatmap,
    coverage_figure,
    drawdown_figure,
    equity_figure,
    metric_bar,
    monthly_heatmap,
    ranking_figure,
    regime_figure,
    risk_return_figure,
    sensitivity_figure,
    signal_history_figure,
    symbol_heatmap,
    walkforward_figure,
    walkforward_fold_figure,
)
from nifty_quant.reporting.formatting import format_frame, format_value
from nifty_quant.reporting.html import ReportBundle, build_html, write_html_report

__all__ = [
    "ExportBundle",
    "ReportBundle",
    "build_html",
    "checks_heatmap",
    "coverage_figure",
    "drawdown_figure",
    "equity_figure",
    "export_all",
    "export_comparison",
    "export_strategy",
    "format_frame",
    "format_value",
    "metric_bar",
    "monthly_heatmap",
    "ranking_figure",
    "regime_figure",
    "risk_return_figure",
    "sensitivity_figure",
    "signal_history_figure",
    "symbol_heatmap",
    "walkforward_figure",
    "walkforward_fold_figure",
    "write_frame",
    "write_html_report",
]
