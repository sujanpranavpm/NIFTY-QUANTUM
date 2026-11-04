"""Milestone item 3 - resolve the terminal-candle convention (open-labelled vs close-labelled).

The provider stamps every 1-minute bar with a single timestamp and documents nothing in
the file itself.  Some expiry files end their session at 15:29 and others at 15:30, so
before any 5-minute or 15-minute aggregation is allowed, the meaning of a label must be
established from evidence.

Two mutually exclusive hypotheses for a bar stamped T:

  START (open-labelled)   the bar covers [T, T+1)   - session buckets are
                          09:15 .. 15:29, and a trade printed at exactly 15:30:00 needs
                          an extra bucket stamped 15:30.
  END   (close-labelled)  the bar covers (T-1, T]   - session buckets are
                          09:16 .. 15:30, and 09:15 can only exist if something printed
                          at exactly 09:15:00.

Arbiters used, strongest first:

1. Source documentation.  Fetch the dataset card and file tree anonymously and quote
   whatever the publisher actually says about timestamps, volume units and OI.
2. Terminal-bucket volume shape.  Under END the 15:30 bucket contains the whole final
   minute, so its volume is comparable with a typical minute.  Under START the 15:30
   bucket contains only the 15:30:00 print, so its volume is a small fraction of a
   typical minute and the bar is usually degenerate (open = high = low = close).
3. Closing-print containment.  P1 measured the last bar's close == official LastPric for
   100% of contracts from 2025-01-30.  A file whose last label is 15:29 can only contain
   the last trade of the day if 15:29 covers [15:29, 15:30] - i.e. open-labelled with the
   session's final bucket closed at the right edge.  Re-measure this per file.
4. Official day high/low coverage with and without the terminal bucket: does the terminal
   bucket hold unique price information (real minute trades) or nothing but a print?
5. Opening-bucket shape (volume vs a typical minute, and degeneracy).

Sessions that match neither hypothesis are reported as AMBIGUOUS and must be rejected by
strict validation rather than guessed.  Nothing here writes to the raw files, and no
5-minute/15-minute aggregation is produced by this script.

Usage (repo root):  python scripts/_m4_terminal_candle.py
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _p1_nse_reconcile import ensure_bhavcopy  # noqa: E402

HF = Path("data/raw/p1_sample")
OUT_TXT = Path("logs/m4_terminal_candle.txt")
OUT_JSON = Path("logs/m4_terminal_candle.json")
REPO = "thetrademarkk/india-index-options-1m"
UA = {"User-Agent": "Mozilla/5.0 (research audit; anonymous read)"}
KEYS = ["symbol", "expiry", "strike", "option_type", "timestamp"]
COLS = ["timestamp", "open", "high", "low", "close", "volume", "trading_day"]
# The key columns have to be read as well, otherwise drop_duplicates(subset=KEYS) raises.
READ_COLS = list(dict.fromkeys(COLS + KEYS))
# NSE normal session in derivative minutes: 09:15 .. 15:30 inclusive = 376 labels.
IN_RANGE = "09:15"
OUT_RANGE = "15:30"


def get(url: str) -> bytes | str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def official_rows(ymd: str) -> pd.DataFrame | None:
    path = ensure_bhavcopy(ymd)
    if path is None:
        return None
    with zipfile.ZipFile(path) as zf:
        text = zf.read(zf.namelist()[0]).decode("utf-8", "replace")
    frame = pd.read_csv(io.StringIO(text))
    nifty = frame[(frame["TckrSymb"] == "NIFTY") & (frame["FinInstrmTp"] == "IDO")].copy()
    if nifty.empty:
        return pd.DataFrame()
    nifty["strike"] = nifty["StrkPric"].astype(float).round(0).astype("int64")
    nifty["option_type"] = nifty["OptnTp"]
    nifty["expiry"] = pd.to_datetime(nifty["XpryDt"], errors="coerce").dt.strftime("%Y-%m-%d")
    return nifty.drop_duplicates(subset=["strike", "option_type", "expiry"], keep="first")


def documentation(lines: list[str], say) -> None:
    """Quote what the publisher actually says, or record that nothing is said."""
    say("=== 1. SOURCE DOCUMENTATION (fetched anonymously) ===")
    try:
        tree = json.loads(get(f"https://huggingface.co/api/datasets/{REPO}/tree/main"))
        names = [t["path"] for t in tree]
        say(f"  repo root entries ({len(names)}): {names[:12]}")
    except Exception as exc:  # noqa: BLE001
        say(f"  tree listing failed: {type(exc).__name__}: {exc}")
    card = None
    for candidate in ("README.md", "DATASHEET.md", "docs/README.md", "card.md"):
        try:
            raw = get(f"https://huggingface.co/datasets/{REPO}/resolve/main/{candidate}")
        except Exception:  # noqa: BLE001
            continue
        if raw:
            card = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            say(f"  found {candidate}: {len(card)} chars")
            break
    if card is None:
        say("  no dataset card at README.md / DATASHEET.md - the publisher documents nothing")
        return
    keep = []
    for ln in card.splitlines():
        low = ln.lower()
        if any(k in low for k in ("timestamp", "volume", "open_interest", "open interest",
                                  "lot", "minute", "candle", "bar", "session", "ist",
                                  "timezone", "unit", "oi ")):
            keep.append(ln.strip())
    if keep:
        say("  lines that mention timestamp meaning / units / session:")
        for ln in keep[:40]:
            say(f"    | {ln[:160]}")
    else:
        say("  card exists but never mentions timestamp meaning, volume units or OI")
    say("  first 20 raw lines of the card:")
    for ln in card.splitlines()[:20]:
        say(f"    | {ln[:160]}")


def session_shape(day: pd.DataFrame, max_contracts: int = 40) -> dict:
    """Label-set and terminal/opening bucket shape for one contract-day block."""
    d = day.copy()
    d["clock"] = pd.to_datetime(d["timestamp"]).dt.strftime("%H:%M")
    labels = sorted(d["clock"].unique())
    out: dict = {"labels": len(labels), "first_label": labels[0], "last_label": labels[-1],
                 "has_0915": "09:15" in labels, "has_0916": "09:16" in labels,
                 "has_1529": "15:29" in labels, "has_1530": "15:30" in labels}
    # Any label outside the 09:15..15:30 session, and any missing label inside it, makes
    # the session unverifiable under both hypotheses - record it instead of guessing.
    expected = pd.date_range("2026-01-01 09:15", "2026-01-01 15:30", freq="1min").strftime("%H:%M")
    out["outside_session_labels"] = sorted(x for x in labels if not (IN_RANGE <= x <= OUT_RANGE))
    out["missing_in_session_labels"] = len([x for x in expected if x not in set(labels)])
    bar_counts = d.groupby(["strike", "option_type"], observed=True)["clock"].nunique()
    # Only contracts that traded in >=20 minutes have a meaningful "typical minute".
    busy = bar_counts[bar_counts >= 20].nlargest(max_contracts).index
    if len(busy) < 3:
        return out
    dense = d.merge(pd.DataFrame({"strike": [b[0] for b in busy],
                                  "option_type": [b[1] for b in busy]}),
                    on=["strike", "option_type"])
    ratios_first, ratios_last, lengths = [], [], []
    first_degen = last_degen = 0
    for _, grp in dense.groupby(["strike", "option_type"], observed=True):
        grp = grp.sort_values("clock")
        med = float(grp["volume"].median())
        if med <= 0:
            continue
        first, last = grp.iloc[0], grp.iloc[-1]
        lengths.append(len(grp))
        ratios_first.append(float(first["volume"]) / med)
        ratios_last.append(float(last["volume"]) / med)
        first_degen += int(first["open"] == first["high"] == first["low"] == first["close"])
        last_degen += int(last["open"] == last["high"] == last["low"] == last["close"])
    n = len(lengths)
    if n == 0:
        return out
    out.update({
        "n_contracts_used": n,
        "median_bars_per_contract": float(pd.Series(lengths).median()),
        "first_bucket_volume_ratio_median": round(float(pd.Series(ratios_first).median()), 3),
        "last_bucket_volume_ratio_median": round(float(pd.Series(ratios_last).median()), 3),
        "first_bucket_degenerate_pct": round(100.0 * first_degen / n, 1),
        "last_bucket_degenerate_pct": round(100.0 * last_degen / n, 1),
    })
    return out


def closing_print_test(day: pd.DataFrame, off: pd.DataFrame) -> dict:
    """Does the last bucket's close equal the official LastPric, by terminal label?"""
    d = day.copy()
    d["clock"] = pd.to_datetime(d["timestamp"]).dt.strftime("%H:%M")
    last = d.loc[d.groupby(["strike", "option_type"], observed=True)["clock"].idxmax()]
    merged = last.merge(
        off[["strike", "option_type", "LastPric", "ClsPric", "HghPric", "LwPric", "OpnPric"]],
        on=["strike", "option_type"], how="inner")
    if merged.empty:
        return {}
    merged["match_last"] = merged["close"] == pd.to_numeric(merged["LastPric"], errors="coerce")
    merged["match_cls"] = merged["close"] == pd.to_numeric(merged["ClsPric"], errors="coerce")
    out = {
        "contracts": int(len(merged)),
        "last_bucket_close_eq_LastPric_pct": round(100.0 * float(merged["match_last"].mean()), 2),
        "last_bucket_close_eq_ClsPric_pct": round(100.0 * float(merged["match_cls"].mean()), 2),
    }
    # price-information test: is the official day range reachable from the archive range?
    hi = d.groupby(["strike", "option_type"], observed=True)["high"].max()
    lo = d.groupby(["strike", "option_type"], observed=True)["low"].min()
    r = merged.set_index(["strike", "option_type"])
    r["arch_hi"] = hi
    r["arch_lo"] = lo
    o = pd.to_numeric(r["HghPric"], errors="coerce")
    l = pd.to_numeric(r["LwPric"], errors="coerce")
    out["arch_hi_ge_official_hi_pct"] = round(100.0 * float((r["arch_hi"] >= o - 1e-9).mean()), 2)
    out["arch_lo_le_official_lo_pct"] = round(100.0 * float((r["arch_lo"] <= l + 1e-9).mean()), 2)
    return out


