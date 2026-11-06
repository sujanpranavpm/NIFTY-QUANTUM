"""P2.2.5 - Initial strategy screening of the five Tier-1 candidates.

Runs the *unchanged, audited* options-buying adapter on the expanded 265-expiry
dataset and evaluates each candidate strictly against the pre-registered
methodology in ``P2.2_BACKTESTING_METHODOLOGY.md``:

* chronological periods (Development -> 2024-06-30, Validation -> 2025-06-30,
  Protected Holdout = the reserved tail), attributed by **entry date**;
* the §3 trade-count floors and exit-reason diversity;
* §4 metrics on the risked-premium basis (win rate, profit factor, expectancy
  in rupees and per unit premium, drawdown on the cumulative net-P&L curve);
* §5 scenario spread (OPTIMISTIC / BASELINE / CONSERVATIVE) and the
  Development->Validation sign-stability qualification rule;
* §6 statistics reusing ``nifty_quant.validation.stats`` (bootstrap CIs, PSR,
  deflated Sharpe, Benjamini-Hochberg across the five candidates, sign test);
* §2 walk-forward folds reusing ``validation.walkforward`` (``generate_folds`` +
  ``window_metrics``) over Development + Validation only;
* §9 baselines: no-trade, naive-ATM hold-to-expiry (same selection, exits only at
  expiry), index buy-and-hold and the cost-only gross-vs-net gap;
* §10 decision rule (advance / insufficient evidence / fails screening).

Nothing here optimises a parameter, modifies a strategy or touches the holdout to
set a threshold: the adapter is called with the published ``AdapterConfig`` and
all thresholds are fixed constants quoted from the methodology.  15m is primary,
5m is a sensitivity pass only.

    $env:PYTHONPATH='.'; python scripts/_p225_screen.py
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from nifty_quant.config import BASELINE, CONSERVATIVE, OPTIMISTIC, ResearchConfig
from nifty_quant.analytics.metrics import max_drawdown
from nifty_quant.engine.options_costs import IndiaOptionsCommission
from nifty_quant.engine.options_execution import ExitRules, OptionsExecutionEngine
from nifty_quant.options.bars import build_option_bars
from nifty_quant.options.data import MarketData
from nifty_quant.options.pipeline import TIER1, AdapterConfig, ExitConvention, OptionTradeAdapter
from nifty_quant.options.reference import OptionReference
from nifty_quant.options.selection import select_contract
from nifty_quant.validation import (
    benjamini_hochberg,
    binomial_sign_test,
    bootstrap_mean_ci,
    generate_folds,
    probabilistic_sharpe_ratio,
    window_metrics,
)
from nifty_quant.validation.report import MIN_DSR, MIN_PSR
from nifty_quant.validation.stats import bootstrap_sharpe_ci, deflated_sharpe_ratio
from nifty_quant.validation.readiness import holdout_protected

# ---- pre-registered constants (from P2.2_BACKTESTING_METHODOLOGY.md) --------
DEV_END = pd.Timestamp("2024-06-30")   # §1 Development -> 2024-06-30
VAL_END = pd.Timestamp("2025-06-30")   # §1 Validation  -> 2025-06-30 ; tail = holdout
MIN_DEV_TRADES = 30                     # §3 Development trade floor
MIN_FOLD_TRADES = 5                      # §3 per-OOS-fold trade floor
MIN_EXIT_REASONS = 2                     # §3 exit-reason diversity
FDR_ALPHA = ResearchConfig().validation.fdr_alpha   # §6 BH alpha (0.10)
N_TRIALS = 5 * 3 * 2                    # §6 candidates x scenarios x resolutions (pre-registered)
INITIAL_CAPITAL = 100_000.0
PAINLY = "statutory-only, brokerage-excluded"
ARTIFACTS = Path("artifacts") / "p225"
LOG_TXT = Path("logs") / "p225_screen.txt"
LOG_JSON = Path("logs") / "p225_screen.json"

# P2.2.6.2 pin: P2.2.5 screened under the then-default +/-30%/30% band.  The
# library default is now the approved +15%/-8% EXIT_POLICY, so the historical band
# is stated explicitly to keep this script's as-run configuration reproducible.
# The +/-30% results are superseded (P2.2.5.1 audit); do not re-run for decisions.
HISTORICAL_EXIT = ExitConvention(stop_fraction=0.30, target_fraction=0.30)

SCENARIOS = (OPTIMISTIC.name, BASELINE.name, CONSERVATIVE.name)


# =========================================================================
# helpers
# =========================================================================
def period_of(entry_date: str) -> str:
    d = pd.Timestamp(entry_date)
    if d <= DEV_END:
        return "Development"
    if d <= VAL_END:
        return "Validation"
    return "Holdout"


def pnl_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Trades that contribute P&L: exclude UNRESOLVED / NO_ENTRY (§4)."""
    return df[df["status"].isin(["FILLED", "OPEN_AT_END"])].copy()


