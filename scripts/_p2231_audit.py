"""P2.2.3.1 final independent audit of the options-buying adapter.

Every figure below is recomputed *independently of the adapter's own helpers*
where possible (the DTE ladder is rebuilt from raw parquet, the cost labels are
re-derived through the commission, the signals count is re-tallied), then compared
to what the adapter reports.  A mismatch is printed as a BLOCKER, not smoothed over.

    $env:PYTHONPATH='.'; python scripts/_p2231_audit.py
"""

from __future__ import annotations

import bisect
import glob
import hashlib
import json
import os
from datetime import date

import pandas as pd

from nifty_quant.engine.options_costs import IndiaOptionsCommission
from nifty_quant.engine.orders import OrderSide
from nifty_quant.options.data import MarketData
from nifty_quant.options.pipeline import TIER1, AdapterConfig, ExitConvention, OptionTradeAdapter
from nifty_quant.options.signals import index_daily_signals

# P2.2.6.2 pin: this audit ran against the then-default +/-30%/30% band; the
# library default is now the approved +15%/-8% policy.  The historical band is
# stated explicitly so this audit stays byte-reproducible.  Do not "update" it.
HISTORICAL_EXIT = ExitConvention(stop_fraction=0.30, target_fraction=0.30)

CANDLES = "data/derived/candles"
INDEX = "data/raw/p1_sample/index__NIFTY.parquet"


def _sessions() -> list[date]:
    idx = pd.read_parquet(INDEX, columns=["trading_day"])
    return sorted({date.fromisoformat(str(d)[:10]) for d in idx["trading_day"].unique()})


def _usable_15m() -> dict[date, pd.DataFrame]:
    out: dict[date, pd.DataFrame] = {}
    for path in glob.glob(os.path.join(CANDLES, "15m__NIFTY__*.parquet")):
        exp = date.fromisoformat(os.path.basename(path).split("__")[-1][:10])
        try:  # guard EACH read: a stub must not abort the sweep
            df = pd.read_parquet(path, columns=["trading_day", "strike", "option_type", "volume"])
        except Exception:  # noqa: BLE001
            continue
        out[exp] = df
    return out


def _dte(sessions: list[date], day: date, expiry: date) -> int:
    lo = bisect.bisect_right(sessions, day)
    hi = bisect.bisect_right(sessions, expiry)
    return max(0, hi - lo)


def independent_dte_ladder():
    sessions = _sessions()
    files = _usable_15m()
    session_set = set(sessions)

    days_by_expiry: dict[date, set[date]] = {}
    opt_days: set[date] = set()
    for exp, df in files.items():
        d = {date.fromisoformat(str(x)[:10]) for x in df["trading_day"].unique()}
        days_by_expiry[exp] = d
        opt_days |= d
    overlap = sorted(d for d in opt_days if d in session_set)

    def max_dte(day: date) -> int:
        return max((_dte(sessions, day, e) for e, ds in days_by_expiry.items() if day in ds),
                   default=-1)

    ladder = {f"dte_ge_{t}": sum(1 for d in overlap if max_dte(d) >= t) for t in (0, 1, 2, 3)}
    eligible = [d for d in overlap if max_dte(d) >= 2
                and sessions[bisect.bisect_left(sessions, d) - 1] in session_set
                and bisect.bisect_left(sessions, d) > 0]
    return {
        "index_sessions": len(sessions),
        "usable_expiry_files": len(files),
        "distinct_option_days": len(opt_days),
        "overlap_option_days": len(overlap),
        "dte_ladder_overlap_days": ladder,
        "eligible_entry_days_min_dte2_with_prior_session": len(eligible),
    }, sessions


def _hash(df: pd.DataFrame) -> str:
    norm = df.sort_values(list(df.columns)).reset_index(drop=True)
    return hashlib.sha256(pd.util.hash_pandas_object(norm, index=True).values.tobytes()).hexdigest()


