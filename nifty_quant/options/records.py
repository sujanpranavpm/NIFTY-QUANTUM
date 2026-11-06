"""Per-trade and per-rejection records, and their persistence.

Requirement P2.2.3-10: every executed trade is recorded with the strategy, the
direction, the concrete contract (symbol/strike/expiry), the days-to-expiry at
entry, the entry and exit, the costs, the scenario and the cost-verification status.
Requirement P2.2.3-9: **every rejected signal is recorded with its reason** - an
opportunity the data did not support is a reported fact, not a silently dropped row.

A cost-verification flag is carried on every trade row so a reader can never mistake
a brokerage-excluded figure for a fully all-in net profit (the P2.1.2 discipline).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd


@dataclass
class TradeRecord:
    strategy_id: str
    direction: str
    option_type: str
    symbol: str
    strike: float
    expiry: str
    dte_at_entry: int
    moneyness: str
    signal_date: str
    entry_date: str
    exit_date: Optional[str]
    lot_size: int
    lots: int
    scenario: str
    status: str
    exit_reason: Optional[str]
    bars_held: int
    entry_premium: Optional[float]
    exit_premium: Optional[float]
    gross_pnl: float
    total_costs: float
    net_pnl: float
    statutory_verified: bool
    all_in_verified: bool
    brokerage_excluded: bool
    cost_basis: str
    fallback_used: bool = False
    tick_size: float = 0.0
    strike_interval: float = 0.0


@dataclass
class RejectionRecord:
    strategy_id: str
    signal_date: str
    entry_date: Optional[str]
    direction: str
    spot: float
    reason: str
    stage: str                       # "selection" | "bars" | "execution"
    context: dict = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["context"] = json.dumps(self.context, default=str)
        return row


@dataclass
class RecordSet:
    trades: list[TradeRecord] = field(default_factory=list)
    rejections: list[RejectionRecord] = field(default_factory=list)

    def trades_frame(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(t) for t in self.trades])

    def rejections_frame(self) -> pd.DataFrame:
        return pd.DataFrame([r.as_row() for r in self.rejections])


def persist_run(records: RecordSet, out_dir: Path, *, summary: Optional[dict] = None) -> dict:
    """Write trades/rejections as parquet + csv and a JSON summary; return paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    trades = records.trades_frame()
    rejections = records.rejections_frame()
    paths: dict[str, str] = {}

    if not trades.empty:
        trades.to_parquet(out_dir / "trades.parquet", index=False)
        trades.to_csv(out_dir / "trades.csv", index=False)
        paths["trades"] = str(out_dir / "trades.csv")
    if not rejections.empty:
        rejections.to_parquet(out_dir / "rejections.parquet", index=False)
        rejections.to_csv(out_dir / "rejections.csv", index=False)
        paths["rejections"] = str(out_dir / "rejections.csv")

    counts = dict(trades=len(trades), rejections=len(rejections))
    if not rejections.empty:
        counts["by_reason"] = rejections["reason"].value_counts().to_dict()
    if not trades.empty:
        counts["by_status"] = trades["status"].value_counts().to_dict()
    payload = {"counts": counts, **(summary or {})}
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, default=str),
                                          encoding="utf-8")
    paths["summary"] = str(out_dir / "summary.json")
    return paths