def main() -> int:
    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    records: list[dict] = []

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    say("M4 item 3 - terminal candle convention")
    say("=" * 78)
    documentation(lines, say)
    say("")
    say("=== 2-5. EMPIRICAL SESSION SHAPE (per date x expiry file) ===")
    say(f"{'date':11s} {'expiry':11s} {'nlab':>5s} {'first':>6s} {'last':>6s} "
        f"{'frstV/med':>9s} {'lastV/med':>9s} {'degenF%':>7s} {'degenL%':>7s} {'lastPric%':>9s}")

    expiries = sorted(p.stem.replace("options__NIFTY__", "") for p in HF.glob("options__NIFTY__*.parquet"))
    for expiry in expiries:
        path = HF / f"options__NIFTY__{expiry}.parquet"
        frame = pd.read_parquet(path, columns=READ_COLS).drop_duplicates(subset=KEYS, keep="first")
        all_days = sorted(d for d in frame["trading_day"].astype(str).unique() if d >= "2024-01-02")
        if not all_days:
            continue
        # Sample the session: earliest, latest and two middle days.  A convention claim
        # has to hold on both sides of the provider's own 2025 change, not on one day.
        picks = sorted({all_days[0], all_days[-1],
                        all_days[len(all_days) // 3], all_days[2 * len(all_days) // 3]})
        for day_iso in picks:
            ymd = day_iso.replace("-", "")
            off = official_rows(ymd)
            if off is None or off.empty:
                continue
            sub = off[off["expiry"] == expiry]
            if sub.empty:
                continue
            day = frame[frame["trading_day"].astype(str) == day_iso]
            shape = session_shape(day)
            closing = closing_print_test(day, sub)
            rec = {"date": day_iso, "expiry": expiry, **shape, **closing}
            records.append(rec)
            say(f"{day_iso:11s} {expiry:11s} {shape.get('labels',0):5d} "
                f"{shape.get('first_label','-'):>6s} {shape.get('last_label','-'):>6s} "
                f"{shape.get('first_bucket_volume_ratio_median',float('nan')):9.3f} "
                f"{shape.get('last_bucket_volume_ratio_median',float('nan')):9.3f} "
                f"{shape.get('first_bucket_degenerate_pct',float('nan')):7.1f} "
                f"{shape.get('last_bucket_degenerate_pct',float('nan')):7.1f} "
                f"{closing.get('last_bucket_close_eq_LastPric_pct',float('nan')):9.2f}")

    say("")
    say("=== SUMMARY BY TERMINAL LABEL ===")
    ok = [r for r in records if r.get("n_contracts_used")]
    by_last: dict[str, list[dict]] = {}
    for r in ok:
        by_last.setdefault(r["last_label"], []).append(r)
    for label, rows in sorted(by_last.items()):
        med_first = pd.Series([r["first_bucket_volume_ratio_median"] for r in rows]).median()
        med_last = pd.Series([r["last_bucket_volume_ratio_median"] for r in rows]).median()
        degen_f = pd.Series([r["first_bucket_degenerate_pct"] for r in rows]).median()
        degen_l = pd.Series([r["last_bucket_degenerate_pct"] for r in rows]).median()
        nlab = pd.Series([r["labels"] for r in rows]).median()
        has0915 = sum(1 for r in rows if r["has_0915"]) / len(rows) * 100
        say(f"  last label {label}: {len(rows)} sessions, median {nlab:.0f} distinct labels, "
            f"{has0915:.0f}% contain 09:15")
        say(f"      opening bucket: volume/typical {med_first:.3f}, degenerate {degen_f:.1f}%")
        say(f"      terminal bucket: volume/typical {med_last:.3f}, degenerate {degen_l:.1f}%")
        lp = [r.get("last_bucket_close_eq_LastPric_pct") for r in rows if r.get("last_bucket_close_eq_LastPric_pct") is not None]
        if lp:
            say(f"      last bucket close == official LastPric: median {pd.Series(lp).median():.2f}%")
        hi = [r.get("arch_hi_ge_official_hi_pct") for r in rows if r.get("arch_hi_ge_official_hi_pct") is not None]
        if hi:
            say(f"      archive high covers official high: median {pd.Series(hi).median():.2f}%")

    say("")
    say("=== LABEL-SET SIGNATURES ===")
    sig = Counter(
        (r["first_label"], r["last_label"], int(r["labels"])) for r in ok)
    for (f, l, n), count in sig.most_common(12):
        say(f"  first {f} .. last {l}, {n} distinct labels: {count} sessions")
    say(f"  sessions analysed: {len(ok)} of {len(records)} blocks")
    say("")
    say("=== SESSIONS WITH LABELS OUTSIDE 09:15..15:30 (strict-validation rejects) ===")
    stray = [r for r in records if r.get("outside_session_labels")]
    for r in stray:
        say(f"  {r['date']} x {r['expiry']}: {r['outside_session_labels']}")
    say(f"  {len(stray)} of {len(records)} blocks carry out-of-session labels")
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    OUT_JSON.write_text(json.dumps(records, indent=1, default=str), encoding="utf-8")
    say(f"wrote {OUT_TXT.name} and {OUT_JSON.name} ({len(records)} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
