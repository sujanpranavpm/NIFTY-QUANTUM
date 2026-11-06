"""P2.2.6 - corrected screening replay under the APPROVED exit convention.

The P2.2.5.1 integrity audit found that the exit band *run* in P2.2.5
(+/-30%/30%, uniform) is NOT the band *approved* by the methodology
(P2.2_OPTIONS_ADAPTATION_DESIGN.md and P2.2.2.1_METHODOLOGY_RECONCILIATION.md
sec 3.4: TP **+15% / SL -8%**, ratio-preserving -> ``orders_12`` +16%/-8%).

This script is a *controlled research replay*:

* the adapter is called with the same audited ``OptionTradeAdapter``, only the
  ``ExitConvention`` value inside ``AdapterConfig`` is replaced by the approved
  band (a supported config-level override - no library/strategy file is touched);
* everything else is held identical: signals (real engine), contract selection,
  entry sessions (D+1), MIN_DTE=2, one-lot sizing, P2.1.2 statutory costs, the
  three scenarios, 15m primary / 5m sensitivity;
* three exit series are compared per candidate on IDENTICAL entries/contracts:
    (A) approved premium TP/SL (+15/-8; orders_12 +16/-8),
    (B) signal-matched hold-to-expiry (only the exit rule changed),
    (C) the original unconditional all-days ATM hold reference, READ from the
        preserved P2.2.5 artifact - never recomputed or altered;
* the as-run +/-30% figures are read from the preserved P2.2.5 artifacts and
    reproduced verbatim for the deviation table;
* all floors/thresholds/statistics are the pre-registered ones, reused from
  ``scripts/_p225_screen.py`` helpers (``period_of``, ``trade_block``,
  ``evaluate_candidate``, ``decide``) - nothing is re-tuned on results and the
  previously accessed holdout is reported as SUPPLEMENTAL only (it was disclosed
  in P2.2.5 sec 9 and therefore cannot serve as pristine confirmation).

    $env:PYTHONPATH='.'; python scripts/_p226_exit_replay.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np
import pandas as pd

from nifty_quant.config import BASELINE, CONSERVATIVE, OPTIMISTIC
from nifty_quant.engine.options_costs import IndiaOptionsCommission
from nifty_quant.engine.options_execution import ExitRules, OptionsExecutionEngine
from nifty_quant.options.bars import build_option_bars
from nifty_quant.options.data import MarketData
from nifty_quant.options.pipeline import TIER1, AdapterConfig, ExitConvention, OptionTradeAdapter
from nifty_quant.options.reference import OptionReference
from nifty_quant.options.selection import select_contract
from nifty_quant.options.signals import index_daily_signals
from nifty_quant.validation import benjamini_hochberg
from nifty_quant.validation.stats import deflated_sharpe_ratio

# reuse the EXACT P2.2.5 screening helpers so metric definitions are not re-derived
import _p225_screen as S

# ---- approved exit band (from P2.2.2.1 sec 3.4; NOT selected on results) ----
CONV_APPROVED = ExitConvention(stop_fraction=0.08, target_fraction=0.15)
CONV_APPROVED_R12 = ExitConvention(stop_fraction=0.08, target_fraction=0.16)  # 2:1 honoured

ART25 = Path("artifacts") / "p225"          # preserved, read-only
ART26 = Path("artifacts") / "p226"
LOG_TXT = Path("logs") / "p226_exit_replay.txt"
LOG_JSON = Path("logs") / "p226_exit_replay.json"
SCENARIOS = S.SCENARIOS

# zero-metrics for an empty leg (mirrors trade_block's n==0 branch without
# touching a column-less frame)
ZERO_METRICS = {"n_trades": 0, "unresolved": 0, "open_at_end": 0, "win_rate": np.nan,
                "profit_factor": np.nan, "expectancy_net": np.nan, "expectancy_ret": np.nan,
                "max_drawdown_pct": np.nan, "total_net_pnl": 0.0}


# =========================================================================
# legs
# =========================================================================
def run_adapter_approved(resolution: str, market: MarketData) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Audited adapter, config-level exit override; orders_12 gets its own band."""
    others = tuple(sid for sid in TIER1 if sid != "orders_12")
    trades, rejections, eligible = [], [], {}
    for sids, conv in ((others, CONV_APPROVED), (("orders_12",), CONV_APPROVED_R12)):
        cfg = AdapterConfig(resolution=resolution, strategies=sids, exit=conv)
        res = OptionTradeAdapter(cfg, market=market).run()
        trades.append(res.trades_frame)
        rejections.append(res.rejections_frame)
        eligible = res.eligible          # strategy-independent; same value both runs
        assert res.summary["exit_convention"] == {
            "stop_fraction": conv.stop_fraction, "target_fraction": conv.target_fraction,
            "max_hold_bars": conv.max_hold_bars}
    tr = pd.concat(trades, ignore_index=True)
    rj = pd.concat(rejections, ignore_index=True)
    out = ART26 / resolution
    out.mkdir(parents=True, exist_ok=True)
    tr.to_csv(out / "trades.csv", index=False)
    rj.to_csv(out / "rejections.csv", index=False)
    return tr, rj, eligible