def ret_on_premium(df: pd.DataFrame) -> pd.Series:
    outlay = df["entry_premium"] * df["lot_size"] * df["lots"]
    return df["net_pnl"] / outlay.replace(0, np.nan)


def trade_block(df: pd.DataFrame) -> dict:
    """§4 metric set over one candidate x scenario (one row per trade)."""
    closed = pnl_rows(df)
    n = int(len(closed))
    if n == 0:
        return {"n_trades": 0, "unresolved": int((df["status"] == "UNRESOLVED").sum()),
                "open_at_end": int((df["status"] == "OPEN_AT_END").sum()),
                "win_rate": np.nan, "profit_factor": np.nan, "expectancy_net": np.nan,
                "expectancy_ret": np.nan, "max_drawdown_pct": np.nan, "total_net_pnl": 0.0}
    pnl = closed["net_pnl"].astype(float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    rets = ret_on_premium(closed)
    curve = (INITIAL_CAPITAL + pnl.reset_index(drop=True).cumsum())
    idx = pd.DatetimeIndex(pd.to_datetime(closed["exit_date"])).sort_values()
    mdd = max_drawdown(pd.Series(curve.to_numpy(), index=idx)) if len(curve) > 1 else np.nan
    return {
        "n_trades": n,
        "unresolved": int((df["status"] == "UNRESOLVED").sum()),
        "open_at_end": int((df["status"] == "OPEN_AT_END").sum()),
        "win_rate": float(len(wins) / n),
        "profit_factor": float(wins.sum() / abs(losses.sum())) if losses.sum() < 0 else np.nan,
        "expectancy_net": float(pnl.mean()),
        "expectancy_ret": float(rets.mean()),
        "max_drawdown_pct": float(mdd),
        "total_net_pnl": float(pnl.sum()),
    }


def equity_trades_bridge(df: pd.DataFrame) -> tuple[pd.Series, pd.DataFrame]:
    """Bridge option trades onto the shape ``window_metrics`` consumes (§2 reuse)."""
    closed = pnl_rows(df).sort_values("exit_date")
    pnl = closed["net_pnl"].astype(float)
    eq = pd.Series(INITIAL_CAPITAL + pnl.cumsum().to_numpy(),
                   index=pd.DatetimeIndex(pd.to_datetime(closed["exit_date"])), name="equity")
    bridge = pd.DataFrame({
        "entry_time": pd.DatetimeIndex(pd.to_datetime(closed["entry_date"])),
        "fees": closed["total_costs"].astype(float).to_numpy(),
        "slippage_cost": 0.0,
        "is_open": False,
        "net_pnl": pnl.to_numpy(),
        "bars_held": closed["bars_held"].astype(float).to_numpy(),
    })
    return eq, bridge


# =========================================================================
# walk-forward (reuses validation.walkforward over Development + Validation)
# =========================================================================
def walkforward_for_candidate(df_scen: pd.DataFrame, sessions_index: pd.DatetimeIndex) -> dict:
    # §2 walk-forward is run over Development + Validation ONLY - the holdout tail
    # (entry > VAL_END) is never folded, so fold geometry cannot peek at it.
    dv_sessions = pd.DatetimeIndex(sessions_index[sessions_index <= VAL_END])
    folds = generate_folds(dv_sessions, train_bars=125, test_bars=42,
                           step_bars=42, min_test_bars=5)
    eq, bridge = equity_trades_bridge(df_scen)
    rows, reliable_oos, positive_oos = [], 0, 0
    for fold in folds:
        is_m = window_metrics(eq, bridge, fold.train_start, fold.train_end)
        oos_m = window_metrics(eq, bridge, fold.test_start, fold.test_end)
        oos_n = int(oos_m.get("n_trades", 0)) if oos_m else 0
        is_ok = (is_m or {}).get("n_trades", 0) >= MIN_DEV_TRADES // 3
        if oos_m and oos_n >= MIN_FOLD_TRADES:
            reliable_oos += 1
            if oos_m.get("expectancy", 0.0) > 0:
                positive_oos += 1
        rows.append({
            "fold": fold.fold,
            "train_start": str(fold.train_start.date()), "test_start": str(fold.test_start.date()),
            "test_end": str(fold.test_end.date()),
            "is_n": int((is_m or {}).get("n_trades", 0)), "is_expectancy": (is_m or {}).get("expectancy"),
            "oos_n": oos_n, "oos_expectancy": (oos_m or {}).get("expectancy"),
            "oos_reliable": bool(oos_n >= MIN_FOLD_TRADES),
            "is_train_ok": bool(is_ok),
        })
    return {
        "folds": len(folds),
        "reliable_oos_folds": reliable_oos,
        "positive_oos_fold_share": (positive_oos / reliable_oos) if reliable_oos else np.nan,
        "table": rows,
    }


# =========================================================================
# naive-ATM hold-to-expiry benchmark (§9.3): same selection, exits only at expiry
# =========================================================================
def naive_atm(resolution: str = "15m") -> pd.DataFrame:
    market = MarketData()
    ref = OptionReference(sessions=market.sessions())
    engine = OptionsExecutionEngine(IndiaOptionsCommission())
    cfg = AdapterConfig(resolution=resolution)
    daily = market.index_daily()
    closes = daily["close"]
    expiries = market.option_expiries(resolution)
    hold = ExitRules()          # no stop, no target, no max-hold -> hold to expiry
    rows = []
    for day_ts in daily.index:
        D = day_ts.date()
        entry_day = ref.next_session(D)
        if entry_day is None:
            continue
        spot = float(closes.loc[day_ts])
        sel = select_contract(entry_day, spot, "CE", ref=ref, market=market,
                              observed_expiries=expiries, min_dte=cfg.min_dte,
                              max_strike_steps=cfg.max_strike_steps, resolution=resolution)
        if not sel.ok:
            continue
        frame = market.option_frame(sel.expiry, resolution)
        series = build_option_bars(frame, strike=sel.strike, option_type=sel.option_type,
                                   entry_day=entry_day, expiry=sel.expiry)
        if not series.ok:
            continue
        for sc in cfg.scenarios_for(sel.lot_size, cfg.lots):
            tr = engine.run(series.bars, series.signal_index, hold, lots=cfg.lots,
                            lot_size=sel.lot_size, scenario=sc)
            rows.append({
                "strategy_id": "naive_atm_hold", "scenario": sc.name, "status": tr.status,
                "exit_reason": tr.reason, "entry_date": str(entry_day),
                "exit_date": (str(pd.Timestamp(tr.exit_timestamp).date())
                              if tr.exit_timestamp is not None else None),
                "entry_premium": tr.entry_premium, "lot_size": tr.lot_size, "lots": tr.lots,
                "dte_at_entry": sel.dte_at_entry, "net_pnl": tr.net_pnl,
                "gross_pnl": tr.gross_pnl, "total_costs": tr.total_costs,
            })
    return pd.DataFrame(rows)


# =========================================================================
# per-candidate evaluation
# =========================================================================
def evaluate_candidate(sid: str, trades: pd.DataFrame, sessions_index: pd.DatetimeIndex) -> dict:
    scdf = trades[trades["strategy_id"] == sid].copy()
    scdf["period"] = scdf["entry_date"].map(period_of)
    out: dict[str, object] = {"strategy_id": sid}

    # unique-entry counts (collapse scenarios) per period + floor check
    uniq = scdf.drop_duplicates(subset=["scenario", "signal_date", "expiry", "strike"])
    counts = (uniq[uniq["scenario"] == BASELINE.name]["period"].value_counts().to_dict())
    dev_n = int(counts.get("Development", 0))
    val_n = int(counts.get("Validation", 0))
    hold_n = int(counts.get("Holdout", 0))
    out["unique_entries"] = {"Development": dev_n, "Validation": val_n,
                             "Holdout": hold_n, "Dev+Val": dev_n + val_n}
    out["floor_dev_trades"] = {"value": dev_n, "floor": MIN_DEV_TRADES,
                               "met": bool(dev_n >= MIN_DEV_TRADES)}
    reasons = sorted(set(pnl_rows(scdf[scdf["scenario"] == BASELINE.name])["exit_reason"].dropna()))
    out["exit_reason_diversity"] = {"reasons": reasons,
                                    "met": bool(len(reasons) >= MIN_EXIT_REASONS)}

    # metric tables by scenario x period + Dev+Val scoring block
    per_scen = {}
    for scen in SCENARIOS:
        sub = scdf[scdf["scenario"] == scen]
        per_scen[scen] = {
            "all": trade_block(sub),
            "Development": trade_block(sub[sub["period"] == "Development"]),
            "Validation": trade_block(sub[sub["period"] == "Validation"]),
            "Holdout": trade_block(sub[sub["period"] == "Holdout"]),
            "score_dev_val": trade_block(sub[sub["period"].isin(["Development", "Validation"])]),
        }
    out["by_scenario"] = per_scen

    # BASELINE Dev+Val is the scoring basis (§5 qualification rests on BASELINE)
    base_dv = pnl_rows(scdf[(scdf["scenario"] == BASELINE.name)
                            & (scdf["period"].isin(["Development", "Validation"]))])
    net = base_dv["net_pnl"].astype(float).tolist()
    rets = ret_on_premium(base_dv).replace([np.inf, -np.inf], np.nan).dropna().tolist()
    pos = int((base_dv["net_pnl"] > 0).sum())
    neg = int((base_dv["net_pnl"] < 0).sum())
    boot_net = bootstrap_mean_ci(net) if len(net) >= 2 else {}
    boot_ret = bootstrap_sharpe_ci(rets) if len(rets) >= 10 else {}
    out["stats_baseline_dev_val"] = {
        "bootstrap_net_ci": boot_net,
        "bootstrap_sharpe_ci": boot_ret,
        "psr": probabilistic_sharpe_ratio(rets) if len(rets) > 2 else np.nan,
        "sign_test_p": binomial_sign_test(pos, neg),
        "positive": pos, "negative": neg,
    }

    # CONSERVATIVE sign stability Development -> Validation (§5)
    cons = scdf[scdf["scenario"] == CONSERVATIVE.name]
    dev_e = trade_block(cons[cons["period"] == "Development"])["expectancy_net"]
    val_e = trade_block(cons[cons["period"] == "Validation"])["expectancy_net"]
    sign_stable = (not (np.isnan(dev_e) or np.isnan(val_e))) and bool(
        (dev_e >= 0) == (val_e >= 0)) or (np.isnan(dev_e) or np.isnan(val_e))
    out["conservative_sign_stability"] = {
        "dev_expectancy": dev_e, "val_expectancy": val_e,
        "flipped": bool((not np.isnan(dev_e)) and (not np.isnan(val_e))
                        and (dev_e >= 0) != (val_e >= 0)),
    }

    # walk-forward (Dev+Val only, holdout never touched)
    out["walkforward"] = walkforward_for_candidate(
        scdf[scdf["scenario"] == BASELINE.name], sessions_index)
    return out


def decide(ev: dict, beats_no_trade: bool, beats_naive: bool, dsr: float,
           bh_q: float) -> str:
    """§10 decision rule: advance / insufficient evidence / fails screening."""
    floor_met = ev["floor_dev_trades"]["met"]
    base = ev["by_scenario"][BASELINE.name]["score_dev_val"]
    breakeven = (base["expectancy_net"] is not np.nan) and base["expectancy_net"] >= 0
    sign_stable = not ev["conservative_sign_stability"]["flipped"]
    under_powered = (ev["unique_entries"]["Dev+Val"] < MIN_DEV_TRADES) or (
        ev["stats_baseline_dev_val"]["sign_test_p"] is not None
        and not np.isnan(ev["stats_baseline_dev_val"]["sign_test_p"])
        and ev["stats_baseline_dev_val"]["sign_test_p"] > FDR_ALPHA)
    survives_stats = (dsr >= MIN_DSR) and (bh_q < FDR_ALPHA)

    if not floor_met:
        return "Insufficient evidence"
    if floor_met and (not breakeven or not sign_stable):
        return "Fails screening"
    if breakeven and sign_stable and survives_stats and beats_no_trade and beats_naive:
        return "Advances to deeper research"
    if breakeven and sign_stable and not survives_stats:
        return "Insufficient evidence"
    return "Fails screening"


# =========================================================================
# main
# =========================================================================
def main() -> None:
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 60)
    lines: list[str] = []
    say = lambda *a: (print(*a), lines.append(" ".join(str(x) for x in a)))

    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    # ---- 15m primary (persisted as the record of truth) + 5m sensitivity ----
    say("== Running unchanged adapter: 15m primary ==")
    primary = OptionTradeAdapter(
        AdapterConfig(resolution="15m", exit=HISTORICAL_EXIT)).run(
        persist=True, out_dir=ARTIFACTS / "15m")
    trades = primary.trades_frame
    rej = primary.rejections_frame
    trades["period"] = trades["entry_date"].map(period_of)

    say("== Running unchanged adapter: 5m sensitivity ==")
    sens = OptionTradeAdapter(
        AdapterConfig(resolution="5m", exit=HISTORICAL_EXIT)).run(
        persist=True, out_dir=ARTIFACTS / "5m")
    trades5 = sens.trades_frame
    trades5["period"] = trades5["entry_date"].map(period_of)

    # holdout-protection invariant on the fixed boundaries (no leakage by date)
    sessions_index = pd.DatetimeIndex([pd.Timestamp(s) for s in
                                       OptionReference(sessions=MarketData().sessions()).sessions])
    dev_end, val_end = DEV_END, VAL_END
    protected = bool(dev_end <= val_end < sessions_index[-1])
    say(f"\nEligible (15m): {primary.eligible}")
    say(f"Period boundaries fixed a priori: dev_end={dev_end.date()} val_end={val_end.date()} "
        f"tail_start>{val_end.date()} holdout_reserved={protected}")
    say(f"Trades 15m={len(trades)} rejections={len(rej)} | "
        f"5m trades={len(trades5)} rejections={len(sens.rejections_frame)}")

    # ---- baselines ----
    say("\n== Baselines ==")
    atm = naive_atm("15m")
    atm["period"] = atm["entry_date"].map(period_of) if len(atm) else atm
    base_scen = BASELINE.name
    atm_base = atm[atm["scenario"] == base_scen] if len(atm) else pd.DataFrame()
    atm_dv = trade_block(atm_base[atm_base["period"].isin(["Development", "Validation"])]) \
        if len(atm_base) else trade_block(pd.DataFrame())
    # index buy-and-hold over the joint-eligible calendar (descriptive)
    daily = MarketData().index_daily()
    elig_days = sessions_index[(sessions_index >= pd.Timestamp("2021-05-24"))
                               & (sessions_index <= VAL_END)]
    idx_bh = {"start": str(elig_days[0].date()), "end": str(elig_days[-1].date()),
              "total_return": float(daily.loc[elig_days, "close"].iloc[-1]
                                    / daily.loc[elig_days, "close"].iloc[0] - 1.0),
              "n_days": int(len(elig_days))}
    # cost-only gap (BASELINE, whole sample): gross vs net
    b_all = trades[trades["scenario"] == base_scen]
    cost_only = {"gross": float(b_all["gross_pnl"].sum()), "net": float(b_all["net_pnl"].sum()),
                 "friction": float(b_all["total_costs"].sum())}
    say(f"no-trade baseline: net_pnl = 0 (explicit bar)")
    say(f"naive-ATM hold-to-expiry (15m, BASELINE, Dev+Val): {atm_dv}")
    say(f"index buy-and-hold Dev+Val: {idx_bh}")
    say(f"cost-only friction (all candidates, BASELINE): {cost_only}")

    # ---- per-candidate evaluation ----
    say("\n== Per-candidate screening (BASELINE scoring, all scenarios reported) ==")
    evals = {sid: evaluate_candidate(sid, trades, sessions_index) for sid in TIER1}

    # cross-candidate multiple-testing: BH over sign-test p-values; DSR sr_std
    pvals = []
    per_trade_sharpe = []
    for sid in TIER1:
        p = evals[sid]["stats_baseline_dev_val"]["sign_test_p"]
        pvals.append(p if p is not None and np.isfinite(p) else 1.0)
        b = evals[sid]["by_scenario"][BASELINE.name]["score_dev_val"]
        sd = _std_ret(trades, sid)
        per_trade_sharpe.append(
            b["expectancy_ret"] / sd if (sd and np.isfinite(sd) and sd > 1e-12
                                         and np.isfinite(b["expectancy_ret"])) else np.nan)
    sr_std = float(np.nanstd(per_trade_sharpe, ddof=1)) if len(per_trade_sharpe) > 1 else float("nan")
    qvals = benjamini_hochberg(pvals)
    bh_map = dict(zip(TIER1, qvals))

    decisions = {}
    for sid, ev in evals.items():
        rets = ret_on_premium(pnl_rows(
            trades[(trades["strategy_id"] == sid) & (trades["scenario"] == base_scen)
                   & (trades["period"].isin(["Development", "Validation"]))])
        ).replace([np.inf, -np.inf], np.nan).dropna().tolist()
        dsr = deflated_sharpe_ratio(rets, n_trials=N_TRIALS, sr_std=sr_std) \
            if len(rets) > 2 else np.nan
        base_dv = ev["by_scenario"][BASELINE.name]["score_dev_val"]
        beats_no_trade = base_dv["expectancy_net"] is not None and base_dv["expectancy_net"] > 0 \
            and not np.isnan(base_dv["expectancy_net"])
        beats_naive = (not np.isnan(base_dv["expectancy_ret"])) and (
            base_dv["expectancy_ret"] > (atm_dv["expectancy_ret"] or np.nan))
        decisions[sid] = decide(ev, beats_no_trade, beats_naive, dsr, bh_map[sid])
        ev["dsr"] = dsr
        ev["bh_qvalue"] = float(bh_map[sid])
        ev["beats_no_trade"] = bool(beats_no_trade)
        ev["beats_naive_atm"] = bool(beats_naive)
        ev["decision"] = decisions[sid]
        say(f"\n--- {sid} ---")
        say(f"  unique entries {ev['unique_entries']}  dev_floor_met={ev['floor_dev_trades']['met']}"
            f"  exit_reasons={ev['exit_reason_diversity']['reasons']}"
            f"  (diversity_met={ev['exit_reason_diversity']['met']})")
        say(f"  BASELINE Dev+Val: {base_dv}")
        say(f"  scenario spread (Dev+Val expectancy_net): "
            + ", ".join(f"{s}={ev['by_scenario'][s]['score_dev_val']['expectancy_net']:.1f}"
                        for s in SCENARIOS))
        say(f"  conservative sign Dev={ev['conservative_sign_stability']['dev_expectancy']}, "
            f"Val={ev['conservative_sign_stability']['val_expectancy']}, "
            f"flipped={ev['conservative_sign_stability']['flipped']}")
        say(f"  sign-test p={ev['stats_baseline_dev_val']['sign_test_p']}, BH q={bh_map[sid]:.3f}, "
            f"DSR={dsr if dsr is np.nan else round(dsr,3)}, PSR={ev['stats_baseline_dev_val']['psr']}")
        say(f"  walk-forward reliable_oos_folds={ev['walkforward']['reliable_oos_folds']}/"
            f"{ev['walkforward']['folds']}, positive_share={ev['walkforward']['positive_oos_fold_share']}")
        say(f"  beats no-trade={ev['beats_no_trade']}, beats naive-ATM={ev['beats_naive_atm']}")
        say(f"  DECISION: {decisions[sid]}")

    # ---- 5m sensitivity summary (BASELINE, Dev+Val) ----
    say("\n== 5m sensitivity (BASELINE, Dev+Val expectancy_net) ==")
    sens5 = {}
    for sid in TIER1:
        sub = trades5[(trades5["strategy_id"] == sid) & (trades5["scenario"] == base_scen)
                      & (trades5["period"].isin(["Development", "Validation"]))]
        sens5[sid] = trade_block(sub)
        say(f"  {sid}: {sens5[sid]}")

    # ---- persist frames ----
    persist_frames(evals, trades, trades5, atm, idx_bh, cost_only, decisions, sr_std)

    payload = build_payload(primary, sens, evals, atm_dv, idx_bh, cost_only,
                            decisions, sr_std, N_TRIALS)
    LOG_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    LOG_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(f"\nWrote {LOG_TXT.name}, {LOG_JSON.name}, artifacts under {ARTIFACTS}")


