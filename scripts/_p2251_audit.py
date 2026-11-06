"""P2.2.5.1 - narrow SCREENING INTEGRITY AUDIT (read-only; changes no strategy).

Three questions from the directive, answered with numbers recomputed from the
*unchanged* adapter modules so the audit itself cannot introduce a strategy change:

  S1  TP/SL impact: what the AS-RUN +/-30%/30% convention produced versus the
      APPROVED +15%/-8% band (orders_12 ratio-preserving +16%/-8%), Dev+Val BASELINE.
      Cross-checked against artifacts/p225/15m/trades.csv to prove the harness
      reproduces the published screening numbers before any counterfactual is quoted.

  S2  Naive-ATM benchmark integrity: why it reports n=855, whether its entries
      match each candidate's actual signal dates/contracts, and a clearly labelled
      SIGNAL-MATCHED hold-to-expiry comparison - identical entries/contracts/costs,
      ONLY the exit rule changed (the +/-30% band replaced by hold-to-expiry).

  S3  The orders_12/orders_01/orders_18 identical-signal finding, checked against
      independently recomputed signal arrays.

Nothing here optimises a parameter, selects a value on the holdout, or mutates a
strategy; the approved-vs-as-run bands are reported as an integrity comparison, not
a new choice.  The original P2.2.5 artifacts and report are read, never rewritten.

    $env:PYTHONPATH='.'; python scripts/_p2251_audit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np
import pandas as pd

from nifty_quant.config import BASELINE
from nifty_quant.engine.options_costs import IndiaOptionsCommission
from nifty_quant.engine.options_execution import ExitRules, OptionsExecutionEngine
from nifty_quant.options.bars import build_option_bars
from nifty_quant.options.data import MarketData
from nifty_quant.options.pipeline import TIER1, AdapterConfig, ExitConvention
from nifty_quant.options.reference import OptionReference
from nifty_quant.options.selection import select_contract
from nifty_quant.options.signals import index_daily_signals

# reuse the EXACT screening helpers so the metric definitions are not re-derived
import _p225_screen as S

RES = "15m"
ART = ROOT / "artifacts" / "p225"
OUT_TXT = Path("logs") / "p2251_audit.txt"


def _emit(say):
    market = MarketData()
    ref = OptionReference(sessions=market.sessions())
    engine = OptionsExecutionEngine(IndiaOptionsCommission())
    cfg = AdapterConfig(resolution=RES)
    daily = market.index_daily()
    expiries = market.option_expiries(RES)

    def run_signals(sid: str, rules_for) -> pd.DataFrame:
        """One row per candidate signal x scenario, with the caller's exit rule.

        Entries/contracts/costs are produced by the unchanged selection + engine;
        ``rules_for(entry_open, tick)`` is the ONLY thing that varies between
        as-run, approved-band and hold-to-expiry passes.
        """
        rows = []
        for signal in index_daily_signals(sid, daily):
            entry_day = ref.next_session(signal.day)
            if entry_day is None:
                continue
            sel = select_contract(entry_day, signal.spot, signal.direction,
                                  ref=ref, market=market, observed_expiries=expiries,
                                  min_dte=cfg.min_dte, max_strike_steps=cfg.max_strike_steps,
                                  resolution=RES)
            if not sel.ok:
                continue
            frame = market.option_frame(sel.expiry, RES)
            series = build_option_bars(frame, strike=sel.strike, option_type=sel.option_type,
                                       entry_day=entry_day, expiry=sel.expiry)
            if not series.ok:
                continue
            rules = rules_for(series.entry_open, sel.tick_size)
            for sc in cfg.scenarios_for(sel.lot_size, cfg.lots):
                tr = engine.run(series.bars, series.signal_index, rules, lots=cfg.lots,
                                lot_size=sel.lot_size, scenario=sc)
                rows.append({
                    "strategy_id": sid, "scenario": sc.name, "status": tr.status,
                    "exit_reason": tr.reason, "signal_date": str(signal.day),
                    "entry_date": str(entry_day), "expiry": str(sel.expiry),
                    "strike": float(sel.strike), "direction": sel.direction,
                    "entry_premium": tr.entry_premium, "lot_size": tr.lot_size,
                    "lots": tr.lots, "dte_at_entry": sel.dte_at_entry,
                    "net_pnl": tr.net_pnl, "gross_pnl": tr.gross_pnl,
                    "total_costs": tr.total_costs,
                    "exit_date": (str(pd.Timestamp(tr.exit_timestamp).date())
                                  if tr.exit_timestamp is not None else None),
                })
        df = pd.DataFrame(rows)
        if len(df):
            df["period"] = df["entry_date"].map(S.period_of)
        return df

    def dv_baseline(df: pd.DataFrame) -> dict:
        if not len(df):
            return S.trade_block(pd.DataFrame())
        sub = df[(df["scenario"] == BASELINE.name)
                 & (df["period"].isin(["Development", "Validation"]))]
        return S.trade_block(sub)

    # ---- cross-check the harness against the published trades.csv -------------
    published = pd.read_csv(ART / "15m" / "trades.csv")
    asrun_exit = ExitConvention(stop_fraction=0.30, target_fraction=0.30)  # as-run band, pinned explicitly (P2.2.6.2)
    say("=" * 78)
    say("HARNESS CROSS-CHECK (as-run +/-30% must reproduce the published report)")
    say("=" * 78)
    asrun = {}
    for sid in TIER1:
        df = run_signals(sid, asrun_exit.rules_for)
        asrun[sid] = df
        m = dv_baseline(df)
        pub = published[(published["strategy_id"] == sid)
                        & (published["scenario"] == BASELINE.name)]
        pub_unique = pub.drop_duplicates(subset=["signal_date", "expiry", "strike"]).shape[0]
        say(f"  {sid}: recomputed Dev+Val n={m['n_trades']} expectancy_ret={m['expectancy_ret']:.6f} "
            f"| published 15m unique BASELINE entries={pub_unique}")

    # ---- S1: as-run +/-30% vs APPROVED +15%/-8% (ratio-preserving for orders_12) --
    say("")
    say("=" * 78)
    say("S1  TP/SL INTEGRITY  (as-run +/-30%/30%  vs  APPROVED +15%/-8%)")
    say("    approved band = TP +15% / SL -8%; orders_12 uses ratio-preserving +16%/-8%")
    say("=" * 78)
    say(f"  {'candidate':<10} {'as-run Ret':>12} {'as-run Net':>12} "
        f"{'appr Ret':>12} {'appr Net':>12} {'appr PF':>8} {'appr win':>9}")
    appr_exit = ExitConvention(stop_fraction=0.08, target_fraction=0.15)
    appr_exit_r12 = ExitConvention(stop_fraction=0.08, target_fraction=0.16)
    appr = {}
    for sid in TIER1:
        conv = appr_exit_r12 if sid == "orders_12" else appr_exit
        df = run_signals(sid, conv.rules_for)
        appr[sid] = df
        a = dv_baseline(df)
        r = dv_baseline(asrun[sid])
        say(f"  {sid:<10} {r['expectancy_ret']:>12.5f} {r['expectancy_net']:>12.1f} "
            f"{a['expectancy_ret']:>12.5f} {a['expectancy_net']:>12.1f} "
            f"{a['profit_factor']:>8.3f} {a['win_rate']:>9.3f}")
    say("  (expectancy_ret = mean net_pnl / premium-outlay, the metric the naive-ATM")
    say("   comparison in report S9 uses; Dev+Val BASELINE only; holdout untouched.)")

    # ---- S2: signal-matched hold-to-expiry (ONLY the exit rule changed) --------
    say("")
    say("=" * 78)
    say("S2  NAIVE-ATM BENCHMARK INTEGRITY + SIGNAL-MATCHED HOLD-TO-EXPIRY")
    say("=" * 78)
    naive = pd.read_csv(ART / "naive_atm_baseline.csv")
    nb = naive[naive["scenario"] == BASELINE.name]
    nb_dv = nb[nb["period"].isin(["Development", "Validation"])] if "period" in nb else nb
    say(f"  published all-days naive-ATM (BASELINE): total rows={len(nb)}, "
        f"distinct entry_date={nb['entry_date'].nunique()}, Dev+Val rows={len(nb_dv)}")
    say(f"    -> n=855 in the report is the Dev+Val subset of a benchmark that enters")
    say(f"       EVERY session day ({nb['entry_date'].nunique()} distinct entry dates over the")
    say(f"       whole calendar), i.e. it is an UNCONDITIONAL daily ATM series.")
    # candidate unique signal entries for context
    for sid in TIER1:
        e = asrun[sid]
        u = e[(e["scenario"] == BASELINE.name)
              & (e["period"].isin(["Development", "Validation"]))]
        say(f"  {sid}: its own unique Dev+Val entries={u.drop_duplicates(subset=['signal_date','expiry','strike']).shape[0]}")
    say("")
    say("  SIGNAL-MATCHED hold-to-expiry (identical entries/contracts/costs, exit rule -> hold):")
    say(f"  {'candidate':<10} {'n':>4} {'win':>7} {'PF':>7} {'exp_ret(30%)':>13} {'exp_ret(hold)':>14} {'exp_net(hold)':>14}")
    hold = ExitRules()  # no stop, no target, no max_hold -> settle at expiry
    holdres = {}
    for sid in TIER1:
        df = run_signals(sid, lambda op, tick: hold)
        holdres[sid] = df
        h = dv_baseline(df)
        r = dv_baseline(asrun[sid])
        say(f"  {sid:<10} {h['n_trades']:>4} {h['win_rate']:>7.3f} {h['profit_factor']:>7.3f} "
            f"{r['expectancy_ret']:>13.5f} {h['expectancy_ret']:>14.5f} {h['expectancy_net']:>14.1f}")
    say("  (This is the like-for-like comparison the report's all-days n=855 benchmark is NOT.")
    say("   Only the exit rule differs from each candidate's own signal-matched entries.)")

    # ---- S3: identical-signal finding vs independent signal arrays ------------
    say("")
    say("=" * 78)
    say("S3  orders_12 / orders_01 / orders_18 IDENTICAL-SIGNAL VERIFICATION")
    say("=" * 78)
    arrays = {}
    for sid in TIER1:
        sigs = sorted((str(s.day), s.direction, round(float(s.spot), 2))
                      for s in index_daily_signals(sid, daily))
        arrays[sid] = sigs
        say(f"  {sid}: {len(sigs)} signals; first 3 = {sigs[:3]}")
    a12, a01, a18 = arrays["orders_12"], arrays["orders_01"], arrays["orders_18"]
    say(f"  orders_12 == orders_01 ? {a12 == a01}")
    say(f"  orders_12 == orders_18 ? {a12 == a18}")
    say(f"  orders_12 == orders_05 ? {a12 == arrays['orders_05']}")
    say(f"  orders_12 == orders_07 ? {a12 == arrays['orders_07']}")
    # also compare full trade entry sets (signal_date+expiry+strike) BASELINE Dev+Val
    def entryset(df):
        if not len(df):
            return set()
        d = df[(df["scenario"] == BASELINE.name)
               & (df["period"].isin(["Development", "Validation"]))]
        return set(zip(d["signal_date"], d["expiry"], d["strike"]))
    say(f"  entry-set 12==01 ? {entryset(asrun['orders_12']) == entryset(asrun['orders_01'])}"
        f" | 12==18 ? {entryset(asrun['orders_12']) == entryset(asrun['orders_18'])}"
        f" | 12==05 ? {entryset(asrun['orders_12']) == entryset(asrun['orders_05'])}")


def main() -> int:
    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    _emit(say)
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(f"\nWrote {OUT_TXT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