def main() -> None:
    findings: dict[str, object] = {}

    # ---- Check 3: independent DTE ladder, reconciled to the adapter ----
    ladder, sessions = independent_dte_ladder()
    market = MarketData()
    adapter = OptionTradeAdapter(AdapterConfig(exit=HISTORICAL_EXIT))
    adapter_stats = adapter.eligible_entry_stats()
    findings["check3_independent"] = ladder
    findings["check3_adapter"] = adapter_stats
    findings["check3_agree"] = {
        "index_sessions": ladder["index_sessions"] == adapter_stats["index_sessions"],
        "distinct_option_days": ladder["distinct_option_days"] == adapter_stats["distinct_option_days"],
        "overlap": ladder["overlap_option_days"] == adapter_stats["overlap_option_days"],
        "ladder": ladder["dte_ladder_overlap_days"] == adapter_stats["dte_ladder_overlap_days"],
        "eligible_121": (ladder["eligible_entry_days_min_dte2_with_prior_session"]
                          == adapter_stats["eligible_entry_days"]),
    }

    # ---- Checks 5/6/8: two full runs (also feeds Check 7 determinism) ----
    r1 = adapter.run()
    r2 = OptionTradeAdapter(AdapterConfig(exit=HISTORICAL_EXIT)).run()
    trades, rej = r1.trades_frame, r1.rejections_frame

    # Check 7 determinism: run-to-run identical hashes
    findings["check7_determinism"] = {
        "trades_hash_r1": _hash(trades), "trades_hash_r2": _hash(r2.trades_frame),
        "rejections_hash_r1": _hash(rej), "rejections_hash_r2": _hash(r2.rejections_frame),
        "trades_identical": _hash(trades) == _hash(r2.trades_frame),
        "rejections_identical": _hash(rej) == _hash(r2.rejections_frame),
    }

    # Check 5: reconcile scenario rows -> unique entries, per strategy & scenario
    uniq = trades.groupby(["strategy_id", "signal_date", "expiry", "strike"]).size()
    findings["check5"] = {
        "total_trade_rows": int(len(trades)),
        "unique_entries": int(len(uniq)),
        "rows_per_entry": sorted(uniq.unique().tolist()),
        "scenarios": sorted(trades["scenario"].unique().tolist()),
        "per_strategy_x_scenario": pd.crosstab(trades["strategy_id"], trades["scenario"]).to_dict(),
        "by_status": trades["status"].value_counts().to_dict(),
        "dte_at_entry_values": sorted(trades["dte_at_entry"].unique().tolist()),
        "min_dte_ok": bool((trades["dte_at_entry"] >= 2).all()),
    }

    # Check 6: rejections by strategy x reason + duplicate-count test
    per_strategy_signals = {sid: len(index_daily_signals(sid, market.index_daily()))
                            for sid in TIER1}
    recon = []
    for sid in TIER1:
        t_rows = int((trades["strategy_id"] == sid).sum())
        r_rows = int((rej["strategy_id"] == sid).sum())
        sig = per_strategy_signals[sid]
        # each signal: success -> 3 trade rows; failure -> 1 rejection row
        implied = (t_rows / 3) + r_rows
        recon.append({"strategy": sid, "signals": sig, "trade_rows": t_rows,
                      "unique_entries": round(t_rows / 3), "rejections": r_rows,
                      "entries+rejections==signals": implied == sig})
    findings["check6"] = {
        "total_rejections": int(len(rej)),
        "by_reason": rej["reason"].value_counts().to_dict(),
        "by_strategy_x_reason": pd.crosstab(rej["strategy_id"], rej["reason"]).to_dict(),
        "duplicate_signal_rejections": int(
            rej.duplicated(subset=["strategy_id", "signal_date", "reason"]).sum()),
        "per_strategy_reconciliation": recon,
    }

    # Check 4: liquidity floor arithmetic + exit-only evidence
    cfg = AdapterConfig()
    lot_examples = sorted(int(ls) for ls in trades["lot_size"].unique())
    floors: dict[int, dict[str, float]] = {}
    for ls in lot_examples:
        sc = cfg.scenarios_for(lot_size=ls, lots=1)
        floors[ls] = {s.name: float(s.min_exit_volume) for s in sc}
    conservative_floors_are_lot_x_lots = all(
        f["C_CONSERVATIVE"] == ls * 1 and f["A_OPTIMISTIC"] == 0.0 and f["B_BASELINE"] == 0.0
        for ls, f in floors.items())
    findings["check4"] = {
        "floors_by_lot_size_share_equivalents": floors,
        "conservative_equals_lot_size_x_lots": conservative_floors_are_lot_x_lots,
        "note": "engine gate is EXIT-only and fail-closed (below floor the exit is refused "
                "and the position stays open -> OPEN_AT_END/UNRESOLVED); it never blocks "
                "entry, so a FILLED row does not attest that entry was liquid.",
    }

    # Check 8: cost labels re-derived through the commission, compared to stored
    comm = IndiaOptionsCommission()
    mismatches, all_be, all_ai, stat_true = [], True, False, 0
    for row in trades.itertuples(index=False):
        if row.exit_date and row.exit_premium is not None:
            rt = comm.round_trip(row.entry_premium, row.exit_premium, int(row.lot_size),
                                 int(row.lots), row.entry_date, row.exit_date)
            exp_stat, exp_ai, exp_be = (rt["statutory_verified"], rt["all_in_verified"],
                                        rt["brokerage_excluded"])
        else:
            leg = comm.assess(row.entry_premium, int(row.lot_size), int(row.lots),
                             OrderSide.SELL, row.entry_date)
            exp_stat, exp_ai, exp_be = (leg.statutory_verified, leg.all_in_verified,
                                        leg.brokerage_excluded)
        if (exp_stat != row.statutory_verified or exp_ai != row.all_in_verified
                or exp_be != row.brokerage_excluded):
            mismatches.append({"row": f"{row.strategy_id}/{row.signal_date}",
                               "exp": (exp_stat, exp_ai, exp_be),
                               "stored": (row.statutory_verified, row.all_in_verified,
                                          row.brokerage_excluded)})
        all_be = all_be and bool(row.brokerage_excluded)
        all_ai = all_ai or bool(row.all_in_verified)
        stat_true += int(row.statutory_verified)
        # cost_basis honesty: a verified-but-brokerage-excluded row must warn "do not
        # label as net profit"; an unverified row must flag "estimated"; none may claim
        # an all-in net figure.  (Two legitimate label families, not one string.)
        basis = str(row.cost_basis)
        if row.statutory_verified and "do not label as net profit" not in basis:
            mismatches.append({"row": f"{row.strategy_id}/{row.signal_date}",
                               "missing_warning": basis})
        if (not row.statutory_verified) and "estimated" not in basis:
            mismatches.append({"row": f"{row.strategy_id}/{row.signal_date}",
                               "unverified_not_flagged": basis})
    findings["check8"] = {
        "recomputed_vs_stored_mismatches": mismatches,
        "brokerage_excluded_all_true": all_be,
        "all_in_verified_any_true": all_ai,
        "statutory_verified_true_count": stat_true,
        "total_trades": int(len(trades)),
    }

    print(json.dumps(findings, indent=2, default=str))


if __name__ == "__main__":
    main()
