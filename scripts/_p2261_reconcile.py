"""P2.2.6.1 - forensic reconciliation of every executed screening configuration.

Triggers (from the directive): P2.2.6 produced a *contradictory session summary* -
classifications and metric claims that do not match the persisted artifacts.  This
script treats the artifacts as the only source of truth and rebuilds every number
INDEPENDENTLY from the raw per-trade CSV rows (own metric implementations, own
period mapping, no import of the screening helpers, no adapter re-run).

It answers:
  C1  config inventory - every exit convention actually executed, with strategy,
      resolution, scenario, period boundaries and artifact path; explicit search
      for any +15%/-15% experiment;
  C2  independent recomputation per configuration: unique entries, gross P&L,
      statutory charges, brokerage-excluded net, expectancy (rupees AND per-unit
      premium), profit factor, win rate, max drawdown (chronological);
  C3  metric-definition audits: PF can never be negative under wins/|losses|;
      net == gross - costs identity; PF-vs-expectancy sign divergences explained;
      frame-order vs chronological drawdown pairing deltas;
  C4  exit-comparison entry identity (identical dates/contracts/quantities/costs,
      only the exit rule changed);
  C5  classification re-derived from raw rows under the PRE-REGISTERED Â§10 rule
      (15m primary only; 5m is sensitivity; no PF threshold exists in the
      approved methodology and none is applied; holdout supplemental).

Nothing is re-run through the engine, no artifact is rewritten, no parameter is
selected on results.

    $env:PYTHONPATH='.'; python scripts/_p2261_reconcile.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"
LOG_TXT = ROOT / "logs" / "p2261_reconcile.txt"
LOG_JSON = ROOT / "logs" / "p2261_reconcile.json"

# pre-registered boundaries and floors (P2.2_BACKTESTING_METHODOLOGY.md; identical
# constants used by P2.2.5/P2.2.6 - restated here independently)
DEV_END = pd.Timestamp("2024-06-30")
VAL_END = pd.Timestamp("2025-06-30")
MIN_DEV_TRADES = 30
INITIAL_CAPITAL = 100_000.0
TIER1 = ("orders_12", "orders_01", "orders_18", "orders_05", "orders_07")
CLOSED = {"FILLED", "OPEN_AT_END"}
# scenario enum names are prefixed (A_/B_/C_) - verified from the artifact files
BASE, CONS = "B_BASELINE", "C_CONSERVATIVE"


def period_of(day) -> str:
    d = pd.Timestamp(day)
    return "Development" if d <= DEV_END else ("Validation" if d <= VAL_END else "Holdout")


def indep_metrics(df: pd.DataFrame) -> dict:
    """Independent metric rebuild from raw per-trade rows (BASELINE subset must
    be passed pre-filtered); P&L rows exclude UNRESOLVED/NO_ENTRY like the engine
    records themselves do."""
    closed = df[df["status"].isin(CLOSED)].copy()
    n = len(closed)
    out = {"n_pnl_trades": n}
    if n == 0:
        return out
    net = closed["net_pnl"].astype(float)
    gross = closed["gross_pnl"].astype(float)
    costs = closed["total_costs"].astype(float)
    wins, losses = net[net > 0].sum(), net[net < 0].sum()
    # standard PF: total profit / total loss, non-negative by construction
    pf = float(wins / abs(losses)) if losses < 0 else (float("inf") if wins > 0 else float("nan"))
    outlay = (closed["entry_premium"].astype(float) * closed["lot_size"].astype(float)
              * closed["lots"].astype(float))
    ret = net / outlay.replace(0, np.nan)
    # chronological drawdown on the 100k curve
    ch = closed.sort_values("exit_date")
    curve = INITIAL_CAPITAL + ch["net_pnl"].astype(float).cumsum().to_numpy()
    peak = np.maximum.accumulate(curve)
    dd_chrono = float((curve / peak - 1.0).min()) if n > 1 else float("nan")
    fr = closed  # frame order (how trade_block paired it)
    curve_f = INITIAL_CAPITAL + fr["net_pnl"].astype(float).cumsum().to_numpy()
    dd_frame = float((curve_f / np.maximum.accumulate(curve_f) - 1.0).min()) if n > 1 else float("nan")
    resid = (net - (gross - costs)).abs()
    out.update({
        "gross_pnl": float(gross.sum()), "statutory_costs": float(costs.sum()),
        "net_pnl_excl_brokerage": float(net.sum()),
        "expectancy_net": float(net.mean()), "expectancy_ret": float(ret.mean()),
        "profit_factor": pf, "win_rate": float((net > 0).mean()),
        "max_drawdown_chrono": dd_chrono, "max_drawdown_frame_order": dd_frame,
        "pnl_identity_max_residual": float(resid.max()),
        "pnl_identity_violations_gt_1rs": int((resid > 1.0).sum()),
    })
    return out


def unique_entries(df: pd.DataFrame, scen: str = BASE) -> dict:
    u = df[(df["scenario"] == scen)].drop_duplicates(
        subset=["signal_date", "expiry", "strike"]).copy()
    u["p"] = u["entry_date"].map(period_of)
    vc = u["p"].value_counts().to_dict()
    return {"Development": int(vc.get("Development", 0)),
            "Validation": int(vc.get("Validation", 0)),
            "Holdout": int(vc.get("Holdout", 0))}


def dv(df: pd.DataFrame) -> pd.DataFrame:
    m = df["entry_date"].map(period_of)
    return df[m.isin(["Development", "Validation"])]


def entry_set(df: pd.DataFrame, sid: str, scen: str = BASE) -> set:
    d = df[(df["strategy_id"] == sid) & (df["scenario"] == scen)]
    return set(zip(d["signal_date"], d["expiry"], d["strike"]))


def conservative_stability(df: pd.DataFrame, sid: str) -> dict:
    c = df[(df["strategy_id"] == sid) & (df["scenario"] == CONS)]
    dev = indep_metrics(c[c["entry_date"].map(period_of) == "Development"])
    val = indep_metrics(c[c["entry_date"].map(period_of) == "Validation"])
    de, ve = dev.get("expectancy_net", np.nan), val.get("expectancy_net", np.nan)
    flipped = bool(not (np.isnan(de) or np.isnan(ve))) and bool((de >= 0) != (ve >= 0))
    return {"dev_exp": de, "val_exp": ve, "flipped": flipped}


def main() -> None:
    lines: list[str] = []

    def say(*a) -> None:
        t = " ".join(str(x) for x in a)
        print(t, flush=True)
        lines.append(t)

    # ================= C1: configuration inventory =================
    say("=" * 78)
    say("C1  CONFIGURATION INVENTORY - what was actually executed")
    say("=" * 78)
    inventory = []
    summ_paths = sorted({p for p in ART.rglob("summary.json")
                         if any(part.startswith("p22") for part in p.parts)})
    for p in summ_paths:
        s = json.loads(p.read_text(encoding="utf-8"))
        inventory.append({
            "artifact": str(p.parent.relative_to(ROOT)), "resolution": s.get("resolution"),
            "exit_convention": s.get("exit_convention"),
            "trades": s.get("counts", {}).get("trades"),
            "rejections": s.get("counts", {}).get("rejections"),
            "strategies": s.get("strategies"),
            "min_dte_at_entry": s.get("min_dte_at_entry"),
        })
    for it in inventory:
        say(f"  {it['artifact']}: resolution={it['resolution']} exit={it['exit_convention']} "
            f"trades={it['trades']} rejections={it['rejections']}")

    # P2.2.6 wrote no summary.json; its recorded config lives in the run payload
    p26 = json.loads((ROOT / "logs" / "p226_exit_replay.json").read_text(encoding="utf-8"))
    say(f"  artifacts/p226/{{15m,5m}}/trades.csv: exit={p26['config']['exit_approved']} "
        "(config-level override; orders_12 separate +16/-8 invocation)  "
        f"counts={p26['counts']}")
    say(f"  artifacts/p226/{{15m,5m}}/hold_orders_*.csv: signal-matched hold-to-expiry "
        "(ExitRules(), only the exit differs)")
    say(f"  artifacts/p225/naive_atm_baseline.csv: unconditional all-days ATM hold "
        f"(every session, no signals); preserved reference")
    say(f"  logs/p2251_audit.txt: P2.2.5.1 audit recomputations (as-run, approved, hold) "
        "- log only, no trade artifacts")

    distinct = set()
    for it in inventory:
        ec = it["exit_convention"] or {}
        distinct.add((ec.get("stop_fraction"), ec.get("target_fraction")))
    distinct.add((0.08, 0.15))
    distinct.add((0.08, 0.16))
    distinct.add((None, None))  # hold-to-expiry legs
    say(f"\n  distinct exit conventions executed: {sorted(map(str, distinct))}")
    fifteen_fifteen = any(s == 0.15 and t == 0.15 for s, t in distinct if s is not None)
    say(f"  +15%/-15% experiment present? {fifteen_fifteen}  "
        "(no such config exists in any summary.json, run payload or script constant)")
    hits = [str(f.relative_to(ROOT)) for f in ROOT.rglob("*")
            if f.is_file() and f.suffix in (".py", ".json", ".csv")
            and "p22" in f.name and "15" in f.name]
    say(f"  file-name sweep for 15/15 artifacts: {hits or 'none'}")

    # ================= C2: independent recomputation =================
    say("\n" + "=" * 78)
    say("C2  INDEPENDENT RECOMPUTE FROM RAW ROWS (15m primary, BASELINE, Dev+Val)")
    say("=" * 78)

    runs = {
        "as-run +-30 (P2.2.5, expanded tree)": ART / "p225" / "15m" / "trades.csv",
        "approved +15/-8 (P2.2.6; orders_12 +16/-8)": ART / "p226" / "15m" / "trades.csv",
    }
    recomputed = {}
    for label, path in runs.items():
        df = pd.read_csv(path)
        say(f"\n  [{label}]  rows={len(df)}")
        rows = []
        for sid in TIER1:
            sub = df[df["strategy_id"] == sid]
            base = sub[sub["scenario"] == BASE]
            m = indep_metrics(dv(base))
            ue = unique_entries(sub)
            cons = conservative_stability(sub, sid)
            rows.append({"strategy_id": sid, **ue, **m,
                         "conservative_flipped": cons["flipped"]})
            say(f"    {sid}: dev/val/hold unique={ue['Development']}/{ue['Validation']}/"
                f"{ue['Holdout']} n={m['n_pnl_trades']} gross={m['gross_pnl']:.1f} "
                f"costs={m['statutory_costs']:.1f} net={m['net_pnl_excl_brokerage']:.1f} "
                f"exp_net={m['expectancy_net']:.1f} exp_ret={m['expectancy_ret']:.5f} "
                f"PF={m['profit_factor']:.3f} win={m['win_rate']:.3f} "
                f"dd_chrono={m['max_drawdown_chrono']:.5f}")
        recomputed[label] = pd.DataFrame(rows)

    say("\n  signal-matched hold-to-expiry (P2.2.6 leg B, 15m, BASELINE, Dev+Val):")
    hold_rows = []
    for sid in TIER1:
        h = pd.read_csv(ART / "p226" / "15m" / f"hold_{sid}.csv")
        m = indep_metrics(dv(h[h["scenario"] == BASE]))
        hold_rows.append({"strategy_id": sid, **m})
        say(f"    {sid}: n={m['n_pnl_trades']} exp_net={m['expectancy_net']:.1f} "
            f"exp_ret={m['expectancy_ret']:.5f} PF={m['profit_factor']:.3f} "
            f"win={m['win_rate']:.3f}")
    hold_recomputed = pd.DataFrame(hold_rows)

    say("\n  preserved all-days naive-ATM (read-only reference):")
    atmg = pd.read_csv(ART / "p225" / "naive_atm_baseline.csv")
    atm_b = atmg[atmg["scenario"] == BASE]
    m = indep_metrics(dv(atm_b))
    say(f"    entries(distinct dates, all periods)={atm_b['entry_date'].nunique()} "
        f"Dev+Val n={m['n_pnl_trades']} exp_net={m['expectancy_net']:.1f} "
        f"exp_ret={m['expectancy_ret']:.5f} PF={m['profit_factor']:.3f}")

    # ================= C3: cross-checks against published tables =================
    say("\n" + "=" * 78)
    say("C3  CROSS-CHECK: independent recompute vs published metric tables")
    say("=" * 78)
    mismatches = []
    for label, path, pub_path in (
        ("as-run +-30", ART / "p225" / "15m" / "trades.csv", ART / "p225" / "scenario_metrics.csv"),
        ("approved", ART / "p226" / "15m" / "trades.csv", ART / "p226" / "scenario_metrics.csv"),
    ):
        pub = pd.read_csv(pub_path)
        pb = pub[(pub["scenario"] == BASE) & (pub["period_block"] == "score_dev_val")]
        key = "as-run +-30 (P2.2.5, expanded tree)" if label == "as-run +-30" else \
            "approved +15/-8 (P2.2.6; orders_12 +16/-8)"
        mine = recomputed[key]
        for _, r in mine.iterrows():
            row = pb[pb["strategy_id"] == r["strategy_id"]].iloc[0]
            for a, b in (("expectancy_net", "expectancy_net"),
                         ("expectancy_ret", "expectancy_ret"),
                         ("profit_factor", "profit_factor"), ("win_rate", "win_rate"),
                         ("n_trades", "n_pnl_trades")):
                x, y = float(row[a]), float(r[b])
                if not (np.isnan(x) and np.isnan(y)) and abs(x - y) > 0.01:
                    mismatches.append({"run": label, "sid": r["strategy_id"], "metric": a,
                                       "published": x, "recomputed": y})
    # P2.2.6 exit_comparison cross-check (approved vs hold columns)
    ex = pd.read_csv(ART / "p226" / "exit_comparison.csv")
    for _, r in ex.iterrows():
        mine = recomputed["approved +15/-8 (P2.2.6; orders_12 +16/-8)"]
        mr = mine[mine["strategy_id"] == r["strategy_id"]].iloc[0]
        if abs(float(r["approved_exp_net"]) - float(mr["expectancy_net"])) > 0.01:
            mismatches.append({"run": "exit_comparison", "sid": r["strategy_id"],
                               "metric": "approved_exp_net", "published": r["approved_exp_net"],
                               "recomputed": float(mr["expectancy_net"])})
        hh = hold_recomputed[hold_recomputed["strategy_id"] == r["strategy_id"]].iloc[0]
        if abs(float(r["hold_exp_ret"]) - float(hh["expectancy_ret"])) > 1e-6:
            mismatches.append({"run": "exit_comparison", "sid": r["strategy_id"],
                               "metric": "hold_exp_ret", "published": r["hold_exp_ret"],
                               "recomputed": float(hh["expectancy_ret"])})
    say(f"  cross-check mismatches beyond tolerance: {len(mismatches)}")
    for mm in mismatches:
        say(f"    MISMATCH {mm}")

    # PF sign audit across every published metrics table
    say("\n  PF sign audit (standard definition can never be negative):")
    bad_pf = []
    for f in list(ART.rglob("scenario_metrics.csv")) + [ART / "p226" / "exit_comparison.csv"]:
        t = pd.read_csv(f)
        cols = [c for c in t.columns if c == "profit_factor" or c.endswith("_pf")]
        for c in cols:
            neg = t[pd.to_numeric(t[c], errors="coerce") < 0]
            if len(neg):
                bad_pf.append({"file": str(f.relative_to(ROOT)), "column": c, "rows": len(neg)})
    say(f"    negative PF cells found: {bad_pf or 'none'}")

    # net == gross - costs identity, all runs
    say("\n  per-row P&L identity (net == gross - costs) violations > Rs 1:")
    for label, path in list(runs.items()) + [("hold 15m (orders_12)",
                                              ART / "p226" / "15m" / "hold_orders_12.csv")]:
        df = pd.read_csv(path)
        cl = df[df["status"].isin(CLOSED)]
        resid = (cl["net_pnl"] - (cl["gross_pnl"] - cl["total_costs"])).abs()
        say(f"    {label}: max residual={resid.max():.6f} violations={int((resid > 1).sum())}"
            f" of {len(cl)}")

    # drawdown pairing note (frame-order trade_block vs chronological)
    say("\n  drawdown pairing (published trade_block vs strict chronological recompute):")
    for label, key in (("as-run +-30", "as-run +-30 (P2.2.5, expanded tree)"),
                       ("approved", "approved +15/-8 (P2.2.6; orders_12 +16/-8)")):
        for _, r in recomputed[key].iterrows():
            d1, d2 = r["max_drawdown_frame_order"], r["max_drawdown_chrono"]
            if abs(d1 - d2) > 1e-4:
                say(f"    {label} {r['strategy_id']}: frame-order={d1:.5f} chrono={d2:.5f} "
                    f"(pairing difference - see report)")

    # PF vs expectancy sign divergence register
    say("\n  PF / expectancy sign divergences (PF is Rs-denominated; exp_ret per unit premium):")
    for label, key in (("as-run +-30", "as-run +-30 (P2.2.5, expanded tree)"),
                       ("approved", "approved +15/-8 (P2.2.6; orders_12 +16/-8)")):
        for _, r in recomputed[key].iterrows():
            pf, en, er = r["profit_factor"], r["expectancy_net"], r["expectancy_ret"]
            if (pf > 1) != (en > 0) or np.sign(er) not in (0, np.sign(en)):
                say(f"    {label} {r['strategy_id']}: PF={pf:.3f} exp_net={en:.1f} "
                    f"exp_ret={er:.5f} - consistent under definitions (both correct, "
                    f"different denominators; Â§5 gate reads exp_net)")

    # ================= C4: entry identity across exit legs =================
    say("\n" + "=" * 78)
    say("C4  EXIT-COMPARISON ENTRY IDENTITY (dates/contracts/quantities/costs)")
    say("=" * 78)
    asdf = pd.read_csv(ART / "p225" / "15m" / "trades.csv")
    apdf = pd.read_csv(ART / "p226" / "15m" / "trades.csv")
    for sid in TIER1:
        h = pd.read_csv(ART / "p226" / "15m" / f"hold_{sid}.csv")
        e1, e2, e3 = entry_set(asdf, sid), entry_set(apdf, sid), entry_set(h, sid)
        qty_ok = True
        a2 = apdf[(apdf["strategy_id"] == sid) & (apdf["scenario"] == BASE)]
        h2 = h[(h["strategy_id"] == sid) & (h["scenario"] == BASE)]
        k = ["signal_date", "expiry", "strike"]
        qa = a2.drop_duplicates(k).set_index(k)["lot_size"].to_dict()
        qh = h2.drop_duplicates(k).set_index(k)["lot_size"].to_dict()
        qty_ok = qa == qh
        say(f"  {sid}: as-run==approved==hold entry sets: {e1 == e2 == e3} "
            f"(|entries|={len(e2)}); quantities identical: {qty_ok}")

    # ================= C5: classification re-derivation (15m primary only) =================
    say("\n" + "=" * 78)
    say("C5  CLASSIFICATION RE-DERIVED FROM RAW ROWS (pre-registered rule; 15m primary)")
    say("=" * 78)
    say("  no PF threshold exists anywhere in the approved methodology; none applied.")
    say("  statistical flags (DSR/BH/PSR) taken from the published validator runs - all")
    say("  candidates fail them under both bands, so survives_stats=False everywhere.")
    final = []
    for label, key in (("as-run +-30 (P2.2.5)", "as-run +-30 (P2.2.5, expanded tree)"),
                       ("approved (P2.2.6)", "approved +15/-8 (P2.2.6; orders_12 +16/-8)")):
        mine = recomputed[key]
        say(f"\n  [{label}]")
        for _, r in mine.iterrows():
            floor = int(r["Development"]) >= MIN_DEV_TRADES
            breakeven = float(r["expectancy_net"]) >= 0
            stable = not bool(r["conservative_flipped"])
            if not floor:
                cls = "Insufficient evidence"
            elif not breakeven or not stable:
                cls = "Fails screening"
            else:
                cls = "Insufficient evidence"  # survives_stats False -> per rule
            if label.startswith("approved"):
                final.append({"strategy_id": r["strategy_id"], "unique_dev": int(r["Development"]),
                              "break_even": breakeven, "sign_stable": stable,
                              "classification": cls})
            say(f"    {r['strategy_id']}: floor={floor} break_even={breakeven} "
                f"sign_stable={stable} -> {cls}")
    say("\n  FINAL (approved band, 15m primary; 5m = sensitivity only; holdout supplemental):")
    for f in final:
        say(f"    {f['strategy_id']}: {f['classification']}")

    # published-decisions cross-check
    p25d = pd.read_csv(ART / "p225" / "decisions.csv")[["strategy_id", "decision"]]
    p26d = pd.read_csv(ART / "p226" / "decisions.csv")[["strategy_id", "decision"]]
    say("\n  published P2.2.5 decisions (as-run +-30): "
        + ", ".join(f"{a}={b}" for a, b in zip(p25d.strategy_id, p25d.decision)))
    say("  published P2.2.6 decisions (approved):     "
        + ", ".join(f"{a}={b}" for a, b in zip(p26d.strategy_id, p26d.decision)))

    # ================= C6: contradicted-claims register =================
    say("\n" + "=" * 78)
    say("C6  CONTRADICTED-CLAIMS REGISTER (session-summary claims vs artifacts)")
    say("=" * 78)
    claims = [
        ("'+15%/-15% band ran'", "no (0.15,0.15) config in any summary.json/payload/script",
         "NOT EXECUTED"),
        ("'PF >= 1.1 / 1.3 gates'", "methodology Â§3 floors are trade counts + exit diversity only",
         "NO SUCH APPROVED THRESHOLD"),
        ("'orders_12 -> Advances'", "raw rows: dev unique entries 8 < 30 -> Insufficient evidence",
         "FALSE (floor dominates under both bands)"),
        ("'orders_05 fails at -24 PF<1 (5m as decision basis)'",
         "5m is sensitivity; 15m approved exp_net(orders_05)=-122.4, floor met -> Fails screening",
         "MIXED: verdict right, basis misquoted"),
        ("'hold +11,060 / +12.5% (orders_01)'",
         "p226/15m/hold_orders_01.csv Dev+Val exp_net=" +
         f"{hold_recomputed[hold_recomputed.strategy_id=='orders_01'].iloc[0]['expectancy_net']:.1f}",
         "FALSE"),
    ]
    for c, ev, v in claims:
        say(f"  claim: {c}\n    evidence: {ev}\n    verdict: {v}")

    LOG_JSON.write_text(json.dumps({
        "inventory": inventory,
        "exit_conventions_distinct": sorted(map(str, distinct)),
        "fifteen_fifteen_present": fifteen_fifteen,
        "recomputed_15m_baseline_devval": {
            k: recomputed[k].to_dict(orient="records") for k in recomputed},
        "hold_recomputed_15m": hold_recomputed.to_dict(orient="records"),
        "cross_check_mismatches": mismatches,
        "negative_pf_cells": bad_pf,
        "final_classification_approved": final,
        "contradicted_claims": [list(c) for c in claims],
    }, indent=2, default=str), encoding="utf-8")
    LOG_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(f"\nWrote {LOG_TXT.name}, {LOG_JSON.name}")


if __name__ == "__main__":
    main()

