"""Aggregating verified one-minute buckets into 5- and 15-minute candles.

This module only became writable once the timestamp convention was resolved
(``nifty_quant.derivatives.timestamps``): a bar stamped T covers [T, T+1), and the
15:30 bar is the session's closing print, not a minute.  Both facts change the answer,
so aggregating before the resolution would have produced systematically wrong closes.

Rules implemented:

* a coarser candle takes the open of its first constituent minute, the high/low across
  all constituents, the close of its last constituent, the summed volume and the
  open-interest observation carried by its last constituent minute;
* the 15:30 closing print is folded into the candle that ends the session (the one
  covering 15:25-15:30 for a 5-minute grid), so the session's final candle closes at
  the official close instead of one minute early;
* empty intervals are **not** filled.  A contract that did not trade has no candle, and
  inventing one from a previous close would fabricate both price and zero volume;
* sessions that fail strict classification are returned as rejections, never aggregated
  into the result, because their timestamps cannot be given a verified meaning.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from nifty_quant.derivatives.timestamps import (
    CONVENTION,
    SESSION_CLOSE,
    SessionVerdict,
    classify_session,
    clock_labels,
)

#: Minutes that carry trade data in a session: 09:15 .. 15:29 inclusive.
SESSION_MINUTE_BUCKETS = 375
#: Minute index of the closing print bucket (15:30), clamped into the last candle.
CLOSING_PRINT_INDEX = 375
LAST_BUCKET_INDEX = SESSION_MINUTE_BUCKETS - 1

CONTRACT_COLUMNS = ("symbol", "expiry", "strike", "option_type")


@dataclass(frozen=True)
class AggregationResult:
    """Candles plus the sessions that were refused, and why."""

    candles: pd.DataFrame
    rejected: pd.DataFrame

    @property
    def n_rejected(self) -> int:
        return len(self.rejected)


def minute_index(clock: str, session_open: str = "09:15") -> int:
    """Minutes elapsed since the session open for a ``HH:MM`` bucket label."""
    oh, om = (int(x) for x in session_open.split(":"))
    h, m = (int(x) for x in clock.split(":"))
    return (h * 60 + m) - (oh * 60 + om)


def candle_index(clock: str, minutes: int) -> int:
    """Which coarser candle a minute bucket belongs to.

    The 15:30 closing print is deliberately mapped onto the final in-session candle:
    it is a single print at the session close, and leaving it in its own bucket would
    make the last traded price of the day invisible to anything consuming candles.
    """
    if minutes < 1 or SESSION_MINUTE_BUCKETS % minutes:
        raise ValueError(f"{minutes}-minute candles do not tile the session evenly")
    index = minute_index(clock)
    if index == CLOSING_PRINT_INDEX:
        index = LAST_BUCKET_INDEX
    if index < 0 or index > LAST_BUCKET_INDEX:
        raise ValueError(f"clock label {clock} is outside the verified session")
    return index // minutes


def candle_label(index: int, minutes: int, session_open: str = "09:15") -> pd.Timestamp:
    """Left-edge timestamp of candle ``index``, in the session's own clock."""
    oh, om = (int(x) for x in session_open.split(":"))
    total = oh * 60 + om + index * minutes
    return pd.Timestamp(f"2000-01-01 {total // 60:02d}:{total % 60:02d}:00")


def aggregate_contract(grp: pd.DataFrame, minutes: int, *,
                       stamp_column: str = "timestamp",
                       volume_column: str = "volume",
                       oi_column: str = "open_interest") -> pd.DataFrame:
    """One contract-day of minute buckets -> coarse candles (left-edge labelled)."""
    d = grp.copy()
    d["clock"] = clock_labels(d[stamp_column])
    d = d.sort_values("clock")
    # candle_index is validated arithmetic; evaluate it once per distinct bucket label
    # (~376 of them) instead of once per row, so a full-archive aggregation is not
    # dominated by millions of Python calls while still refusing any out-of-session label.
    bucket_index = {c: candle_index(c, minutes) for c in sorted(d["clock"].unique())}
    d["candle"] = d["clock"].map(bucket_index)
    rows = []
    for candle, block in d.groupby("candle", sort=True):
        first, last = block.iloc[0], block.iloc[-1]
        # The 15:30 print is folded into the terminal candle's close, but its raw
        # value is preserved next to it so a consumer can see exactly which official
        # last-trade the close was taken from - never a silently invented number.
        prints = block[block["clock"] == SESSION_CLOSE.strftime("%H:%M")]
        rows.append({
            "candle_open": candle_label(int(candle), minutes),
            "open": float(first["open"]),
            "high": float(block["high"].max()),
            "low": float(block["low"].min()),
            "close": float(last["close"]),
            volume_column: float(block[volume_column].sum()),
            oi_column: float(last[oi_column]) if pd.notna(last[oi_column]) else float("nan"),
            "n_minute_buckets": int(block["clock"].nunique()),
            "last_minute_label": str(last["clock"]),
            "contains_closing_print": bool((block["clock"] ==
                                            SESSION_CLOSE.strftime("%H:%M")).any()),
            "oi_is_last_minute_observation": True,
            # raw-preserved closing print (NaN when this candle did not carry the print)
            "closing_print": (float(prints.iloc[-1]["close"]) if len(prints)
                              else float("nan")),
            "closing_print_volume": (float(prints[volume_column].sum()) if len(prints)
                                     else float("nan")),
        })
    return pd.DataFrame(rows)


