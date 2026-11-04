"""Strategy-wise CSV exports.

Every table written here is a projection of a recorded run. Nothing is
recomputed, nothing is rounded, and nothing is invented: given the artifact
directory of a run, any file produced here can be re-derived byte-for-byte from
``results`` plus ``validation``.

Layout::

    <root>/
        strategy_metrics.csv      one row per strategy, aggregate metrics
        symbol_metrics.csv        one row per strategy x symbol
        ranking.csv               descriptive ranking, sample-size gated
        strategy_specs.csv        all twenty profiles, including the excluded two
        trades.csv                every round-trip trade, all strategies
        signals.csv               every fill (entry/exit event log)
        monthly_returns.csv       long format: strategy, year, month, return
        run_metadata.json         provenance of the result set itself
        strategies/<id>/          per-strategy detail
            metrics.csv  symbol_metrics.csv  trades.csv  signals.csv
            orders.csv  equity.csv  monthly_returns.csv  yearly_returns.csv
        validation/               copied from ``save_validation`` when supplied
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from nifty_quant.config import PATHS
from nifty_quant.research.results import ResearchResults
from nifty_quant.signals.io import signals_by_strategy
from nifty_quant.signals.schemas import Signal, signals_to_frame
from nifty_quant.strategies.registry import spec_table

LOG = logging.getLogger(__name__)

COMBINED_COLUMN = "COMBINED"


@dataclass
class ExportBundle:
    """Paths written by one export call."""

    root: Path
    files: dict[str, Path] = field(default_factory=dict)
    strategies: dict[str, dict[str, Path]] = field(default_factory=dict)
    validation_dir: Path | None = None

    @property
    def n_files(self) -> int:
        return len(self.files) + sum(len(v) for v in self.strategies.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "files": {k: str(v) for k, v in self.files.items()},
            "strategies": {k: {n: str(p) for n, p in v.items()} for k, v in self.strategies.items()},
            "validation_dir": str(self.validation_dir) if self.validation_dir else "",
            "n_files": self.n_files,
        }


def write_frame(frame: pd.DataFrame | None, path: Path, *, index: bool = False) -> Path | None:
    """Write one frame; an empty frame produces no file at all.

    An absent file is the honest representation of "this run produced nothing
    here"; an empty CSV invites the reader to believe a table was computed.
    """
    if frame is None or frame.empty:
        return None
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=index, index_label="timestamp" if index else None)
    return path


def export_comparison(
    results: ResearchResults,
    directory: Path | str,
    *,
    write_specs: bool = True,
    write_metadata: bool = True,
) -> dict[str, Path]:
    """The cross-strategy tables (the comparison surfaces of the dashboard)."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)

    files: dict[str, Path] = {}
    candidates = {
        "strategy_metrics": results.strategy_metrics(),
        "symbol_metrics": results.symbol_metrics(),
        "ranking": results.ranking(),
        "trades": results.all_trades(),
        "signals": results.signal_history(),
        "monthly_returns": results.monthly_return_panel(),
    }
    if write_specs:
        candidates["strategy_specs"] = spec_table()
    for name, frame in candidates.items():
        written = write_frame(frame, root / f"{name}.csv")
        if written is not None:
            files[name] = written

    if write_metadata:
        payload = dict(results.metadata)
        payload["exported_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload["strategy_fingerprints"] = {
            sid: run.fingerprint for sid, run in results.runs.items()
        }
        path = root / "run_metadata.json"
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        files["run_metadata"] = path
    return files