def _std_ret(trades: pd.DataFrame, sid: str) -> float:
    r = ret_on_premium(pnl_rows(trades[(trades["strategy_id"] == sid)
                                       & (trades["scenario"] == BASELINE.name)
                                       & (trades["period"].isin(["Development", "Validation"]))])
    ).replace([np.inf, -np.inf], np.nan).dropna()
    return float(r.std(ddof=1)) if len(r) > 1 else float("nan")


def persist_frames(evals, trades, trades5, atm, idx_bh, cost_only, decisions, sr_std) -> None:
    # scenario x period metric table (one row per candidate x scenario x period block)
    rows = []
    for sid, ev in evals.items():
        for scen, blocks in ev["by_scenario"].items():
            for period, m in blocks.items():
                rows.append({"strategy_id": sid, "scenario": scen, "period_block": period, **m})
    pd.DataFrame(rows).to_csv(ARTIFACTS / "scenario_metrics.csv", index=False)

    dec_rows = []
    for sid, ev in evals.items():
        dec_rows.append({
            "strategy_id": sid, "decision": decisions[sid],
            "dev_unique": ev["unique_entries"]["Development"],
            "val_unique": ev["unique_entries"]["Validation"],
            "holdout_unique": ev["unique_entries"]["Holdout"],
            "dev_floor_met": ev["floor_dev_trades"]["met"],
            "exit_reason_diversity_met": ev["exit_reason_diversity"]["met"],
            "baseline_devval_expectancy_net":
                ev["by_scenario"][BASELINE.name]["score_dev_val"]["expectancy_net"],
            "baseline_devval_profit_factor":
                ev["by_scenario"][BASELINE.name]["score_dev_val"]["profit_factor"],
            "baseline_devval_win_rate":
                ev["by_scenario"][BASELINE.name]["score_dev_val"]["win_rate"],
            "conservative_flipped": ev["conservative_sign_stability"]["flipped"],
            "sign_test_p": ev["stats_baseline_dev_val"]["sign_test_p"],
            "bh_qvalue": ev["bh_qvalue"], "dsr": ev["dsr"],
            "psr": ev["stats_baseline_dev_val"]["psr"],
            "wf_reliable_folds": ev["walkforward"]["reliable_oos_folds"],
            "wf_positive_share": ev["walkforward"]["positive_oos_fold_share"],
            "beats_no_trade": ev["beats_no_trade"], "beats_naive_atm": ev["beats_naive_atm"],
        })
    pd.DataFrame(dec_rows).to_csv(ARTIFACTS / "decisions.csv", index=False)

    pd.DataFrame([{**{"strategy_id": s, "resolution": "15m", **ev["by_scenario"][BASELINE.name]},
                   **{"dsr": ev["dsr"], "bh_q": ev["bh_qvalue"], "decision": decisions[s]}}
                  for s, ev in evals.items()]).drop(
        columns=["score_dev_val", "Development", "Validation", "Holdout", "all"],
        errors="ignore").to_csv(ARTIFACTS / "per_strategy_results.csv", index=False)

    if len(atm):
        atm.to_csv(ARTIFACTS / "naive_atm_baseline.csv", index=False)
    pd.DataFrame([idx_bh]).to_csv(ARTIFACTS / "index_buyhold_baseline.csv", index=False)


