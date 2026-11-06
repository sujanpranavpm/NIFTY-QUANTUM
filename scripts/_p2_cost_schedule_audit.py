"""P2 stage 5 / P2.1 / P2.1.2 - audit the derivative charge schedule against its official sources.

The directive requires options statutory charges to be *sourced* from official documents,
and every rate that cannot be independently verified from those bytes to be marked
``estimated`` and excluded from strict financial validation.  P2.1 replaced the earlier
fabricated ``Rs 20 per lot`` exchange fee with premium-turnover rates read from NSE
circulars FA73061 / FA56129, the Finance (No.2) Bill 2024 and the NSE STT/levies/stamp
pages (all fetched at Rs 0 by ``scripts/_p21_charge_sources.py`` into
``data/raw/p21_charges``).  P2.1.2 added the primary circulars FA64232 (the real
Sept-27-2024 revision FA73061 mis-cites as "64323") and FA46730 (the 2021 slab schedule).
This script:

1. scans the cached NSE F&O circulars and ``contract_specs.html`` (they carry expiry
   regimes, not charges) so the ``estimated`` flags remain an evidenced conclusion;
2. writes the effective-dated ``options_charge_schedule.csv`` from the encoded defaults,
   carrying source_url / circular_ref / basis / verification / provenance / estimated on
   every row;
3. preserves the *previous* (pre-P2.1) schedule verbatim in
   ``options_charge_schedule_superseded.csv`` for auditability;
4. costs worked option round-trips (lot size resolved from the stage-2 reference DB) and
   writes ``logs/p2_options_cost.txt`` with the cited-vs-estimated split, the strict
   verdict, and the effect of the configurable/verified-brokerage rule.

Outputs:
  data/reference/options_charge_schedule.csv
  data/reference/options_charge_schedule_superseded.csv
  logs/p2_options_cost.txt

Usage (from the repo root):
    python scripts/_p2_cost_schedule_audit.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nifty_quant.config import (  # noqa: E402
    BrokerageConfig, IndiaOptionsCosts, default_options_charge_regimes)
from nifty_quant.derivatives.reference import LotTable  # noqa: E402
from nifty_quant.engine.options_costs import IndiaOptionsCommission  # noqa: E402

NSE = Path("data/raw/p1_nse")
CIRCULARS = list(NSE.glob("FAOP*.pdf")) + list((NSE / "circulars").glob("FAOP*.pdf"))
SPECS = NSE / "contract_specs.html"
OUT_CSV = Path("data/reference/options_charge_schedule.csv")
OUT_SUPERSEDED = Path("data/reference/options_charge_schedule_superseded.csv")
OUT_TXT = Path("logs/p2_options_cost.txt")

SCHEDULE_COLUMNS = ["component", "rate_type", "value", "effective_from", "effective_to",
                    "side", "basis", "source_url", "circular_ref", "verification",
                    "provenance", "estimated"]

# The pre-P2.1 schedule, kept verbatim so the correction stays traceable: a fabricated
# flat Rs 20/lot exchange fee and STT rising 0.05% -> 0.1%, every row ``estimated``.
SUPERSEDED_CSV = (
    "component,rate_type,value,effective_from,effective_to,side,provenance,estimated\n"
    "stt,premium_pct,0.0005,2015-04-01,2024-09-30,sell,not present in cached NSE "
    "circulars/contract specs (offline audit); public statutory value,True\n"
    "stt,premium_pct,0.001,2024-10-01,,sell,not present in cached NSE circulars/contract "
    "specs (offline audit); public statutory value,True\n"
    "exchange_txn,per_lot,20.0,2015-04-01,,both,not present in cached NSE "
    "circulars/contract specs (offline audit); public statutory value,True\n"
    "sebi,premium_pct,1e-06,2015-04-01,,both,not present in cached NSE circulars/contract "
    "specs (offline audit); public statutory value,True\n"
    "stamp_duty,premium_pct,3e-05,2015-04-01,,buy,not present in cached NSE "
    "circulars/contract specs (offline audit); public statutory value,True\n"
)

CHARGE_TOKENS = ("STT", "securities transaction tax", "transaction charge",
                 "turnover charge", "SEBI turnover", "GST", "stamp duty")


def scan_cached_for_charges(say) -> list[str]:
    """Return evidence lines: which cached bytes were searched and what they contained."""
    lines: list[str] = []
    try:
        from pypdf import PdfReader
    except Exception as exc:  # audit-only dependency
        lines.append(f"pypdf unavailable ({exc}); PDF scan skipped - schedule stays estimated")
        return lines

    total_hits = 0
    for pdf in sorted(set(CIRCULARS)):
        try:
            text = "\n".join((p.extract_text() or "") for p in PdfReader(pdf).pages)
        except Exception as exc:
            lines.append(f"  {pdf.name}: could not read ({exc})")
            continue
        found = [t for t in CHARGE_TOKENS if t.lower() in text.lower()]
        total_hits += len(found)
        lines.append(f"  {pdf.name}: {len(text)} chars; charge tokens present: "
                     f"{found or 'NONE'}")
    if SPECS.exists():
        html = SPECS.read_text(encoding="utf-8", errors="replace").lower()
        found = [t for t in CHARGE_TOKENS if t.lower() in html]
        total_hits += len(found)
        lines.append(f"  contract_specs.html: charge tokens present: {found or 'NONE'}")
    lines.append(f"TOTAL derivative-charge tokens found in cached bytes: {total_hits}")
    return lines


def write_schedule() -> pd.DataFrame:
    rows = [r.as_dict() for r in default_options_charge_regimes()]
    frame = pd.DataFrame(rows, columns=SCHEDULE_COLUMNS)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(OUT_CSV, index=False)
    OUT_SUPERSEDED.write_text(SUPERSEDED_CSV, encoding="utf-8")
    return frame


def _render_leg(say, res) -> None:
    """Print one costed leg with a per-component SOURCED/ESTIMATED/CONFIGURABLE tag."""
    prov_key = {"exchange": "exchange_txn"}  # display key -> provenance key
    say(f"\n{res.side.upper()} leg @ {res.on_date}  turnover Rs {res.transaction_value:,.2f}")
    for comp, val in res.components.items():
        prov = res.provenance.get(prov_key.get(comp, comp), "")
        if comp == "gst":
            tag = "DERIVED"      # 18% on brokerage+exchange+ipft+sebi; a config tax knob
        elif comp == "slippage" or prov.startswith("configurable"):
            tag = "CONFIGURABLE"
        elif prov.startswith("estimated"):
            tag = "ESTIMATED"
        elif prov.startswith("sourced"):
            tag = "SOURCED"
        else:
            tag = "ASSUMED"
        say(f"    {comp:>11}: Rs {val:>12,.4f}   [{tag}]")
    say(f"    {'TOTAL':>11}: Rs {res.total:>12,.4f}")


def worked_round_trip(say) -> None:
    """Cost long-CE round trips at lot sizes resolved from the stage-2 reference DB.

    Three examples are recalculated under the corrected premium-turnover schedule:
      * the original 2025 example (entry 2025-01-02, exit 2025-01-30) - under P2.1 its
        exchange charge fell in the pre-2026 flat era whose effective date was inferred
        (NSE/FA/64323 PDF 404); P2.1.2 sourced the primary circular NSE/FA/64232, so the
        2024-10-01 era is *verified* and this trade is now strict-verifiable;
      * a slab-era example (2022) - the FA46730 slab VALUES are sourced but the marginal
        slab for a research book is an assumption, so the leg stays ESTIMATED and is not
        strict-verifiable;
      * a 2026 example (both legs on/after 2026-03-01) - every statutory rate there is
        read from FA73061, so with brokerage excluded the round trip IS strict-verifiable.
    """
    lots = 2
    day = date(2025, 1, 30)
    lot_res = LotTable().lot_on(day)
    lot_size = lot_res.lot if lot_res.known else 75
    say(f"lot size resolved from the stage-2 reference DB for {day}: {lot_size} "
        f"({'date-regime' if lot_res.known else 'fallback'})")

    neutral = IndiaOptionsCommission(IndiaOptionsCosts())  # brokerage excluded (model='none')

    # ---- Example 1: the original 2025 worked round trip, recalculated ----
    say("\n" + "-" * 104)
    say("EXAMPLE 1 - recalculated 2025 round trip (premium 150 -> 180), brokerage excluded")
    say("-" * 104)
    trip = neutral.round_trip(entry_premium=150.0, exit_premium=180.0,
                              lot_size=lot_size, lots=lots,
                              entry_date="2025-01-02", exit_date="2025-01-30")
    for leg in ("entry", "exit"):
        _render_leg(say, trip[leg])
    say(f"\nround-trip total costs: Rs {trip['total_costs']:,.4f}")
    say(f"brokerage_excluded: {trip['brokerage_excluded']}")
    say(f"statutory_verified: {trip['statutory_verified']}   "
        f"all_in_verified: {trip['all_in_verified']}   "
        f"strict_verifiable: {trip['strict_verifiable']}")
    say(f"cost basis: {trip['cost_basis']}")
    if trip["strict_verifiable"]:
        say("=> P2.1.2: the 2024-10-01 exchange-transaction era is sourced from the actual "
            "circular NSE/FA/64232 (FA73061's '64323' is a circular-reference mismatch, "
            "likely a typographical error), so this "
            "2025 trade IS strict-verifiable with brokerage excluded. It is NOT all-in "
            "verified: the broker fee is a real cost that has been omitted.")
    else:
        say("=> still fail-closed: some statutory rate used by this trade is estimated.")

    # ---- Example 1b: a slab-era trade (fails closed on the slab convention) ----
    say("\n" + "-" * 104)
    say("EXAMPLE 1b - 2022 slab-era round trip (premium 150 -> 180), brokerage excluded")
    say("-" * 104)
    trip22 = neutral.round_trip(entry_premium=150.0, exit_premium=180.0,
                                lot_size=25, lots=lots,
                                entry_date="2022-01-03", exit_date="2022-01-27")
    for leg in ("entry", "exit"):
        _render_leg(say, trip22[leg])
    say(f"\nround-trip total costs: Rs {trip22['total_costs']:,.4f}")
    say(f"statutory_verified: {trip22['statutory_verified']}   "
        f"strict_verifiable: {trip22['strict_verifiable']}")
    if not trip22["strict_verifiable"]:
        say("=> the FA46730/FA56129 slab VALUES are sourced, but the marginal slab for a "
            "research book is an assumption (no member turnover), so the two slab-era "
            "exchange_txn rows stay ESTIMATED and these trades are EXCLUDED from strict "
            "financial validation (fail-closed). Modelling use only.")

    # ---- Example 2: a 2026 round trip on fully-sourced current-era rates ----
    say("\n" + "-" * 104)
    say("EXAMPLE 2 - 2026 round trip (premium 150 -> 180) on FA73061 current-era rates")
    say("-" * 104)
    trip26 = neutral.round_trip(entry_premium=150.0, exit_premium=180.0,
                                lot_size=lot_size, lots=lots,
                                entry_date="2026-03-02", exit_date="2026-03-20")
    for leg in ("entry", "exit"):
        _render_leg(say, trip26[leg])
    say(f"\nround-trip total costs: Rs {trip26['total_costs']:,.4f}")
    say(f"brokerage_excluded: {trip26['brokerage_excluded']}")
    say(f"statutory_verified: {trip26['statutory_verified']}   "
        f"all_in_verified: {trip26['all_in_verified']}   "
        f"strict_verifiable: {trip26['strict_verifiable']}")
    if trip26["strict_verifiable"]:
        say("=> every statutory rate on these dates is sourced (STT 0.10%, exchange txn "
            "Rs 3,552.99/cr, IPFT Rs 0.01/cr, SEBI Rs 10/cr, stamp 0.003% buyer) and "
            "brokerage is excluded, so this round trip IS strict-verifiable.")

    # ---- The configurable / verified-brokerage rule (directive P2.1 section 4) ----
    say("\n" + "-" * 104)
    say("BROKERAGE RULE - strict validation needs a verified broker config or exclusion")
    say("-" * 104)
    unverified = IndiaOptionsCommission(IndiaOptionsCosts(
        brokerage=BrokerageConfig(model="flat", flat_per_order=20.0)))
    verified = IndiaOptionsCommission(IndiaOptionsCosts(
        brokerage=BrokerageConfig(model="flat", flat_per_order=20.0, verified=True)))
    u = unverified.round_trip(150.0, 180.0, lot_size, lots, "2026-03-02", "2026-03-20")
    v = verified.round_trip(150.0, 180.0, lot_size, lots, "2026-03-02", "2026-03-20")
    say(f"    flat Rs 20/order, verified=False: total Rs {u['total_costs']:,.4f}  "
        f"strict_verifiable={u['strict_verifiable']} (unverified broker fee)")
    say(f"    flat Rs 20/order, verified=True : total Rs {v['total_costs']:,.4f}  "
        f"strict_verifiable={v['strict_verifiable']} (user attests the tariff)")


def main() -> int:
    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    say("P2.1 - derivative charge schedule audit (sourced vs estimated, premium-turnover basis)")
    say("=" * 104)
    say("searching cached NSE circulars and contract specs for charge rates ...")
    for ev in scan_cached_for_charges(say):
        say(ev)

    say("\nwriting effective-dated options_charge_schedule.csv (+ superseded pre-P2.1 file) ...")
    frame = write_schedule()
    say(frame[["component", "rate_type", "value", "effective_from", "effective_to", "side",
               "basis", "verification", "estimated"]].to_string(index=False))
    estimated = int(frame["estimated"].astype(bool).sum())
    say(f"\nrows: {len(frame)}  sourced/verified: {len(frame) - estimated}  estimated: {estimated}")
    core = frame[frame["component"].isin(["stt", "exchange_txn", "ipft", "sebi", "stamp_duty"])]
    core_est = int(core["estimated"].astype(bool).sum())
    say(f"core statutory rows (stt/exchange_txn/ipft/sebi/stamp_duty): {len(core)}  "
        f"estimated: {core_est}")
    live = core[core["effective_to"].isna()]
    live_est = int(live["estimated"].astype(bool).sum())
    say(f"currently in-force core rows (effective_to blank): {len(live)}  estimated: {live_est}"
        f"  -> {'all sourced' if live_est == 0 else 'some estimated'}")
    say("estimated rows that remain: the two pre-Apr-2026 slab-era exchange_txn rows "
        "(values sourced from FA46730/FA56129 but the marginal slab is a convention) and "
        "the gst config tax knob; stt_exercised rows are reference-only (engine has no "
        "exercise path). P2.1.2 verified the 2024-10-01 exchange_txn era from the primary "
        "circular FA64232.")

    say("\n" + "=" * 104)
    say("worked option round trips (recalculated on the premium-turnover schedule)")
    say("=" * 104)
    worked_round_trip(say)

    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT_CSV}, {OUT_SUPERSEDED} and {OUT_TXT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
