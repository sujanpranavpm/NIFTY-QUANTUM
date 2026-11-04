"""Signal persistence: CSV and JSON exports.

These are thin wrappers around ``signals_to_frame`` so the signal engine does
not depend on the reporting package, while the reporting package can still
consume the same frame.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from nifty_quant.signals.schemas import Signal, signals_to_frame

LOG = logging.getLogger(__name__)


def save_signals(
    signals: list[Signal] | pd.DataFrame,
    directory: Path | str,
    *,
    tag: str | None = None,
) -> Path:
    """Write ``signals.csv`` and ``signals_metadata.json`` to ``directory``."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    frame = signals if isinstance(signals, pd.DataFrame) else signals_to_frame(signals)
    path = target / "signals.csv"
    frame.to_csv(path, index=False)

    metadata = {
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": int(len(frame)),
        "tag": tag,
    }
    if not frame.empty and "generated_at" in frame.columns:
        metadata["generated_at"] = str(frame["generated_at"].iloc[0])
    (target / "signals_metadata.json").write_text(
        json.dumps(metadata, indent=2, default=str), encoding="utf-8"
    )
    LOG.info("wrote %d signals to %s", len(frame), path)
    return path


def signals_by_strategy(signals: list[Signal] | pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Split a signal list or frame into per-strategy frames for the export tree."""
    frame = signals if isinstance(signals, pd.DataFrame) else signals_to_frame(signals)
    if frame.empty:
        return {}
    return {
        strategy_id: group.copy()
        for strategy_id, group in frame.groupby("strategy_id", sort=True)
    }