def build_payload(primary, sens, evals, atm_dv, idx_bh, cost_only, decisions, sr_std, n_trials) -> dict:
    def compact(ev):
        b = ev["by_scenario"][BASELINE.name]
        return {
            "unique_entries": ev["unique_entries"],
            "dev_floor": ev["floor_dev_trades"],
            "exit_reason_diversity": ev["exit_reason_diversity"],
            "baseline_by_period": {k: v for k, v in b.items()},
            "conservative_devval": ev["by_scenario"][CONSERVATIVE.name]["score_dev_val"],
            "optimistic_devval": ev["by_scenario"][OPTIMISTIC.name]["score_dev_val"],
            "stats": ev["stats_baseline_dev_val"],
            "dsr": ev["dsr"], "bh_qvalue": ev["bh_qvalue"],
            "walkforward": {k: v for k, v in ev["walkforward"].items() if k != "table"},
            "beats_no_trade": ev["beats_no_trade"], "beats_naive_atm": ev["beats_naive_atm"],
            "decision": ev["decision"],
        }
    return {
        "config": {"resolution_primary": "15m", "sensitivity": "5m",
                   "min_dte_at_entry": 2, "scenarios": list(SCENARIOS),
                   "n_trials": n_trials, "fdr_alpha": FDR_ALPHA,
                   "min_psr": MIN_PSR, "min_dsr": MIN_DSR,
                   "cost_basis_label": PAINLY,
                   "sr_std_per_period": sr_std},
        "period_boundaries": {"dev_end": str(DEV_END.date()), "val_end": str(VAL_END.date())},
        "eligible_15m": primary.eligible,
        "counts_15m": {"trades": len(primary.trades_frame),
                       "rejections": len(primary.rejections_frame),
                       "by_status": primary.trades_frame["status"].value_counts().to_dict()},
        "eligible_5m": sens.eligible,
        "counts_5m": {"trades": len(sens.trades_frame),
                      "rejections": len(sens.rejections_frame)},
        "baselines": {"no_trade": 0.0, "naive_atm_hold_devval_baseline": atm_dv,
                      "index_buyhold": idx_bh, "cost_only": cost_only},
        "candidates": {sid: compact(evals[sid]) for sid in evals},
        "decisions": decisions,
    }


if __name__ == "__main__":
    main()
