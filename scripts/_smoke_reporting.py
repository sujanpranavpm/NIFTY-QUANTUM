"""End-to-end reporting smoke test on the recorded artifacts.

Deliberately reloads both the run and the validation from disk: the exports and
the HTML report must be reproducible from what was saved, without re-running a
backtest or a statistical test.
"""

import pandas as pd

from nifty_quant.reporting import export_all, figures, write_html_report
from nifty_quant.research.pipeline import load_results
from nifty_quant.research.results import ResearchResults
from nifty_quant.validation import load_validation

results = load_results("artifacts/smoke")
validation = load_validation("artifacts/validation_smoke")
audit = pd.read_csv("artifacts/smoke/data_audit.csv")

print("strategies reloaded:", results.strategy_ids)
print("trades reloaded:", len(results.all_trades()))
print("verdicts reloaded:", validation.verdicts["verdict"].value_counts().to_dict())

bundle = export_all(results, directory="artifacts/report_smoke", validation=validation)
print("\nexport root:", bundle.root)
print("comparison files:", sorted(bundle.files))
print("total files:", bundle.n_files)
for strategy_id, files in bundle.strategies.items():
    print(" ", strategy_id, len(files), sorted(files))

report = write_html_report(
    results,
    "reports/research_report_smoke.html",
    validation=validation,
    audit=audit,
)
print("\nreport:", report.path, f"{report.path.stat().st_size:,} bytes")
print("figures:", report.n_figures)
print("verdicts in report:", report.verdicts)

# ---- the unvalidated path must shout, not stay silent
from nifty_quant.reporting import build_html

unvalidated = build_html(results, title="Unvalidated smoke")
assert "No validation report attached" in unvalidated
unvalidated_path = report.path.with_name("research_report_unvalidated.html")
unvalidated_path.write_text(unvalidated, encoding="utf-8")
print("unvalidated report:", unvalidated_path, f"{unvalidated_path.stat().st_size:,} bytes")

# ---- empty-input behaviour (a gap must stay visible, never a made-up chart)
blank = ResearchResults(runs={}, metadata={})
assert figures.equity_figure(blank) is None
assert figures.drawdown_figure(blank) is None
assert figures.monthly_heatmap(blank, "orders_01") is None
assert figures.yearly_figure(blank, "orders_01") is None
assert figures.ranking_figure(pd.DataFrame()) is None
assert figures.ranking_figure(results.ranking()) is not None
assert figures.signal_history_figure(pd.DataFrame()) is None
assert figures.signal_history_figure(results.run("orders_01").trades()) is not None
assert figures.checks_heatmap(pd.DataFrame()) is None
print("\nempty-input guards: ok")