def run_signals_hold(sid: str, resolution: str, market: MarketData) -> pd.DataFrame:
    """Signal-matched hold-to-expiry: identical entries/contracts/costs, ONLY the
    exit rule replaced by ``ExitRules()`` (hold to expiry).  Same components the
    adapter itself uses (unchanged modules), same pattern as the P2.2.5.1 audit."""
    ref = OptionReference(sessions=market.sessions())
    engine = OptionsExecutionEngine(IndiaOptionsCommission())
    cfg = AdapterConfig(resolution=resolution)
    daily = market.index_daily()
    expiries = market.option_expiries(resolution)
    hold = ExitRules()
    rows = []
    for signal in index_daily_signals(sid, daily):
        entry_day = ref.next_session(signal.day)
        if entry_day is None:
            continue
        sel = select_contract(entry_day, signal.spot, signal.direction, ref=ref, market=market,
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
                "strategy_id": sid, "scenario": sc.name, "status": tr.status,
                "exit_reason": tr.reason, "signal_date": str(signal.day),
                "entry_date": str(entry_day), "expiry": str(sel.expiry),
                "strike": float(sel.strike), "direction": sel.direction,
                "entry_premium": tr.entry_premium, "lot_size": tr.lot_size, "lots": tr.lots,
                "dte_at_entry": sel.dte_at_entry, "net_pnl": tr.net_pnl,
                "gross_pnl": tr.gross_pnl, "total_costs": tr.total_costs,
                "exit_date": (str(pd.Timestamp(tr.exit_timestamp).date())
                              if tr.exit_timestamp is not None else None),
            })
    df = pd.DataFrame(rows)
    if len(df):
        df["period"] = df["entry_date"].map(S.period_of)
    return df


def dv_block(df: pd.DataFrame, sid: str) -> dict:
    """BASELINE Development+Validation metrics via the P2.2.5 helper."""
    if not len(df) or "status" not in df.columns:
        return dict(ZERO_METRICS)
    sub = df[(df["strategy_id"] == sid) & (df["scenario"] == BASELINE.name)
             & (df["entry_date"].map(S.period_of)).isin(["Development", "Validation"])] \
        if "period" not in df.columns else \
        df[(df["strategy_id"] == sid) & (df["scenario"] == BASELINE.name)
           & (df["period"].isin(["Development", "Validation"]))]
    return S.trade_block(sub)


def entry_set(df: pd.DataFrame, sid: str) -> set:
    if not len(df):
        return set()
    d = df[(df["strategy_id"] == sid) & (df["scenario"] == BASELINE.name)]
    return set(zip(d["signal_date"], d["expiry"], d["strike"]))


