"""P2 stage 5 - audit the derivative charge schedule against the cached sources.

The directive requires options statutory charges to be *sourced* from cached circulars
or contract specs, and every rate that cannot be independently verified from those bytes
to be marked ``estimated`` and excluded from strict financial validation.  This script
does exactly that, offline and at zero cost:

1. scan every cached NSE F&O circular (``FAOP*.pdf``) and ``contract_specs.html`` for
   derivative transaction-charge numbers (STT / transaction charge / SEBI / GST / stamp);
2. record what it finds (it finds none - the cached circulars cover expiry regimes, not
   charges), so the ``estimated`` flags are an evidenced conclusion, not a shrug;
3. write the effective-dated ``options_charge_schedule.csv`` from the encoded defaults,
   carrying provenance and the estimated flag on every row;
4. cost a worked option round-trip (lot size resolved from the stage-2 reference DB) and
   write ``logs/p2_options_cost.txt`` with the cited-vs-estimated split and the resulting
   ``strict_verifiable`` verdict.

Outputs:
  data/reference/options_charge_schedule.csv
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
from nifty_quant.engine.orders import OrderSide  # noqa: E402

NSE = Path("data/raw/p1_nse")
CIRCULARS = list(NSE.glob("FAOP*.pdf")) + list((NSE / "circulars").glob("FAOP*.pdf"))
SPECS = NSE / "contract_specs.html"
OUT_CSV = Path("data/reference/options_charge_schedule.csv")
OUT_TXT = Path("logs/p2_options_cost.txt")

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
    frame = pd.DataFrame(rows, columns=["component", "rate_type", "value", "effective_from",
                                        "effective_to", "side", "provenance", "estimated"])
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(OUT_CSV, index=False)
    return frame


def worked_round_trip(say) -> None:
    """Cost a long-CE round trip at lot sizes resolved from the stage-2 reference DB."""
    lots = 2
    day = date(2025, 1, 30)
    lot_res = LotTable().lot_on(day)
    lot_size = lot_res.lot if lot_res.known else 75
    say(f"lot size resolved from the stage-2 reference DB for {day}: {lot_size} "
        f"({'date-regime' if lot_res.known else 'fallback'})")

    neutral = IndiaOptionsCommission(IndiaOptionsCosts())  # brokerage not configured
    trip = neutral.round_trip(entry_premium=150.0, exit_premium=180.0,
                              lot_size=lot_size, lots=lots,
                              entry_date="2025-01-02", exit_date="2025-01-30")
    # the breakdown display key "exchange" is stored under the provenance key "exchange_txn"
    prov_key = {"exchange": "exchange_txn"}
    for leg in ("entry", "exit"):
        res = trip[leg]
        say(f"\n{res.side.upper()} leg @ {res.on_date}  turnover Rs {res.transaction_value:,.2f}")
        for comp, val in res.components.items():
            prov = res.provenance.get(prov_key.get(comp, comp), "")
            if comp == "gst":
                tag = "DERIVED"      # 18% on brokerage+exchange+sebi; status follows them
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

    say(f"\nround-trip total costs: Rs {trip['total_costs']:,.4f}")
    say(f"strict_verifiable: {trip['strict_verifiable']}")
    if not trip["strict_verifiable"]:
        say("=> at least one statutory rate is estimated (not in cached sources); this "
            "trade is EXCLUDED from strict financial validation (fail-closed), exactly as "
            "the directive requires. The figures are usable for modelling only.")

    # brokerage is configurable and assumed nothing: show a configured variant
    say("\nBrokerage is configurable - with a flat Rs 20/order brokerage the legs become:")
    priced = IndiaOptionsCommission(
        IndiaOptionsCosts(brokerage=BrokerageConfig(model="flat", flat_per_order=20.0)))
    entry = priced.compute(150.0, lot_size, lots, OrderSide.BUY, "2025-01-02")
    exit_ = priced.compute(180.0, lot_size, lots, OrderSide.SELL, "2025-01-30")
    say(f"    entry Rs {entry:,.4f} + exit Rs {exit_:,.4f} = Rs {entry + exit_:,.4f}")


def main() -> int:
    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    say("P2 stage 5 - derivative charge schedule audit (sourced vs estimated)")
    say("=" * 104)
    say("searching cached NSE circulars and contract specs for charge rates ...")
    for ev in scan_cached_for_charges(say):
        say(ev)

    say("\nwriting effective-dated options_charge_schedule.csv ...")
    frame = write_schedule()
    say(frame.to_string(index=False))
    estimated = int(frame["estimated"].sum())
    say(f"\nrows: {len(frame)}  sourced: {len(frame) - estimated}  estimated: {estimated}")

    say("\n" + "=" * 104)
    say("worked option round trip")
    say("=" * 104)
    worked_round_trip(say)

    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT_CSV} and {OUT_TXT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
