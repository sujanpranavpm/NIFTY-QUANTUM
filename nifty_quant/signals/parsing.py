"""Parse the pending orders and positions captured at the end of a run into a
human-readable signal.

The parser is intentionally mechanical: it looks at what the strategy actually
submitted, not at what a human thinks the strategy should do. This preserves
the original logic and keeps the signal engine honest.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from nifty_quant.engine.backtest import PORTFOLIO_LABEL
from nifty_quant.engine.orders import OrderSide, OrderType
from nifty_quant.signals.schemas import Signal, SignalAction

ENTRY_TYPES = {"market", "limit", "stop", "stop_limit", "bracket"}
STOP_TYPES = {"stop", "stop_limit", "stop_trail", "stop_trail_limit"}
EXIT_ORDER_TYPES = {"stop", "stop_limit", "stop_trail", "stop_trail_limit", "limit", "market"}


SignalInfo = dict[str, Any]


def parse_slice(
    strategy_id: str,
    run_slice,
    approval: dict[str, Any],
    *,
    direction: str = "long_only",
    fingerprint: str = "",
    params: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    reference_prices: dict[str, float] | None = None,
    as_of: pd.Timestamp | None = None,
    earliest_fill_at: pd.Timestamp | None = None,
) -> list[Signal]:
    """Produce one signal per symbol represented in this slice.

    For a per-stock run the slice label is the symbol. For a portfolio run the
    slice label is ``PORTFOLIO_LABEL`` and symbols are taken from the pending
    orders and positions.
    """
    if not approval.get("approved", False):
        return _blocked_for_slice(
            strategy_id,
            run_slice,
            approval,
            as_of=as_of,
            fingerprint=fingerprint,
            params=params,
            history=history,
            reference_prices=reference_prices,
        )

    pending = _clean_frame(getattr(run_slice, "pending", None))
    positions = getattr(run_slice, "positions", {}) or {}
    if run_slice.label != PORTFOLIO_LABEL:
        symbols = [run_slice.label]
    else:
        symbols = sorted(
            set(pending["symbol"].astype(str).unique()) | set(positions.keys())
        )

    signals: list[Signal] = []
    for symbol in symbols:
        symbol_pending = pending[pending["symbol"].astype(str) == symbol] if not pending.empty else pending
        position = float(positions.get(symbol, 0.0) or 0.0)
        reference_price = reference_prices.get(symbol) if reference_prices else None
        signals.append(
            _parse_symbol(
                strategy_id=strategy_id,
                symbol=symbol,
                pending=symbol_pending,
                position=position,
                direction=direction,
                approval=approval,
                fingerprint=fingerprint,
                params=params or {},
                history=history or {},
                reference_price=reference_price,
                as_of=as_of,
                earliest_fill_at=earliest_fill_at,
            )
        )
    return signals


def _parse_symbol(
    strategy_id: str,
    symbol: str,
    pending: pd.DataFrame,
    position: float,
    direction: str,
    approval: dict[str, Any],
    *,
    fingerprint: str,
    params: dict[str, Any],
    history: dict[str, Any],
    reference_price: float | None,
    as_of: pd.Timestamp | None,
    earliest_fill_at: pd.Timestamp | None,
) -> Signal:
    raw = pending.to_dict("records") if not pending.empty else []

    # Distinguish entry orders (no OCO pair) from exit children (OCO pair).
    entries = [o for o in raw if not o.get("oco_pair_id")]
    oco_orders = [o for o in raw if o.get("oco_pair_id")]
    oco_pairs = _pair_oco(oco_orders)

    # Long/short classification from the strategy spec.
    long_only = direction == "long_only"

    action, action_note, entry_order, exit_orders = _classify_action(
        position=position,
        entries=entries,
        oco_pairs=oco_pairs,
        long_only=long_only,
    )

    entry_price, order_type = _entry_price(entry_order, reference_price)
    stop_loss, target, rr = _stop_target(entry_price, exit_orders, position)
    exit_conditions = [_describe_exit(o) for o in exit_orders]

    return Signal(
        strategy_id=strategy_id,
        symbol=symbol,
        action=action,
        as_of=as_of,
        generated_at=pd.Timestamp.now(),
        signal_at=as_of,
        earliest_fill_at=earliest_fill_at,
        reference_price=reference_price,
        entry_price=entry_price,
        stop_loss=stop_loss,
        target=target,
        order_type=order_type,
        exit_conditions=exit_conditions,
        risk_reward=rr,
        valid=True,
        validity_reason=f"approved: {approval.get('reason', '')}" + (f"; note: {action_note}" if action_note else ""),
        verdict=str(approval.get("verdict", "untested")),
        checks=dict(approval.get("checks", {})),
        history=history,
        fingerprint=fingerprint,
        params=params,
        position=position,
        raw_orders=raw,
    )


def _blocked_for_slice(
    strategy_id: str,
    run_slice,
    approval: dict[str, Any],
    *,
    as_of: pd.Timestamp | None,
    fingerprint: str,
    params: dict[str, Any] | None,
    history: dict[str, Any] | None,
    reference_prices: dict[str, float] | None,
) -> list[Signal]:
    """One BLOCKED signal per symbol the slice touched."""
    pending = _clean_frame(getattr(run_slice, "pending", None))
    positions = getattr(run_slice, "positions", {}) or {}
    if run_slice.label != PORTFOLIO_LABEL:
        symbols = [run_slice.label]
    else:
        symbols = sorted(
            set(pending["symbol"].astype(str).unique()) | set(positions.keys())
        )
    reason = approval.get("reason", "validation did not approve this strategy")
    signals: list[Signal] = []
    for symbol in symbols:
        reference_price = reference_prices.get(symbol) if reference_prices else None
        signals.append(
            Signal(
                strategy_id=strategy_id,
                symbol=symbol,
                action=SignalAction.BLOCKED,
                as_of=as_of,
                generated_at=pd.Timestamp.now(),
                signal_at=as_of,
                earliest_fill_at=earliest_fill_at,
                reference_price=reference_price,
                valid=False,
                validity_reason=reason,
                verdict=str(approval.get("verdict", "untested")),
                checks=dict(approval.get("checks", {})),
                history=history or {},
                fingerprint=fingerprint,
                params=params or {},
                position=float(positions.get(symbol, 0.0) or 0.0),
            )
        )
    return signals


def _classify_action(
    position: float,
    entries: list[dict[str, Any]],
    oco_pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None]],
    long_only: bool,
) -> tuple[SignalAction, str, dict[str, Any] | None, list[dict[str, Any]]]:
    """Return (action, note, chosen_entry_order, list_of_exit_orders)."""
    buy_entries = [o for o in entries if o.get("side") == "buy"]
    sell_entries = [o for o in entries if o.get("side") == "sell"]

    # Flat: only bracket children (OCO) and no position.
    if abs(position) < 1e-9:
        if buy_entries and sell_entries:
            return SignalAction.FLAT, "conflicting entry sides without a position", None, []
        if buy_entries:
            return SignalAction.BUY, "", buy_entries[0], _flatten_exits(oco_pairs)
        if sell_entries:
            if long_only:
                # In a long-only strategy a pending sell without a position is
                # almost certainly a bracket child; report flat.
                return SignalAction.FLAT, "pending sell is a bracket child, no entry", None, _flatten_exits(oco_pairs)
            return SignalAction.SELL, "", sell_entries[0], _flatten_exits(oco_pairs)
        return SignalAction.FLAT, "", None, []

    # Long position.
    if position > 0:
        exits = [o for o in entries if o.get("side") == "sell"] + _flatten_exits(oco_pairs)
        if exits:
            return SignalAction.EXIT, "", None, exits
        if buy_entries:
            # Pyramiding / adding to a winner: not expected in the upstream
            # examples, so report the intent honestly.
            return SignalAction.BUY, "adding to existing long", buy_entries[0], []
        return SignalAction.HOLD, "", None, []

    # Short position.
    exits = [o for o in entries if o.get("side") == "buy"] + _flatten_exits(oco_pairs)
    if exits:
        return SignalAction.EXIT, "", None, exits
    if sell_entries and not long_only:
        return SignalAction.SELL, "adding to existing short", sell_entries[0], []
    return SignalAction.HOLD, "", None, []


def _entry_price(entry: dict[str, Any] | None, reference_price: float | None) -> tuple[float | None, str]:
    if entry is None:
        return reference_price, ""
    otype = str(entry.get("order_type", "")).lower()
    limit = _to_float(entry.get("limit_price"))
    stop = _to_float(entry.get("stop_price"))
    if otype == "limit" and limit is not None:
        return limit, "limit"
    if otype == "stop" and stop is not None:
        return stop, "stop"
    if otype == "stop_limit":
        # The trigger is the actionable level; limit is the worst accepted fill.
        return (stop if stop is not None else limit), "stop_limit"
    if otype == "bracket":
        # Bracket parents decompose to market/limit entry.
        return (limit if limit is not None else reference_price), "bracket"
    if otype == "market":
        return reference_price, "market"
    return reference_price, otype


def _order_price(order: dict[str, Any]) -> float | None:
    """The actionable price of an order, chosen by its type.

    A stop/stop-limit child carries its level in ``stop_price`` and leaves
    ``limit_price`` empty; a take-profit limit does the opposite.  Selecting by
    type rather than ``stop_price or limit_price`` matters because an empty
    numeric cell arrives here as ``NaN``, which is *truthy* - the naive ``or``
    therefore returned the NaN and silently dropped every bracket take-profit
    (and, for a stop child, dropped the stop level from the written description).
    """
    otype = str(order.get("order_type", "")).lower()
    key = "stop_price" if otype in STOP_TYPES else "limit_price"
    return _to_float(order.get(key))


def _stop_target(
    entry_price: float | None,
    exit_orders: list[dict[str, Any]],
    position: float,
) -> tuple[float | None, float | None, float | None]:
    stop_loss: float | None = None
    target: float | None = None
    for o in exit_orders:
        otype = str(o.get("order_type", "")).lower()
        price = _order_price(o)
        if price is None:
            continue
        if otype in STOP_TYPES:
            stop_loss = price
        elif otype == "limit":
            target = price

    if entry_price is None or not math.isfinite(entry_price):
        return stop_loss, target, None
    rr = None
    if position > 0:
        if target is not None and stop_loss is not None and target != stop_loss:
            rr = (target - entry_price) / (entry_price - stop_loss)
    elif position < 0:
        if target is not None and stop_loss is not None and target != stop_loss:
            rr = (entry_price - target) / (stop_loss - entry_price)
    else:
        # Flat entry: reward/risk can still be estimated from the planned levels.
        if target is not None and stop_loss is not None and target != stop_loss:
            rr = abs(target - entry_price) / abs(entry_price - stop_loss)
    if rr is not None and (not math.isfinite(rr) or rr <= 0):
        rr = None
    return stop_loss, target, rr


def _pair_oco(oco_orders: list[dict[str, Any]]) -> list[tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    """Pair OCO orders as (target, stop) where possible."""
    if not oco_orders:
        return []
    by_id: dict[str, list[dict[str, Any]]] = {}
    for o in oco_orders:
        key = str(o.get("oco_pair_id"))
        by_id.setdefault(key, []).append(o)
    pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []
    for group in by_id.values():
        stops = [o for o in group if str(o.get("order_type", "")).lower() in STOP_TYPES]
        limits = [o for o in group if str(o.get("order_type", "")).lower() == "limit"]
        target = limits[0] if limits else None
        stop = stops[0] if stops else None
        pairs.append((target, stop))
    return pairs


def _flatten_exits(oco_pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for target, stop in oco_pairs:
        if target is not None:
            out.append(target)
        if stop is not None:
            out.append(stop)
    return out


def _describe_exit(order: dict[str, Any]) -> str:
    otype = str(order.get("order_type", "")).lower()
    side = str(order.get("side", "")).lower()
    price = _order_price(order)
    price_str = f"@{price:.2f}" if price is not None else ""
    if otype in STOP_TYPES:
        return f"{side} {otype}{price_str}"
    if otype == "limit":
        return f"{side} target{price_str}"
    if otype == "market":
        return f"{side} market"
    return f"{side} {otype}{price_str}"


def _clean_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(
            columns=[
                "order_id",
                "symbol",
                "side",
                "order_type",
                "limit_price",
                "stop_price",
                "oco_pair_id",
                "quantity",
            ]
        )
    return frame.copy()


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num
