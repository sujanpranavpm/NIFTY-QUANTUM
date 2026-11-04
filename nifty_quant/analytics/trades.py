"""Reconstruct round-trip trades from a fill ledger.

A trade is one continuous holding episode in one symbol. Partial exits are split
across trades by allocating entry-side fees and slippage pro rata to the closed
share count, so the per-trade costs always sum back to the portfolio totals.

Nothing here infers profitability from anything except recorded fills and the
mark price used for positions still open when the sample ends.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

TRADE_COLUMNS = [
    "strategy_id",
    "symbol",
    "direction",
    "entry_time",
    "exit_time",
    "bars_held",
    "entry_price",
    "exit_price",
    "quantity",
    "gross_pnl",
    "fees",
    "slippage_cost",
    "net_pnl",
    "return_pct",
    "net_return_pct",
    "entry_order_type",
    "exit_order_type",
    "exit_reason",
    "is_open",
]


def _exit_reason(order_type: str, tag: str) -> str:
    if tag.startswith("bracket_tp"):
        return "take_profit"
    if tag.startswith("bracket_sl"):
        return "stop_loss"
    if order_type == "stop_trail":
        return "trailing_stop"
    if order_type == "stop_trail_limit":
        return "trailing_stop_limit"
    if order_type == "stop_limit":
        return "stop_limit"
    if order_type == "stop":
        return "stop"
    if order_type == "limit":
        return "limit"
    return "market"


def extract_trades(
    fills: pd.DataFrame,
    *,
    strategy_id: str,
    index: pd.DatetimeIndex,
    last_prices: dict[str, float],
) -> pd.DataFrame:
    """Turn fills into round-trip trades, marking anything still open at the end."""
    if fills.empty:
        return pd.DataFrame(columns=TRADE_COLUMNS)

    bar_position = {timestamp: i for i, timestamp in enumerate(index)}
    open_lots: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []

    ordered = fills.sort_values("timestamp", kind="stable")
    for row in ordered.itertuples(index=False):
        symbol = row.symbol
        side = row.side
        quantity = float(row.quantity)
        price = float(row.price)
        fees = float(row.commission)
        slippage = float(row.slippage_cost)
        timestamp = row.timestamp

        if side == "buy":
            lot = open_lots.get(symbol)
            if lot is None or lot["quantity"] <= 0:
                open_lots[symbol] = {
                    "quantity": quantity,
                    "entry_price": price,
                    "entry_time": timestamp,
                    "entry_fees": fees,
                    "entry_slippage": slippage,
                    "entry_order_type": row.order_type,
                    "entry_bar": bar_position.get(timestamp, np.nan),
                }
            else:
                total = lot["quantity"] + quantity
                lot["entry_price"] = (lot["entry_price"] * lot["quantity"] + price * quantity) / total
                lot["entry_fees"] += fees
                lot["entry_slippage"] += slippage
                lot["quantity"] = total
            continue

        lot = open_lots.get(symbol)
        if lot is None or lot["quantity"] <= 0:
            # A sell with no open lot would be a short; the equity engine never
            # creates one, so record nothing rather than inventing a trade.
            continue

        closed = min(quantity, lot["quantity"])
        share = closed / lot["quantity"]
        entry_fees = lot["entry_fees"] * share
        entry_slippage = lot["entry_slippage"] * share
        gross = (price - lot["entry_price"]) * closed
        # Slippage is already embedded in the fill prices, so it is reported for
        # cost attribution but never subtracted a second time.
        net = gross - entry_fees - fees

        entry_bar = lot["entry_bar"]
        exit_bar = bar_position.get(timestamp, np.nan)
        bars_held = (
            float(exit_bar - entry_bar)
            if pd.notna(entry_bar) and pd.notna(exit_bar)
            else np.nan
        )

        trades.append(
            {
                "strategy_id": strategy_id,
                "symbol": symbol,
                "direction": "long",
                "entry_time": lot["entry_time"],
                "exit_time": timestamp,
                "bars_held": bars_held,
                "entry_price": lot["entry_price"],
                "exit_price": price,
                "quantity": closed,
                "gross_pnl": gross,
                "fees": entry_fees + fees,
                "slippage_cost": entry_slippage + slippage,
                "net_pnl": net,
                "return_pct": gross / (lot["entry_price"] * closed) if lot["entry_price"] > 0 else np.nan,
                "net_return_pct": net / (lot["entry_price"] * closed)
                if lot["entry_price"] > 0
                else np.nan,
                "entry_order_type": lot["entry_order_type"],
                "exit_order_type": row.order_type,
                "exit_reason": _exit_reason(row.order_type, row.tag),
                "is_open": False,
            }
        )

        lot["quantity"] -= closed
        lot["entry_fees"] -= entry_fees
        lot["entry_slippage"] -= entry_slippage
        if lot["quantity"] <= 1e-9:
            open_lots.pop(symbol, None)

    # Positions never closed inside the sample: mark to the last observed close.
    for symbol, lot in open_lots.items():
        mark = last_prices.get(symbol)
        if mark is None or not np.isfinite(mark):
            continue
        gross = (mark - lot["entry_price"]) * lot["quantity"]
        net = gross - lot["entry_fees"]
        trades.append(
            {
                "strategy_id": strategy_id,
                "symbol": symbol,
                "direction": "long",
                "entry_time": lot["entry_time"],
                "exit_time": index[-1] if len(index) else lot["entry_time"],
                "bars_held": (
                    float(len(index) - 1 - lot["entry_bar"])
                    if pd.notna(lot["entry_bar"])
                    else np.nan
                ),
                "entry_price": lot["entry_price"],
                "exit_price": float(mark),
                "quantity": lot["quantity"],
                "gross_pnl": gross,
                "fees": lot["entry_fees"],
                "slippage_cost": lot["entry_slippage"],
                "net_pnl": net,
                "return_pct": gross / (lot["entry_price"] * lot["quantity"])
                if lot["entry_price"] > 0
                else np.nan,
                "net_return_pct": net / (lot["entry_price"] * lot["quantity"])
                if lot["entry_price"] > 0
                else np.nan,
                "entry_order_type": lot["entry_order_type"],
                "exit_order_type": "",
                "exit_reason": "open_at_end",
                "is_open": True,
            }
        )

    frame = pd.DataFrame(trades, columns=TRADE_COLUMNS)
    if not frame.empty:
        frame = frame.sort_values("entry_time", kind="stable").reset_index(drop=True)
    return frame


def closed_trades(trades: pd.DataFrame) -> pd.DataFrame:
    """Trades with a realised exit; the base for win rate and profit factor."""
    if trades.empty:
        return trades
    return trades[~trades["is_open"]].copy()
