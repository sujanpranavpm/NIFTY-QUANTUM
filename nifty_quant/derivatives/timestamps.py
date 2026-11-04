"""Candle timestamp convention and strict session classification.

The question the provider never answered
----------------------------------------
Every bar carries one timestamp and the dataset card says only that it is "tz-aware
IST" - it never states whether a bar stamped 09:15 covers the minute *starting* or the
minute *ending* at 09:15.  Some file-days end at 15:29, others at 15:30, and a few run
past the close, so the ambiguity is visible in the data itself.

How it was settled (``logs/m4_terminal_candle.txt``, 68 date x expiry blocks)
---------------------------------------------------------------------------
Under the close-labelled hypothesis the 09:15 bucket could only hold a single opening
print and the 15:30 bucket would be an ordinary full minute.  Measured on the dominant
signature (39 blocks with exactly the 376 labels 09:15..15:30):

* opening bucket volume = 5.34x a typical minute, degenerate in 0% of contracts;
* terminal 15:30 bucket volume = 0.038x a typical minute, degenerate in 83% of them;
* the 15:30 bucket's close equals the official ``LastPric`` for 100% of matched
  contracts (median over 48 blocks).

That is the opposite of the close-labelled expectation, so:

**A bar stamped T covers the half-open interval [T, T+1) - timestamps are candle
opening times.  The 15:30 bar is not a minute: it is the post-open closing/settlement
print bucket, which is why it is tiny, usually single-priced, and always carries the
official close.**

Consequences encoded here: aggregation must label a bucket by its left edge and fold
the 15:30 print into the bar that ends the session, and blocks whose labels fall
outside 09:15..15:30 (12 of 68 blocks, stray labels up to 15:54, all in Jan-Mar 2025
and on 2025-08-01) cannot be given a verified convention and are rejected by strict
validation rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import pandas as pd

#: Resolved convention: the timestamp is the OPEN of the interval, not the close.
CONVENTION = "open_labelled"
CONVENTION_EVIDENCE = "logs/m4_terminal_candle.txt (68 blocks, measured 2026-11-04)"

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
#: A bar stamped T covers [T, T + BAR_MINUTES).
BAR_MINUTES = 1

# Verdict labels.
STRICT_OK = "STRICT_OK"
NO_CLOSING_PRINT = "NO_CLOSING_PRINT"
AMBIGUOUS_OUT_OF_SESSION = "AMBIGUOUS_OUT_OF_SESSION"
AMBIGUOUS_TERMINAL_BUCKET = "AMBIGUOUS_TERMINAL_BUCKET"
AMBIGUOUS_OPENING_BUCKET = "AMBIGUOUS_OPENING_BUCKET"
AMBIGUOUS_NO_TRUSTED_FIRST_BUCKET = "AMBIGUOUS_NO_TRUSTED_FIRST_BUCKET"

STRICT_VERDICTS = (STRICT_OK,)
#: A contract with fewer traded minutes than this has no meaningful "typical minute".
MIN_CONTRACT_MINUTES = 20
#: Aggregated volume of the 09:15 bucket must be at least this multiple of a typical
#: minute for the block to look open-labelled (the opening auction lands inside it).
OPENING_MIN_SHARE = 1.0
#: Volume of a 15:30 bucket above this multiple of a typical minute means it holds a
#: whole minute of trade, which is the close-labelled prediction, not this convention.
CLOSE_PRINT_MAX_SHARE = 0.5


@dataclass(frozen=True)
class SessionVerdict:
    """What one (trading day x expiry file) block says about its own timestamps."""

    trading_day: str
    expiry: str
    verdict: str
    n_labels: int
    first_label: str
    last_label: str
    stray_labels: tuple[str, ...] = ()
    opening_share: float | None = None
    terminal_share: float | None = None
    terminal_bucket_close_pct: float | None = None
    reasons: tuple[str, ...] = field(default=())

    @property
    def strict_usable(self) -> bool:
        return self.verdict in STRICT_VERDICTS


def bar_interval(stamp, minutes: int = BAR_MINUTES) -> tuple[pd.Timestamp, pd.Timestamp]:
    """The half-open interval a bar represents: [stamp, stamp + minutes)."""
    start = pd.Timestamp(stamp)
    if start.tz is not None:
        start = start.tz_convert("Asia/Kolkata")
    return start, start + timedelta(minutes=minutes)


def contains_trade(stamp, trade_time, minutes: int = BAR_MINUTES) -> bool:
    """Is a trade at ``trade_time`` inside the bar stamped ``stamp``?  (left edge)

    Mixed awareness is resolved in the session's own zone rather than by comparing a
    wall clock against an instant: the session is IST in both sources, so a naive value
    is read as IST and an aware one is converted to IST.
    """
    start, end = bar_interval(stamp, minutes)
    ts = pd.Timestamp(trade_time)
    if ts.tz is None and start.tz is not None:
        ts = ts.tz_localize(start.tz)
    elif ts.tz is not None and start.tz is None:
        ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
    elif ts.tz is not None:
        ts = ts.tz_convert(start.tz)
    return bool(start <= ts < end)


def session_close_stamp(day) -> pd.Timestamp:
    """The exact instant the derivative session ends, as a tz-aware IST timestamp."""
    d = day if isinstance(day, date) else pd.Timestamp(str(day)).date()
    naive = datetime.combine(d, SESSION_CLOSE)
    return pd.Timestamp(naive).tz_localize("Asia/Kolkata")


def clock_label(value, fmt: str = "%H:%M") -> str:
    """Wall-clock label of a possibly tz-aware timestamp, always read in IST."""
    ts = pd.Timestamp(value)
    if ts.tz is not None:
        ts = ts.tz_convert("Asia/Kolkata")
    return ts.strftime(fmt)


def clock_labels(stamps, fmt: str = "%H:%M") -> pd.Series:
    """Vectorised :func:`clock_label` over a timestamp column."""
    parsed = pd.to_datetime(pd.Series(stamps))
    if getattr(parsed.dt, "tz", None) is not None:
        parsed = parsed.dt.tz_convert("Asia/Kolkata")
    return pd.Series(parsed.dt.strftime(fmt), index=parsed.index)


def label_set(frame: pd.DataFrame, stamp_column: str = "timestamp") -> tuple[str, ...]:
    return tuple(sorted({clock_label(v) for v in frame[stamp_column]}))


def in_session(label: str) -> bool:
    return SESSION_OPEN.strftime("%H:%M") <= label <= SESSION_CLOSE.strftime("%H:%M")


def minute_volume_profile(frame: pd.DataFrame, *, stamp_column: str = "timestamp",
                          volume_column: str = "volume") -> pd.Series:
    """Total traded volume in the block for every minute label.

    Aggregated over contracts on purpose: a single illiquid strike trades one lot per
    minute, so its per-contract ratios cannot separate a closing print from a full
    minute, while the block total can.
    """
    d = frame[[stamp_column, volume_column]].copy()
    d["clock"] = d[stamp_column].map(clock_label)
    return d.groupby("clock", sort=True)[volume_column].sum()


def classify_session(frame: pd.DataFrame, *, trading_day: str = "", expiry: str = "",
                     stamp_column: str = "timestamp",
                     volume_column: str = "volume",
                     ohlc_columns=("open", "high", "low", "close")) -> SessionVerdict:
    """Apply the resolved convention to one block and report whether it can be trusted.

    ``frame`` must be one trading day of one expiry file and carry ``strike`` and
    ``option_type`` alongside the price columns.  Two independent shapes are tested so
    a block is accepted only when it matches the open-labelled prediction and not the
    close-labelled one:

    * the 09:15 bucket must carry at least a typical minute's volume (under the
      close-labelled reading it would be a lone opening print);
    * a 15:30 bucket, when present, must carry far less than a typical minute (under
      the close-labelled reading it would be a full minute).

    Anything else is returned with the reason spelled out so the caller can exclude it
    instead of guessing.
    """
    labels = label_set(frame, stamp_column)
    first, last = labels[0], labels[-1]
    strays = tuple(x for x in labels if not in_session(x))
    reasons: list[str] = []
    if {"strike", "option_type"} - set(frame.columns):
        raise ValueError("classify_session needs strike and option_type columns to "
                         "measure the terminal bucket shape")

    close_label = SESSION_CLOSE.strftime("%H:%M")
    open_label = SESSION_OPEN.strftime("%H:%M")
    profile = minute_volume_profile(frame, stamp_column=stamp_column,
                                   volume_column=volume_column)
    interior = [x for x in profile.index if open_label < x < close_label]
    typical = float(profile[interior].median()) if interior else float("nan")
    opening_share = float(profile[first]) / typical if typical and typical > 0 else None
    terminal_share = float(profile[last]) / typical if typical and typical > 0 else None

    # How often the terminal bucket is a single price, per contract: the fingerprint of
    # a point print rather than a minute of negotiation.
    cols = [stamp_column, volume_column, *ohlc_columns, "strike", "option_type"]
    d = frame[cols].copy()
    d["clock"] = d[stamp_column].map(clock_label)
    keys = ["strike", "option_type"]
    minutes = d.groupby(keys, observed=True)["clock"].transform("nunique")
    eligible = d[minutes >= MIN_CONTRACT_MINUTES]
    close_pct: float | None = None
    if len(eligible):
        last_rows = eligible.sort_values("clock").groupby(keys, observed=True).tail(1)
        single = (last_rows["open"].eq(last_rows["high"]) & last_rows["high"].eq(last_rows["low"])
                  & last_rows["low"].eq(last_rows["close"]))
        close_pct = 100.0 * float(single.mean())

    if strays:
        reasons.append(f"{len(strays)} labels outside {open_label}..{close_label}: "
                       f"{list(strays)[:6]}; no convention can be verified for them")
        verdict = AMBIGUOUS_OUT_OF_SESSION
    elif opening_share is None:
        reasons.append("too few interior minutes to establish a typical minute")
        verdict = AMBIGUOUS_NO_TRUSTED_FIRST_BUCKET
    elif opening_share < OPENING_MIN_SHARE:
        reasons.append(f"09:15 bucket carries only {opening_share:.3f} of a typical "
                       f"minute, which is the close-labelled prediction, not this one")
        verdict = AMBIGUOUS_OPENING_BUCKET
    elif last == close_label and terminal_share is not None \
            and terminal_share > CLOSE_PRINT_MAX_SHARE:
        reasons.append(f"15:30 bucket carries {terminal_share:.2f} of a typical minute, "
                       f"so it holds a whole minute rather than the closing print")
        verdict = AMBIGUOUS_TERMINAL_BUCKET
    elif last == close_label:
        verdict = STRICT_OK
    elif first == open_label:
        # Intraday buckets are still interpretable under the left-edge convention, but
        # the block carries no closing print, hence no verified session close.
        reasons.append(f"session ends at {last}: no closing print bucket, so the "
                       f"official close is not represented in this block")
        verdict = NO_CLOSING_PRINT
    else:
        reasons.append(f"first label {first} is not the {open_label} session open")
        verdict = AMBIGUOUS_NO_TRUSTED_FIRST_BUCKET

    return SessionVerdict(trading_day=trading_day, expiry=expiry, verdict=verdict,
                          n_labels=len(labels), first_label=first, last_label=last,
                          stray_labels=strays, opening_share=opening_share,
                          terminal_share=terminal_share,
                          terminal_bucket_close_pct=close_pct, reasons=tuple(reasons))


def first_last_bucket(frame: pd.DataFrame, stamp_column: str = "timestamp",
                      volume_column: str = "volume") -> dict:
    """Volume of the opening and terminal buckets relative to a typical minute."""
    d = frame.copy()
    d["clock"] = d[stamp_column].map(clock_label)
    out: dict[str, float] = {}
    firsts, lasts, mids = [], [], []
    for _, grp in d.groupby(["strike", "option_type"], observed=True):
        grp = grp.sort_values("clock")
        if grp["clock"].nunique() < MIN_CONTRACT_MINUTES:
            continue
        med = float(grp[volume_column].median())
        if med <= 0:
            continue
        firsts.append(float(grp.iloc[0][volume_column]) / med)
        lasts.append(float(grp.iloc[-1][volume_column]) / med)
        meds = grp[volume_column]
        mids.append(float(meds.iloc[1:-1].median()) / med if len(meds) > 2 else 1.0)
    if firsts:
        out["first_bucket_ratio_median"] = round(float(pd.Series(firsts).median()), 3)
        out["last_bucket_ratio_median"] = round(float(pd.Series(lasts).median()), 3)
        out["n_contracts"] = len(firsts)
    return out
