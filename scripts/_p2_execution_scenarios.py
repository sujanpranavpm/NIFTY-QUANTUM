"""Deterministic options execution scenario report (P2 stage 6, directive Part 7).

Runs one synthetic long-CE trade through the three named scenarios (A OPTIMISTIC /
B BASELINE / C CONSERVATIVE) and prints the profitability delta between them, then a
collision case (stop must win over target in one bar) and a missing-data case (a gap
interval must refuse the fill, never fabricate one).  Everything is a fixed, hard-coded
bar path so the report is reproducible with no dataset dependency.

Writes ``logs/p2_execution.txt``.  Exits non-zero if a guarantee is violated (the
optimistic scenario is not the most profitable, the collision is not resolved to the
stop, or the gap is traded through).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nifty_quant.config import BASELINE, CONSERVATIVE, OPTIMISTIC  # noqa: E402
from nifty_quant.engine.options_costs import IndiaOptionsCommission  # noqa: E402
from nifty_quant.engine.options_execution import (  # noqa: E402
    REASON_STOP,
    UNRESOLVED,
    ExitRules,
    OptionBar,
    OptionsExecutionEngine,
)

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "p2_execution.txt"

LOT_SIZE = 75          # official NIFTY board lot for the 2025-01 regime
LOTS = 2               # exercise "multiple lots"


def bar(ts_min, o, h, l, c, vol=1_000_000.0, *, available=True, expiry=False):
    ts = datetime(2025, 1, 2, 9, 15 + ts_min)
    if not available:
        return OptionBar(ts, float("nan"), float("nan"), float("nan"), float("nan"),
                         0.0, available=False, is_expiry=expiry)
    return OptionBar(ts, o, h, l, c, vol, available=True, is_expiry=expiry)


def rising_to_target():
    """A path that reaches the 120 target and would never hit the 90 stop."""
    return [
        bar(0, 100, 101, 99, 100),      # signal bar (index 0)
        bar(5, 100, 104, 100, 103),     # next-bar entry at open 100 (index 1)
        bar(10, 105, 112, 104, 110),    # exits evaluated from here (index 2)
        bar(15, 112, 121, 111, 118),    # target 120 touched (index 3)
        bar(20, 120, 124, 119, 122),
    ]


def collision_bar_path():
    """One bar whose range touches BOTH the 90 stop and the 120 target - stop must win."""
    return [
        bar(0, 100, 101, 99, 100),
        bar(5, 100, 101, 99, 100),                    # entry (index 1)
        bar(10, 100, 125, 85, 110),                   # touches stop 90 and target 120
    ]


def gap_at_exit_path():
    """Expiry lands on an unobservable bar: the trade must come back UNRESOLVED."""
    return [
        bar(0, 100, 101, 99, 100),
        bar(5, 100, 104, 100, 103),        # entry (index 1)
        bar(10, 0, 0, 0, 0, available=False, expiry=True),   # expiry bar is a gap
    ]


def money(x: float) -> str:
    return f"{x:,.2f}"


def run():
    lines: list[str] = []
    say = lines.append
    commission = IndiaOptionsCommission()  # default schedule; 2025 date -> pre-2026 exchange era
    engine = OptionsExecutionEngine(commission=commission)
    rules = ExitRules(stop_premium=90.0, target_premium=120.0)
    scenarios = (OPTIMISTIC, BASELINE, CONSERVATIVE)

    say("=" * 74)
    say("NIFTY QUANT - P2 stage 6: options execution scenario report")
    say("=" * 74)
    say("")
    say(f"Trade: BUY NIFTY {LOT_SIZE}-lot CE x {LOTS} lots | entry next-bar open, "
        f"target 120, stop 90 | cost engine: {scheme_note(commission)}")
    say("")
    say("A. Scenario ladder on one rising path (target exit)")
    say("-" * 74)
    say(f"{'scenario':<16}{'slip bps':>9}{'entry':>9}{'exit':>9}{'gross':>12}"
        f"{'costs':>10}{'net P&L':>12}  reason")
    results = {}
    bars = rising_to_target()
    for sc in scenarios:
        r = engine.run(bars, 0, rules, lots=LOTS, lot_size=LOT_SIZE, scenario=sc)
        results[sc.name] = r
        say(f"{sc.name:<16}{sc.adverse_slippage_bps:>9.1f}"
            f"{money(r.entry_premium or 0):>9}{money(r.exit_premium or 0):>9}"
            f"{money(r.gross_pnl):>12}{money(r.total_costs):>10}{money(r.net_pnl):>12}  "
            f"{r.reason}")
    a, b, c = (results[s.name] for s in scenarios)
    say("")
    say(f"Profitability delta  A-B = {money(a.net_pnl - b.net_pnl)}   "
        f"B-C = {money(b.net_pnl - c.net_pnl)}   A-C = {money(a.net_pnl - c.net_pnl)}")
    monotonic = a.net_pnl >= b.net_pnl >= c.net_pnl
    say(f"Monotonic A >= B >= C (optimistic is the most generous): {monotonic}")
    survives_conservative = c.net_pnl > 0
    say(f"Qualification: profitable under the CONSERVATIVE scenario? {survives_conservative}")
    say("  (a strategy that pays only under A is fragile; qualification is not granted "
        "on the optimistic result alone)")
    say(f"Strict financial verifiability of these P&L figures: "
        f"{a.strict_verifiable}  (this 2025 trade's exchange-txn rate is a pre-2026 "
        f"estimated era -> excluded; current-era rates are sourced)")
    say("")

    say("B. Stop/target collision in one bar - conservative resolution")
    say("-" * 74)
    col_rules = ExitRules(stop_premium=90.0, target_premium=120.0)
    r = engine.run(collision_bar_path(), 0, col_rules,
                   lots=LOTS, lot_size=LOT_SIZE, scenario=BASELINE)
    say(f"entry={money(r.entry_premium)} exit={money(r.exit_premium)} "
        f"reason={r.reason} net={money(r.net_pnl)}")
    collision_ok = r.reason == REASON_STOP
    say(f"Collision resolved to the STOP (not the target): {collision_ok}")
    say("")

    say("C. Missing-data interval at expiry - no fabricated fill")
    say("-" * 74)
    g = engine.run(gap_at_exit_path(), 0, ExitRules(stop_premium=90.0, target_premium=120.0),
                   lots=LOTS, lot_size=LOT_SIZE, scenario=BASELINE)
    say(f"status={g.status} exit_premium={g.exit_premium} net={money(g.net_pnl)}")
    gap_ok = g.status == UNRESOLVED and g.exit_premium is None
    say(f"Gap at expiry refused a fill (UNRESOLVED, no invented price): {gap_ok}")
    say("")

    say("=" * 74)
    verdict = monotonic and collision_ok and gap_ok
    say(f"P2 stage 6 execution verdict: {'PASS' if verdict else 'FAIL'}")
    say("=" * 74)

    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if verdict else 1


def scheme_note(commission: IndiaOptionsCommission) -> str:
    regs = commission.config.regimes
    est = sum(1 for r in regs if r.estimated)
    return ("IndiaOptionsCommission, effective-dated premium-turnover schedule, "
            f"{len(regs) - est}/{len(regs)} rows sourced; a trade on a date whose rate "
            "is estimated is not strict-verifiable")


if __name__ == "__main__":
    sys.exit(run())