def export_strategy(
    results: ResearchResults,
    strategy_id: str,
    directory: Path | str,
) -> dict[str, Path]:
    """Everything recorded for one strategy, in one directory."""
    run = results.run(strategy_id)
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)

    metrics = run.aggregate_metrics()
    header = {
        "strategy_id": strategy_id,
        "name": run.spec.name,
        "family": run.spec.family,
        "timeframe": run.spec.timeframe,
        "mode": run.mode,
        "interval": run.interval,
        "applicable_to_nse_equity": run.spec.applicable_to_nse_equity,
        "fingerprint": run.fingerprint,
        "report_start": run.report_start,
        "report_end": run.report_end,
        "n_sleeves": len(run.slices),
        "params": json.dumps(run.params, sort_keys=True, default=str),
    }
    metrics_row = pd.DataFrame([{**header, **metrics}])

    equity = pd.DataFrame(
        {s.label: s.equity["equity"] for s in run.slices if not s.equity.empty}
    )
    combined = run.combined_equity()
    if not equity.empty and not combined.empty:
        equity[COMBINED_COLUMN] = combined
    elif equity.empty and not combined.empty:
        equity[COMBINED_COLUMN] = combined

    monthly = results.monthly_returns(strategy_id)
    monthly_long = pd.DataFrame()
    if not monthly.empty:
        monthly_long = (
            monthly.reset_index()
            .melt(id_vars="year", var_name="month", value_name="return")
            .dropna(subset=["return"])
        )
        monthly_long = monthly_long.assign(strategy_id=strategy_id)

    yearly = results.yearly_returns(strategy_id)
    yearly_frame = pd.DataFrame(
        {"strategy_id": strategy_id, "year": yearly.index, "return": yearly.to_numpy()}
    )

    orders = _orders_frame(run)

    written: dict[str, Path] = {}
    candidates = {
        "metrics": (metrics_row, False),
        "symbol_metrics": (run.metrics_by_symbol(), False),
        "trades": (run.trades(), False),
        "signals": (run.fills(), False),
        "orders": (orders, False),
        "equity": (equity, True),
        "monthly_returns": (monthly_long, False),
        "yearly_returns": (yearly_frame, False),
    }
    for name, (frame, with_index) in candidates.items():
        path = write_frame(frame, root / f"{name}.csv", index=with_index)
        if path is not None:
            written[name] = path
    return written


def export_all(
    results: ResearchResults,
    *,
    directory: Path | str | None = None,
    validation: Any | None = None,
    signals: list[Signal] | pd.DataFrame | None = None,
    tag: str | None = None,
    per_strategy: bool = True,
) -> ExportBundle:
    """Write the full export set and return the paths that were created.

    Args:
        directory: target root; defaults to ``artifacts/exports/<tag>``.
        validation: a :class:`~nifty_quant.validation.report.ValidationReport`;
            when given its frames are exported next to the performance tables.
        signals: verdict-gated signal output from :class:`SignalEngine`; written
            as ``signals.csv`` at the root and per strategy when ``per_strategy``.
        tag: sub-directory name when ``directory`` is omitted.
        per_strategy: write the ``strategies/<id>/`` detail tree.
    """
    root = (
        Path(directory)
        if directory is not None
        else PATHS.artifacts / "exports" / (tag or _timestamp())
    )
    root.mkdir(parents=True, exist_ok=True)

    bundle = ExportBundle(root=root)
    bundle.files = export_comparison(results, root)
    if signals is not None:
        signal_frame = signals if isinstance(signals, pd.DataFrame) else signals_to_frame(signals)
        written = write_frame(signal_frame, root / "signals.csv")
        if written is not None:
            bundle.files["signals_generated"] = written
        if per_strategy:
            for strategy_id, frame in signals_by_strategy(signal_frame).items():
                bundle.strategies.setdefault(strategy_id, {})
                path = write_frame(frame, root / "strategies" / strategy_id / "signals.csv")
                if path is not None:
                    bundle.strategies[strategy_id]["signals"] = path

    if per_strategy:
        for strategy_id in results.strategy_ids:
            bundle.strategies.setdefault(strategy_id, {})
            bundle.strategies[strategy_id].update(
                export_strategy(results, strategy_id, root / "strategies" / strategy_id)
            )

    if validation is not None:
        from nifty_quant.validation.report import save_validation

        bundle.validation_dir = save_validation(validation, root / "validation")

    LOG.info(
        "exported %d strategy bundles and %d comparison files to %s",
        len(bundle.strategies),
        len(bundle.files),
        root,
    )
    return bundle


def _orders_frame(run) -> pd.DataFrame:
    """Order lifecycle across sleeves, tagged with the sleeve they belong to."""
    frames = []
    for run_slice in run.slices:
        orders = getattr(run_slice, "orders", None)
        if orders is None or orders.empty:
            continue
        frames.append(orders.assign(sleeve=run_slice.label))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