def ohlc_containment_failures(candles: pd.DataFrame, *, tolerance: float = 1e-9) -> int:
    """Count candles whose high/low do not bracket their own open/close (or high<low).

    An aggregated candle's high is the maximum of its constituents and its low the
    minimum, so containment is a property that must hold by construction; a violation
    means raw data was internally inconsistent or a candle was invented.
    """
    if candles.empty:
        return 0
    bad = ((candles["high"] < candles["open"] - tolerance) |
           (candles["high"] < candles["close"] - tolerance) |
           (candles["low"] > candles["open"] + tolerance) |
           (candles["low"] > candles["close"] + tolerance) |
           (candles["high"] < candles["low"] - tolerance))
    return int(bad.sum())


def volume_conservation_delta(one_min: pd.DataFrame, candles: pd.DataFrame, *,
                              volume_column: str = "volume") -> float:
    """Absolute volume leaked or invented between the 1-minute grid and a coarse grid.

    A correct aggregation only ever re-buckets existing minute volume, so this must be
    zero (up to float error); a non-zero value means a candle was dropped, duplicated or
    filled.
    """
    if one_min.empty and candles.empty:
        return 0.0
    return float(abs(float(candles[volume_column].sum())
                     - float(one_min[volume_column].sum())))


def closing_print_candles(candles: pd.DataFrame) -> int:
    """How many candles carry the session closing print (one per aggregated contract-day)."""
    if candles.empty or "contains_closing_print" not in candles.columns:
        return 0
    return int(candles["contains_closing_print"].astype(bool).sum())


def aggregate(frame: pd.DataFrame, minutes: int = 5, *, strict: bool = True,
              stamp_column: str = "timestamp",
              day_column: str = "trading_day",
              contract_columns: tuple[str, ...] = CONTRACT_COLUMNS) -> AggregationResult:
    """Aggregate minute buckets to ``minutes``-candle bars, rejecting unverifiable days.

    With ``strict=True`` a (day x expiry) block is aggregated only when its own labels
    confirm the resolved convention.  Rejected blocks are returned with the verdict and
    the reason, so a missing day is a reported fact rather than a silent gap.
    """
    if CONVENTION != "open_labelled":
        raise RuntimeError("candle aggregation requires a resolved timestamp convention")
    keys = [*contract_columns, day_column]
    if set(keys) - set(frame.columns):
        raise ValueError(f"aggregate needs {keys} plus OHLCV columns")

    rejected_rows: list[dict] = []
    pieces: list[pd.DataFrame] = []
    for (day, expiry), block in frame.groupby([day_column, "expiry"], sort=True):
        verdict = classify_session(block, trading_day=str(day)[:10], expiry=str(expiry)[:10],
                                   stamp_column=stamp_column, volume_column="volume")
        if strict and not verdict.strict_usable:
            rejected_rows.append({"trading_day": str(day)[:10], "expiry": str(expiry)[:10],
                                  "verdict": verdict.verdict, "reason": "; ".join(verdict.reasons),
                                  "n_labels": verdict.n_labels,
                                  "first_label": verdict.first_label,
                                  "last_label": verdict.last_label})
            continue
        for contract_keys, grp in block.groupby(list(contract_columns), sort=False, observed=True):
            piece = aggregate_contract(grp, minutes, stamp_column=stamp_column)
            if piece.empty:
                continue
            for name, value in zip(contract_columns, contract_keys):
                piece[name] = value
            piece[day_column] = day
            piece["session_verdict"] = verdict.verdict
            pieces.append(piece)

    candles = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
    if not candles.empty:
        order = [day_column, *contract_columns, "candle_open"]
        candles = candles.sort_values(order).reset_index(drop=True)
    return AggregationResult(candles=candles, rejected=pd.DataFrame(rejected_rows))