# =========================================================================
# main
# =========================================================================
def main() -> None:
    pd.set_option("display.width", 160)
    lines: list[str] = []

    def say(*a) -> None:
        text = " ".join(str(x) for x in a)
        print(text, flush=True)
        lines.append(text)

    ART26.mkdir(parents=True, exist_ok=True)
    market = MarketData()

    say("=" * 78)
    say("P2.2.6 CONTROLLED REPLAY - APPROVED EXIT BAND +15%/-8% (orders_12 +16%/-8%)")
    say("Deviation documented: P2.2.5 ran +/-30%/30% (unapproved, introduced at P2.2.3).")
    say("P2.2.5 artifacts preserved verbatim; nothing in artifacts/p225 is rewritten.")
    say("=" * 78)

    # ---- as-run reference READ from the preserved P2.2.5 artifact (read-only) ----
    asrun15 = pd.read_csv(ART25 / "15m" / "trades.csv")
    atm_all = pd.read_csv(ART25 / "naive_atm_baseline.csv")
    atm_all["period"] = atm_all["entry_date"].map(S.period_of)
    atm_dv = S.trade_block(atm_all[(atm_all["scenario"] == BASELINE.name)
                                   & (atm_all["period"].isin(["Development", "Validation"]))])
    say(f"\nReference (preserved P2.2.5): as-run +/-30% loaded ({len(asrun15)} rows); "
        f"all-days naive-ATM Dev+Val: n={atm_dv['n_trades']} "
        f"exp_ret={atm_dv['expectancy_ret']:.4f} PF={atm_dv['profit_factor']:.3f}")

    # ---- leg A: adapter replay with the approved band (15m primary, 5m sens) ----
    say("\n== Leg A: unchanged adapter, APPROVED ExitConvention (config override only) ==")
    trades15, rej15, elig15 = run_adapter_approved("15m", market)
    say(f"15m approved: trades={len(trades15)} rejections={len(rej15)} "
        f"eligible={ {k: v for k, v in elig15.items() if k in ('usable_expiry_files', 'eligible_entry_days')} }")
    trades5, rej5, _ = run_adapter_approved("5m", market)
    say(f"5m  approved: trades={len(trades5)} rejections={len(rej5)}")
    trades15["period"] = trades15["entry_date"].map(S.period_of)
    trades5["period"] = trades5["entry_date"].map(S.period_of)

    sessions_index = pd.DatetimeIndex([pd.Timestamp(s) for s in
                                       OptionReference(sessions=market.sessions()).sessions])

    # ---- leg B: signal-matched hold-to-expiry (only the exit rule changed) ----
    say("\n== Leg B: signal-matched hold-to-expiry (identical entries/contracts/costs) ==")
    hold15, hold5 = {}, {}
    for sid in TIER1:
        hold15[sid] = run_signals_hold(sid, "15m", market)
        hold5[sid] = run_signals_hold(sid, "5m", market)
        if len(hold15[sid]):
            hold15[sid].to_csv(ART26 / "15m" / f"hold_{sid}.csv", index=False)
            hold5[sid].to_csv(ART26 / "5m" / f"hold_{sid}.csv", index=False)

    # ---- entry-set identity check across the three exit legs (req. 5) ----
    say("\n== Entry-set identity (signal_date, expiry, strike; BASELINE) across legs ==")
    identity_rows = []
    for sid in TIER1:
        e_as, e_ap, e_h = (entry_set(asrun15, sid), entry_set(trades15, sid),
                           entry_set(hold15[sid], sid))
        same = (e_as == e_ap == e_h)
        identity_rows.append({"strategy_id": sid, "identical": bool(same),
                              "n_entries": len(e_ap), "as_vs_appr_diff": len(e_as ^ e_ap),
                              "as_vs_hold_diff": len(e_as ^ e_h)})
        say(f"  {sid}: identical={same} entries={len(e_ap)} "
            f"sym-diff(as,appr)={len(e_as ^ e_ap)} sym-diff(as,hold)={len(e_as ^ e_h)}")
    pd.DataFrame(identity_rows).to_csv(ART26 / "entry_set_identity.csv", index=False)

    # ---- full P2.2.5-methodology evaluation of the approved replay ----
    say("\n== Per-candidate evaluation under the APPROVED band (15m, pre-registered gates) ==")
    evals = {sid: S.evaluate_candidate(sid, trades15, sessions_index) for sid in TIER1}

    # cross-candidate statistics exactly as P2.2.5 (same helpers, same thresholds)
    pvals, per_trade_sharpe = [], []
    for sid in TIER1:
        p = evals[sid]["stats_baseline_dev_val"]["sign_test_p"]
        pvals.append(p if p is not None and np.isfinite(p) else 1.0)
        b = evals[sid]["by_scenario"][BASELINE.name]["score_dev_val"]
        sd = S._std_ret(trades15, sid)
        per_trade_sharpe.append(
            b["expectancy_ret"] / sd if (sd and np.isfinite(sd) and sd > 1e-12
                                         and np.isfinite(b["expectancy_ret"])) else np.nan)
    sr_std = float(np.nanstd(per_trade_sharpe, ddof=1)) if len(per_trade_sharpe) > 1 else float("nan")
    qvals = benjamini_hochberg(pvals)
    bh_map = dict(zip(TIER1, qvals))

    decisions, comparo = {}, []
    for sid, ev in evals.items():
        rets = S.ret_on_premium(S.pnl_rows(
            trades15[(trades15["strategy_id"] == sid) & (trades15["scenario"] == BASELINE.name)
                     & (trades15["period"].isin(["Development", "Validation"]))])
        ).replace([np.inf, -np.inf], np.nan).dropna().tolist()
        dsr = deflated_sharpe_ratio(rets, n_trials=S.N_TRIALS, sr_std=sr_std) \
            if len(rets) > 2 else np.nan
        base_dv = ev["by_scenario"][BASELINE.name]["score_dev_val"]
        appr_ret, appr_net = base_dv["expectancy_ret"], base_dv["expectancy_net"]
        asrun_dv = dv_block(asrun15, sid)
        hold_dv = dv_block(hold15[sid], sid)
        beats_no_trade = (appr_net is not None) and not np.isnan(appr_net) and appr_net > 0
        # the audit-corrected gate: signal-matched hold-to-expiry (Leg B), not all-days ATM
        beats_naive = (not np.isnan(appr_ret)) and (
            appr_ret > (hold_dv["expectancy_ret"] or np.nan))
        ev["dsr"], ev["bh_qvalue"] = dsr, float(bh_map[sid])
        ev["beats_no_trade"] = bool(beats_no_trade)
        ev["beats_naive_atm"] = bool(beats_naive)
        # P2.2.6 adds the legacy all-days comparison as a labelled reference only
        ev["beats_alldays_atm"] = bool((not np.isnan(appr_ret)) and
                                       appr_ret > (atm_dv["expectancy_ret"] or np.nan))
        ev["decision"] = S.decide(ev, beats_no_trade, beats_naive, dsr, bh_map[sid])
        decisions[sid] = ev["decision"]
        cons = ev["conservative_sign_stability"]
        say(f"\n--- {sid} ---")
        say(f"  unique entries {ev['unique_entries']} dev_floor_met={ev['floor_dev_trades']['met']} "
            f"exit_reasons={ev['exit_reason_diversity']['reasons']}")
        say(f"  APPROVED BASELINE Dev+Val: n={base_dv['n_trades']} win={base_dv['win_rate']:.3f} "
            f"PF={base_dv['profit_factor']:.3f} exp_net={appr_net:.1f} exp_ret={appr_ret:.5f} "
            f"maxDD={base_dv['max_drawdown_pct']}")
        say(f"  scenario spread (exp_net): " + ", ".join(
            f"{s}={ev['by_scenario'][s]['score_dev_val']['expectancy_net']:.1f}" for s in SCENARIOS))
        say(f"  conservative sign Dev={cons['dev_expectancy']}, Val={cons['val_expectancy']}, "
            f"flipped={cons['flipped']}")
        st = ev["stats_baseline_dev_val"]
        ci = st["bootstrap_net_ci"]
        say(f"  stats: boot CI={ci}, PSR={st['psr']}, sign_p={st['sign_test_p']}, "
            f"BH q={bh_map[sid]:.3f}, DSR={dsr if dsr is np.nan else round(dsr, 3)}")
        say(f"  walk-forward reliable_oos={ev['walkforward']['reliable_oos_folds']}/"
            f"{ev['walkforward']['folds']}")
        say(f"  EXIT COMPARISON exp_ret: as-run(+-30%)={asrun_dv['expectancy_ret']:.5f} | "
            f"approved(+15/-8)={appr_ret:.5f} | signal-matched hold={hold_dv['expectancy_ret']:.5f} "
            f"| all-days ATM ref={atm_dv['expectancy_ret']:.5f}")
        say(f"  beats: no-trade={beats_no_trade}, signal-matched-hold={beats_naive} "
            f"(legacy all-days bar: {ev['beats_alldays_atm']})")
        say(f"  DECISION (approved band): {ev['decision']}")
        comparo.append({
            "strategy_id": sid,
            "asrun_exp_ret": asrun_dv["expectancy_ret"], "asrun_exp_net": asrun_dv["expectancy_net"],
            "asrun_n": asrun_dv["n_trades"], "asrun_pf": asrun_dv["profit_factor"],
            "approved_exp_ret": appr_ret, "approved_exp_net": appr_net,
            "approved_n": base_dv["n_trades"], "approved_pf": base_dv["profit_factor"],
            "approved_win": base_dv["win_rate"], "approved_maxdd": base_dv["max_drawdown_pct"],
            "hold_exp_ret": hold_dv["expectancy_ret"], "hold_exp_net": hold_dv["expectancy_net"],
            "hold_n": hold_dv["n_trades"], "hold_pf": hold_dv["profit_factor"],
            "hold_win": hold_dv["win_rate"],
            "alldays_atm_exp_ret": atm_dv["expectancy_ret"],
            "decision": ev["decision"],
        })

    # ---- 5m sensitivity (approved band, BASELINE Dev+Val) ----
    say("\n== 5m sensitivity (approved band, BASELINE Dev+Val) ==")
    sens5 = {}
    for sid in TIER1:
        a5 = dv_block(trades5, sid)
        h5 = dv_block(hold5[sid], sid)
        sens5[sid] = {"approved": a5, "hold": h5}
        say(f"  {sid}: approved n={a5['n_trades']} exp_net={a5['expectancy_net']:.1f} "
            f"exp_ret={a5['expectancy_ret']:.5f} PF={a5['profit_factor']:.3f} | "
            f"hold exp_ret={h5['expectancy_ret']:.5f}")

    # ---- supplemental holdout (PREVIOUSLY ACCESSED in P2.2.5 sec 9 - never pristine) ----
    say("\n== Supplemental: protected-holdout blocks under the approved band "
        "(NOT pristine confirmation; holdout already disclosed in P2.2.5) ==")
    for sid in TIER1:
        ho = evals[sid]["by_scenario"][BASELINE.name]["Holdout"]
        say(f"  {sid}: n={ho['n_trades']} exp_net={ho['expectancy_net']} "
            f"exp_ret={ho['expectancy_ret']} (supplemental)")

    # ---- cost labels (req. 8: same statutory-only restrictions) ----
    b15 = trades15[trades15["scenario"] == BASELINE.name]
    labels = {
        "statutory_verified": int(trades15["statutory_verified"].sum()), "n_rows": int(len(trades15)),
        "all_in_verified": int(trades15["all_in_verified"].sum()),
        "brokerage_excluded": int(trades15["brokerage_excluded"].sum()),
        "cost_basis_label": S.PAINLY,
        "cost_only_friction": {"gross": float(b15["gross_pnl"].sum()),
                               "net": float(b15["net_pnl"].sum()),
                               "friction": float(b15["total_costs"].sum())},
    }
    say(f"\nCost labels: {labels['cost_basis_label']} | statutory_verified="
        f"{labels['statutory_verified']}/{labels['n_rows']} | all_in_verified="
        f"{labels['all_in_verified']}/{labels['n_rows']} | cost-only {labels['cost_only_friction']}")

    # ---- persist decision + comparison frames ----
    pd.DataFrame(comparo).to_csv(ART26 / "exit_comparison.csv", index=False)
    dec_rows = []
    for sid, ev in evals.items():
        dec_rows.append({
            "strategy_id": sid, "decision": ev["decision"],
            "dev_unique": ev["unique_entries"]["Development"],
            "val_unique": ev["unique_entries"]["Validation"],
            "holdout_unique_supplemental": ev["unique_entries"]["Holdout"],
            "dev_floor_met": ev["floor_dev_trades"]["met"],
            "approved_baseline_devval_exp_net":
                ev["by_scenario"][BASELINE.name]["score_dev_val"]["expectancy_net"],
            "approved_baseline_devval_exp_ret":
                ev["by_scenario"][BASELINE.name]["score_dev_val"]["expectancy_ret"],
            "approved_baseline_devval_pf":
                ev["by_scenario"][BASELINE.name]["score_dev_val"]["profit_factor"],
            "conservative_flipped": ev["conservative_sign_stability"]["flipped"],
            "sign_test_p": ev["stats_baseline_dev_val"]["sign_test_p"],
            "bh_qvalue": ev["bh_qvalue"], "dsr": ev["dsr"],
            "psr": ev["stats_baseline_dev_val"]["psr"],
            "wf_reliable_folds": ev["walkforward"]["reliable_oos_folds"],
            "beats_no_trade": ev["beats_no_trade"],
            "beats_signal_matched_hold": ev["beats_naive_atm"],
            "beats_alldays_atm_reference_only": ev["beats_alldays_atm"],
        })
    pd.DataFrame(dec_rows).to_csv(ART26 / "decisions.csv", index=False)
    rows = []
    for sid, ev in evals.items():
        for scen, blocks in ev["by_scenario"].items():
            for period, m in blocks.items():
                rows.append({"strategy_id": sid, "scenario": scen, "period_block": period, **m})
    pd.DataFrame(rows).to_csv(ART26 / "scenario_metrics.csv", index=False)

    payload = {
        "deviation": {"approved": "TP+15%/SL-8%; orders_12 +16%/-8% (ratio-preserving)",
                      "as_run_p225": "TP+30%/SL-30% uniform (unapproved, introduced at P2.2.3)",
                      "audit": "P2.2.5.1_SCREENING_INTEGRITY_AUDIT.md"},
        "config": {"exit_approved": {"default": {"stop": 0.08, "target": 0.15},
                                     "orders_12": {"stop": 0.08, "target": 0.16}},
                   "resolution_primary": "15m", "sensitivity": "5m", "min_dte_at_entry": 2,
                   "scenarios": list(SCENARIOS), "n_trials": S.N_TRIALS,
                   "fdr_alpha": S.FDR_ALPHA, "cost_basis_label": S.PAINLY},
        "eligible_15m": elig15,
        "counts": {"15m": {"trades": int(len(trades15)), "rejections": int(len(rej15))},
                   "5m": {"trades": int(len(trades5)), "rejections": int(len(rej5))}},
        "entry_set_identity": identity_rows,
        "alldays_atm_reference_preserved": atm_dv,
        "labels": labels,
        "candidates": {sid: {
            "unique_entries": evals[sid]["unique_entries"],
            "dev_floor": evals[sid]["floor_dev_trades"],
            "approved_by_period_baseline": evals[sid]["by_scenario"][BASELINE.name],
            "conservative_sign_stability": evals[sid]["conservative_sign_stability"],
            "stats": evals[sid]["stats_baseline_dev_val"],
            "dsr": evals[sid]["dsr"], "bh_qvalue": evals[sid]["bh_qvalue"],
            "walkforward": {k: v for k, v in evals[sid]["walkforward"].items() if k != "table"},
            "beats_no_trade": evals[sid]["beats_no_trade"],
            "beats_signal_matched_hold": evals[sid]["beats_naive_atm"],
            "beats_alldays_atm_reference_only": evals[sid]["beats_alldays_atm"],
            "decision": evals[sid]["decision"],
        } for sid in TIER1},
        "sensitivity_5m": {sid: sens5[sid] for sid in TIER1},
        "holdout_note": "supplemental only - holdout was disclosed in P2.2.5 sec 9; "
                        "not used to select anything and not pristine confirmation",
        "decisions": decisions,
    }
    LOG_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    say(f"\nWrote {LOG_TXT.name}, {LOG_JSON.name}, artifacts under {ART26}")
    LOG_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
