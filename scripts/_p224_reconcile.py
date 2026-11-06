"""P2.2.4 stages 7 & 8 - reconcile expanded coverage vs the 22-expiry baseline and
run the UNCHANGED P2.2.3 OptionTradeAdapter over both datasets.

Nothing here changes a strategy, an exit rule, a cost schedule or an adapter default:
the same ``AdapterConfig`` (with the historical +/-30%/30% band pinned explicitly per
P2.2.6.2 - the library default has since become the approved +15%/-8% policy) is
executed twice, once against a candle directory holding
only the expiries that were cached before P2.2.4 ("before") and once against the full
expanded candle directory ("after").  ``OptionTradeAdapter`` discovers expiries by
globbing the candle files, so the only difference is which files are present.

  * "before" expiries = every cached provider file MINUS the files this task downloaded
    (the download manifest records exactly the expiries added by P2.2.4).
  * usable-contract / contract-day counts are measured from the 15m candles of each set.

Outputs: artifacts/p224/* (per-trade + per-rejection of the expanded run, gitignored)
         logs/p224_reconcile.txt
         logs/p224_reconcile.json
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nifty_quant.options.data import MarketData  # noqa: E402
from nifty_quant.options.pipeline import (  # noqa: E402
    AdapterConfig, ExitConvention, OptionTradeAdapter)

# P2.2.6.2 pin: the P2.2.4 experiment ran under the then-default +/-30%/30% band;
# the library default is now the approved +15%/-8% policy.  The historical band is
# stated explicitly so this reconciliation stays reproducible.  Do not "update" it.
HISTORICAL_EXIT = ExitConvention(stop_fraction=0.30, target_fraction=0.30)

CANDLES = Path("data/derived/candles")
MANIFEST = Path("logs/p224_download_manifest.json")
BASE_DIR = Path("artifacts/p224/_baseline_candles")
OUT_DIR = Path("artifacts/p224")
OUT_TXT = Path("logs/p224_reconcile.txt")
OUT_JSON = Path("logs/p224_reconcile.json")
RES = "15m"


def added_expiries() -> set[str]:
    recs = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {r["expiry"] for r in recs if r.get("status") == "eligible"}


def all_expiries() -> set[str]:
    return {p.stem.split("__")[-1] for p in CANDLES.glob(f"{RES}__NIFTY__*.parquet")}


def contract_and_day_counts(candles_dir: Path) -> dict:
    """Distinct tradable contracts and contract-days across a 15m candle set."""
    contracts: set[tuple] = set()
    contract_days: set[tuple] = set()
    files = 0
    for p in sorted(candles_dir.glob(f"{RES}__NIFTY__*.parquet")):
        try:
            d = pd.read_parquet(p, columns=["trading_day", "strike", "option_type"])
        except Exception:  # noqa: BLE001  (empty-schema stub)
            continue
        if not len(d):
            continue
        files += 1
        exp = p.stem.split("__")[-1]
        for row in d.itertuples(index=False):
            day = str(row.trading_day)[:10]
            contracts.add((exp, int(row.strike), str(row.option_type)))
            contract_days.add((exp, int(row.strike), str(row.option_type), day))
    return {"candle_files": files, "usable_contracts": len(contracts),
            "contract_days": len(contract_days)}


def _describe(result) -> dict:
    trades = result.trades_frame
    rej = result.rejections_frame
    out = {"eligible_stats": result.eligible}
    if not trades.empty:
        out["n_trades"] = int(len(trades))
        out["n_unique_entries"] = int(
            trades[["strategy_id", "signal_date", "expiry", "strike"]].drop_duplicates().shape[0])
        out["by_status"] = trades["status"].value_counts().to_dict()
        out["statutory_verified_true"] = int(trades["statutory_verified"].sum())
        out["all_in_verified_true"] = int(trades["all_in_verified"].sum())
        out["brokerage_excluded_all"] = bool(trades["brokerage_excluded"].all())
    else:
        out["n_trades"] = 0
        out["n_unique_entries"] = 0
    out["n_rejections"] = int(len(rej))
    out["rejections_by_reason"] = (rej["reason"].value_counts().to_dict()
                                   if not rej.empty else {})
    return out


def run_adapter(candles_dir: Path, resolution: str, persist: bool = False) -> dict:
    market = MarketData(candles_dir=candles_dir)
    adapter = OptionTradeAdapter(
        AdapterConfig(resolution=resolution, exit=HISTORICAL_EXIT), market=market)
    result = adapter.run(persist=persist, out_dir=OUT_DIR if persist else None)
    return _describe(result)


def main() -> int:
    lines: list[str] = []

    def say(t: str = "") -> None:
        print(t, flush=True)
        lines.append(t)

    added = added_expiries()
    allx = all_expiries()
    baseline = sorted(allx - added)
    say("P2.2.4 reconciliation + unchanged-adapter run (before vs after)")
    say("=" * 72)
    say(f"total expanded 15m candle files : {len(allx)}")
    say(f"expiries ADDED by P2.2.4        : {len(added)}")
    say(f"baseline (pre-P2.2.4) expiries  : {len(baseline)}")

    # build a baseline-only candle directory (copy only baseline expiry files)
    if BASE_DIR.exists():
        shutil.rmtree(BASE_DIR)
    BASE_DIR.mkdir(parents=True)
    for exp in baseline:
        for res in ("1m", "5m", "15m"):
            src = CANDLES / f"{res}__NIFTY__{exp}.parquet"
            if src.exists():
                shutil.copy2(src, BASE_DIR / src.name)

    payload = {"baseline_expiries": baseline, "added_count": len(added)}

    say("\n-- contract / contract-day measurement --")
    payload["coverage_before"] = contract_and_day_counts(BASE_DIR)
    payload["coverage_after"] = contract_and_day_counts(CANDLES)
    for k in ("coverage_before", "coverage_after"):
        c = payload[k]
        say(f"  {k:16s}: files={c['candle_files']} usable_contracts="
            f"{c['usable_contracts']:,} contract_days={c['contract_days']:,}")

    say("\n-- unchanged adapter (primary 15m) --")
    payload["adapter_before_15m"] = run_adapter(BASE_DIR, "15m")
    payload["adapter_after_15m"] = run_adapter(CANDLES, "15m", persist=True)
    for k in ("adapter_before_15m", "adapter_after_15m"):
        e = payload[k]["eligible_stats"]
        say(f"  {k:20s}: usable_expiries={e['usable_expiry_files']} "
            f"distinct_option_days={e['distinct_option_days']} "
            f"overlap_days={e['overlap_option_days']} "
            f"eligible_entry_days={e['eligible_entry_days']} "
            f"unique_entries={payload[k].get('n_unique_entries', 0)} "
            f"trades={payload[k].get('n_trades', 0)} "
            f"rejections={payload[k]['n_rejections']}")

    say("\n-- unchanged adapter (sensitivity 5m) --")
    payload["adapter_before_5m"] = run_adapter(BASE_DIR, "5m")
    payload["adapter_after_5m"] = run_adapter(CANDLES, "5m")
    for k in ("adapter_before_5m", "adapter_after_5m"):
        say(f"  {k:20s}: unique_entries={payload[k].get('n_unique_entries', 0)} "
            f"trades={payload[k].get('n_trades', 0)} "
            f"rejections={payload[k]['n_rejections']}")

    OUT_JSON.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(f"\nwrote {OUT_TXT} and {OUT_JSON}")
    if BASE_DIR.exists():
        shutil.rmtree(BASE_DIR, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