def aggregate_from_1m(one_min: pd.DataFrame, minutes: int, *,
                      stamp_column: str = "timestamp",
                      day_column: str = "trading_day",
                      volume_column: str = "volume",
                      oi_column: str = "open_interest",
                      contract_columns: tuple[str, ...] = CONTRACT_COLUMNS
                      ) -> AggregationResult:
    """Vectorised 5m/15m grid built on an already-trusted 1m frame (no re-classification).

    ``minute_pass_through`` has already decided which (day x expiry) blocks are STRICT_OK,
    so this re-buckets every row in one groupby over
    ``(day, symbol, expiry, strike, option_type, candle)`` rather than looping a
    ``aggregate_contract`` call per contract.  The candle semantics, the closing-print
    fold and the preserved raw print are identical to :func:`aggregate` (an equivalence
    test pins the two together); only the strict re-classification - and the Python
    per-contract loop - are dropped, which is what makes a full-archive run tractable.
    """
    if CONVENTION != "open_labelled":
        raise RuntimeError("candle aggregation requires a resolved timestamp convention")
    if one_min.empty:
        return AggregationResult(candles=pd.DataFrame(), rejected=pd.DataFrame())
    d = one_min.copy()
    if "minute_label" in d.columns:
        clock_col = "minute_label"
    else:
        d["_clock"] = clock_labels(d[stamp_column])
        clock_col = "_clock"
    # validated once per distinct bucket; refuses any out-of-session label
    bucket = {c: candle_index(c, minutes) for c in sorted(d[clock_col].unique())}
    d["candle"] = d[clock_col].map(bucket)
    keys = [day_column, *contract_columns, "candle"]
    d = d.sort_values([*keys[:-1], clock_col, "candle"], kind="mergesort")
    g = d.groupby(keys, sort=True, observed=True)
    head, tail = g.head(1).set_index(keys), g.tail(1).set_index(keys)
    named = {volume_column: (volume_column, "sum"),
            "high": ("high", "max"), "low": ("low", "min"),
            "n_minute_buckets": (clock_col, "nunique")}
    if "session_verdict" in d.columns:
        named["session_verdict"] = ("session_verdict", "first")
    core = g.agg(**named)
    ends = pd.DataFrame({"open": head["open"], "close": tail["close"],
                         oi_column: tail[oi_column],
                         "last_minute_label": tail[clock_col]})
    out = core.join(ends)
    close_print = SESSION_CLOSE.strftime("%H:%M")
    cp = d[d[clock_col] == close_print]
    if len(cp):
        gcp = cp.groupby(keys, sort=True, observed=True)
        out["closing_print"] = gcp["close"].last().reindex(out.index)
        out["closing_print_volume"] = gcp[volume_column].sum().reindex(out.index)
        out["contains_closing_print"] = out.index.isin(gcp.groups.keys())
    else:
        out["closing_print"] = float("nan")
        out["closing_print_volume"] = float("nan")
        out["contains_closing_print"] = False
    out["oi_is_last_minute_observation"] = True
    out = out.reset_index()
    out["candle_open"] = out["candle"].map(
        {c: candle_label(int(c), minutes) for c in out["candle"].unique()})
    order = [day_column, *contract_columns, "candle_open"]
    out = out.sort_values(order).reset_index(drop=True)
    return AggregationResult(candles=out, rejected=pd.DataFrame())


def verdicts_by_block(frame: pd.DataFrame, *, stamp_column: str = "timestamp",
                      day_column: str = "trading_day") -> pd.DataFrame:
    """Classify every (day x expiry) block without aggregating - the audit view."""
    rows: list[SessionVerdict] = []
    for (day, expiry), block in frame.groupby([day_column, "expiry"], sort=True):
        rows.append(classify_session(block, trading_day=str(day)[:10],
                                     expiry=str(expiry)[:10], stamp_column=stamp_column))
    return pd.DataFrame([r.__dict__ for r in rows])


def minute_pass_through(frame: pd.DataFrame, *, strict: bool = True,
                        stamp_column: str = "timestamp",
                        day_column: str = "trading_day") -> AggregationResult:
    """The canonical 1-minute dataset: raw rows of every STRICT_OK block, verbatim.

    No provider value is recomputed or reordered in the OHLCV/OI columns; only three
    annotation columns are added - ``minute_label`` (the open-labelled clock bucket),
    ``is_closing_print`` (the 15:30 print kept as its own raw row, never folded away at
    this resolution) and ``session_verdict``.  Blocks that fail strict classification
    are excluded and returned in ``rejected`` with their reason, so a 5m/15m grid built
    on top conserves exactly this frame's volume.
    """
    if CONVENTION != "open_labelled":
        raise RuntimeError("candle aggregation requires a resolved timestamp convention")
    rejected_rows: list[dict] = []
    pieces: list[pd.DataFrame] = []
    for (day, expiry), block in frame.groupby([day_column, "expiry"], sort=True):
        verdict = classify_session(block, trading_day=str(day)[:10], expiry=str(expiry)[:10],
                                   stamp_column=stamp_column, volume_column="volume")
        if strict and not verdict.strict_usable:
            rejected_rows.append({"trading_day": str(day)[:10], "expiry": str(expiry)[:10],
                                  "verdict": verdict.verdict, "reason": "; ".join(verdict.reasons),
                                  "n_labels": verdict.n_labels,
                                  "first_label": verdict.first_label,
                                  "last_label": verdict.last_label})
            continue
        d = block.copy()
        d["minute_label"] = clock_labels(d[stamp_column])
        d["is_closing_print"] = d["minute_label"] == SESSION_CLOSE.strftime("%H:%M")
        d["session_verdict"] = verdict.verdict
        pieces.append(d)
    one_min = (pd.concat(pieces, ignore_index=True) if pieces
               else frame.iloc[0:0].copy())
    return AggregationResult(candles=one_min, rejected=pd.DataFrame(rejected_rows))
