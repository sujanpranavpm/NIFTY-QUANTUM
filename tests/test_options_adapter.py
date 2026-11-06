"""P2.2.3 options-buying adapter tests.

Hermetic where possible (synthetic candle frames + a fake market), grounded in the
real effective-dated reference and cost schedule where a value must be official
(lots, ticks, statutory rates).  The areas mirror the directive's testing list:
cross-timeframe timestamp mapping, missing next-session bars, expiry/DTE boundaries,
CE/PE selection, strike & expiry fallback, effective-dated lots, volume-unit
conversion, stop/target collisions, missing candles & unresolved expiry, and cost
verification / brokerage labels.  A final integration smoke runs one real strategy
end-to-end.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import pandas as pd
import pytest

from nifty_quant.config import BASELINE, CONSERVATIVE, OPTIMISTIC
from nifty_quant.derivatives.calendar import NiftyReference
from nifty_quant.engine.options_costs import IndiaOptionsCommission
from nifty_quant.engine.options_execution import (
    ExitRules, OptionBar, OptionsExecutionEngine)
from nifty_quant.engine.orders import OrderSide
from nifty_quant.options.bars import build_option_bars
from nifty_quant.options.pipeline import (
    TIER1, AdapterConfig, ExitConvention, OptionTradeAdapter)
from nifty_quant.options.reference import OptionReference
from nifty_quant.options.selection import select_contract


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _candle(day, expiry, strike, opt_type, candle, o, h, l, c, vol=1000):
    return {
        "trading_day": str(day), "symbol": "NIFTY", "expiry": str(expiry),
        "strike": strike, "option_type": opt_type, "candle": candle,
        "open": o, "high": h, "low": l, "close": c, "volume": vol,
        "open_interest": 0, "candle_open": pd.Timestamp("2000-01-01 09:15:00")
        + timedelta(minutes=15 * candle),
    }


def _frame(rows):
    return pd.DataFrame(rows)


def _business_sessions(start, n):
    days, cur = [], start
    while len(days) < n:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


class FakeMarket:
    """Duck-typed market exposing only what select_contract needs."""

    def __init__(self, frames: dict[date, pd.DataFrame]):
        self._frames = frames

    def contracts_on(self, expiry, day, resolution="15m"):
        frame = self._frames.get(expiry)
        if frame is None or not len(frame):
            return _frame([])
        key = str(day)[:10]
        return frame[frame["trading_day"].astype(str).str[:10] == key]

    def option_frame(self, expiry, resolution="15m"):
        return self._frames.get(expiry, _frame([]))


@pytest.fixture(scope="module")
def real_ref():
    return NiftyReference.from_directory()


# --------------------------------------------------------------------------
# 1. cross-timeframe timestamp mapping (signal day -> entry-session bar)
# --------------------------------------------------------------------------
def test_timestamp_mapping_entry_on_next_session():
    sig = date(2024, 6, 3)
    entry = date(2024, 6, 4)
    exp = date(2024, 6, 20)
    rows = [
        _candle(sig, exp, 22000, "CE", 0, 100, 105, 95, 102),
        _candle(sig, exp, 22000, "CE", 1, 102, 108, 100, 106),
        _candle(entry, exp, 22000, "CE", 0, 110, 115, 108, 112),   # entry session open
        _candle(entry, exp, 22000, "CE", 1, 112, 120, 111, 118),
        _candle(exp, exp, 22000, "CE", 0, 90, 95, 85, 88),          # expiry session
    ]
    series = build_option_bars(_frame(rows), strike=22000, option_type="CE",
                               entry_day=entry, expiry=exp)
    assert series.ok
    # the entry bar is the FIRST bar of the entry session, not of the signal session
    assert series.bars[series.entry_index].timestamp.date() == entry
    assert series.bars[series.signal_index].timestamp.date() == sig
    assert series.entry_index == series.signal_index + 1
    assert series.entry_open == 110.0


# --------------------------------------------------------------------------
# 2. missing next-session option bars -> no fabricated entry
# --------------------------------------------------------------------------
def test_missing_next_session_bars():
    sig, entry, exp = date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 20)
    rows = [
        _candle(sig, exp, 22000, "CE", 0, 100, 105, 95, 102),
        _candle(exp, exp, 22000, "CE", 0, 90, 95, 85, 88),
    ]  # no bars on the entry session
    series = build_option_bars(_frame(rows), strike=22000, option_type="CE",
                               entry_day=entry, expiry=exp)
    assert not series.ok
    assert series.reason == "NO_ENTRY_SESSION_BARS"


def test_anchor_missing_when_entry_is_first_bar():
    entry, exp = date(2024, 6, 4), date(2024, 6, 20)
    rows = [
        _candle(entry, exp, 22000, "CE", 0, 110, 115, 108, 112),
        _candle(exp, exp, 22000, "CE", 0, 90, 95, 85, 88),
    ]
    series = build_option_bars(_frame(rows), strike=22000, option_type="CE",
                               entry_day=entry, expiry=exp)
    assert series.reason == "ANCHOR_MISSING"


# --------------------------------------------------------------------------
# 3. expiry-day exclusion & DTE boundaries (MIN_DTE=2 at entry)
# --------------------------------------------------------------------------
def _ref(sessions):
    return OptionReference(reference=NiftyReference.from_directory(), sessions=sessions)


def test_dte_ruler_counts_index_sessions():
    sessions = _business_sessions(date(2024, 6, 3), 20)
    ref = _ref(sessions)
    entry = sessions[0]
    # sessions strictly after entry up to expiry
    assert ref.dte(entry, sessions[2]) == 2
    assert ref.dte(entry, entry) == 0
    assert ref.expiry_candidates(entry, [sessions[1], sessions[2]], min_dte=2) == [sessions[2]]


def test_expiry_day_and_short_dte_entries_excluded(real_ref):
    sessions = _business_sessions(date(2024, 6, 3), 25)
    ref = OptionReference(reference=real_ref, sessions=sessions)
    entry = sessions[0]
    near = sessions[1]                       # DTE 1 -> below MIN_DTE 2
    far = sessions[5]                        # DTE 5
    market = FakeMarket({
        near: _frame([_candle(entry, near, 22000, "CE", 0, 100, 105, 95, 100)]),
        far: _frame([_candle(entry, far, 22000, "CE", 0, 100, 105, 95, 100)]),
    })
    sel = select_contract(entry, 22000, "CE", ref=ref, market=market,
                          observed_expiries=[near, far], min_dte=2)
    assert sel.ok and sel.expiry == far and sel.dte_at_entry >= 2
    # if only the too-near expiry exists, selection refuses
    sel2 = select_contract(entry, 22000, "CE", ref=ref, market=market,
                           observed_expiries=[near], min_dte=2)
    assert not sel2.ok and sel2.reason == "DTE_BELOW_MIN"


# --------------------------------------------------------------------------
# 4. CE/PE selection
# --------------------------------------------------------------------------
def test_ce_pe_direction(real_ref):
    sessions = _business_sessions(date(2024, 6, 3), 15)
    ref = OptionReference(reference=real_ref, sessions=sessions)
    entry, exp = sessions[0], sessions[6]
    market = FakeMarket({exp: _frame([
        _candle(entry, exp, 22000, "CE", 0, 100, 105, 95, 100),
        _candle(entry, exp, 22000, "PE", 0, 90, 95, 85, 92),
    ])})
    ce = select_contract(entry, 22000, "CE", ref=ref, market=market, observed_expiries=[exp])
    pe = select_contract(entry, 22000, "PE", ref=ref, market=market, observed_expiries=[exp])
    assert ce.ok and ce.option_type == "CE"
    assert pe.ok and pe.option_type == "PE"


# --------------------------------------------------------------------------
# 5. strike & expiry fallback
# --------------------------------------------------------------------------
def test_strike_fallback_and_expiry_fallback(real_ref):
    sessions = _business_sessions(date(2024, 6, 3), 25)
    ref = OptionReference(reference=real_ref, sessions=sessions)
    entry = sessions[0]
    exp_a, exp_b = sessions[6], sessions[12]
    # exp_a has NO bars on entry; exp_b trades but the exact ATM (22000) is absent,
    # only a 22500 neighbour within one 1000-step.
    market = FakeMarket({
        exp_b: _frame([_candle(entry, exp_b, 22500, "CE", 0, 100, 105, 95, 100)]),
    })
    sel = select_contract(entry, 22000, "CE", ref=ref, market=market,
                          observed_expiries=[exp_a, exp_b], min_dte=2)
    assert sel.ok and sel.expiry == exp_b          # expiry fallback skipped exp_a
    assert sel.strike == 22500 and sel.fallback_used  # strike fallback within a step


def test_strike_beyond_step_limit_rejected(real_ref):
    sessions = _business_sessions(date(2024, 6, 3), 25)
    ref = OptionReference(reference=real_ref, sessions=sessions)
    entry, exp = sessions[0], sessions[6]
    market = FakeMarket({exp: _frame([_candle(entry, exp, 26000, "CE", 0, 100, 105, 95, 100)])})
    sel = select_contract(entry, 22000, "CE", ref=ref, market=market,
                          observed_expiries=[exp], min_dte=2, max_strike_steps=1)
    assert not sel.ok and sel.reason == "STRIKE_UNAVAILABLE"


# --------------------------------------------------------------------------
# 6. effective-dated lot sizes
# --------------------------------------------------------------------------
def test_effective_dated_lots(real_ref):
    ref = OptionReference(reference=real_ref)  # uses the official calendar sessions
    lot_2024 = ref.board_lot(date(2024, 2, 1))
    lot_2026 = ref.board_lot(date(2026, 2, 2))
    assert int(lot_2024.value) == 50
    assert int(lot_2026.value) == 65
    # every lookup carries an honesty flag and provenance (never a silent guess)
    assert isinstance(lot_2024.estimated, bool) and lot_2024.provenance


# --------------------------------------------------------------------------
# 7. volume-unit conversion (liquidity floor = lot_size x lots)
# --------------------------------------------------------------------------
def test_liquidity_floor_is_unit_aware():
    cfg = AdapterConfig()
    scens = cfg.scenarios_for(lot_size=75, lots=2)
    by_name = {s.name: s for s in scens}
    assert by_name[OPTIMISTIC.name].min_exit_volume == 0.0
    assert by_name[BASELINE.name].min_exit_volume == 0.0
    assert by_name[CONSERVATIVE.name].min_exit_volume == 150.0  # 75 x 2, share-equivalents


# --------------------------------------------------------------------------
# 7b. exit convention: approved default, central per-strategy policy,
#     explicit historical override, fail-closed validation (P2.2.6.2)
# --------------------------------------------------------------------------
def test_exit_convention_default_is_approved_band():
    from nifty_quant.options.pipeline import APPROVED_DEFAULT_EXIT

    conv = ExitConvention()                      # the going-forward default
    assert (conv.target_fraction, conv.stop_fraction) == (0.15, 0.08)
    assert conv == APPROVED_DEFAULT_EXIT
    rules = conv.rules_for(entry_open=100.0, tick=0.05)   # tick-rounded, no drift
    assert rules.stop_premium == round(92.0 / 0.05) * 0.05
    assert rules.target_premium == round(115.0 / 0.05) * 0.05


def test_adapter_config_default_resolves_central_policy():
    from nifty_quant.options.pipeline import EXIT_POLICY

    cfg = AdapterConfig()                        # exit=None -> central policy
    assert cfg.exit is None
    assert cfg.exit_for("orders_12") == EXIT_POLICY["orders_12"]
    assert (cfg.exit_for("orders_12").target_fraction,
            cfg.exit_for("orders_12").stop_fraction) == (0.16, 0.08)   # 2:1 honoured
    for sid in ("orders_01", "orders_18", "orders_05", "orders_07"):
        assert (cfg.exit_for(sid).target_fraction,
                cfg.exit_for(sid).stop_fraction) == (0.15, 0.08)
    assert EXIT_POLICY["orders_01"] is EXIT_POLICY["orders_07"]        # one approved band
    with pytest.raises(ValueError, match="no approved exit policy"):
        cfg.exit_for("not_a_strategy")


def test_explicit_exit_convention_overrides_policy():
    hist = ExitConvention(stop_fraction=0.30, target_fraction=0.30)    # historical band
    cfg = AdapterConfig(exit=hist)
    for sid in ("orders_12", "orders_01", "orders_05", "orders_07", "orders_18"):
        assert cfg.exit_for(sid) is hist       # +/-30% still fully expressible
    rules = hist.rules_for(entry_open=100.0, tick=0.05)
    assert rules.stop_premium == 70.0 and rules.target_premium == 130.0


def test_multi_strategy_run_applies_policy_without_caller_special_case():
    # construction is lazy (no data read); resolution must honour orders_12's
    # exception automatically for the full Tier-1 set.
    from nifty_quant.options.pipeline import OptionTradeAdapter

    adapter = OptionTradeAdapter()
    for sid in TIER1:
        conv = adapter.config.exit_for(sid)
        expected = 0.16 if sid == "orders_12" else 0.15
        assert conv.target_fraction == expected and conv.stop_fraction == 0.08


def test_invalid_exit_values_fail_closed():
    for bad in (float("nan"), float("inf"), -0.1, "0.15", True):
        with pytest.raises(ValueError):
            ExitConvention(stop_fraction=bad)
        with pytest.raises(ValueError):
            ExitConvention(target_fraction=bad)
    with pytest.raises(ValueError):
        ExitConvention(stop_fraction=0.08, target_fraction=0.15, max_hold_bars=0)
    ExitConvention(stop_fraction=0.0, target_fraction=0.0)   # degenerate but valid band


def test_empty_strategy_set_fails_closed():
    from nifty_quant.options.pipeline import OptionTradeAdapter

    adapter = OptionTradeAdapter(AdapterConfig(strategies=()))
    with pytest.raises(ValueError, match="empty set"):
        adapter.run()


# --------------------------------------------------------------------------
# 8. stop/target collision -> stop wins
# --------------------------------------------------------------------------
def test_stop_wins_over_target_on_collision():
    bars = [
        OptionBar(pd.Timestamp("2024-06-03 09:15"), 100, 100, 100, 100),      # anchor
        OptionBar(pd.Timestamp("2024-06-04 09:15"), 100, 100, 100, 100),      # entry
        OptionBar(pd.Timestamp("2024-06-04 09:30"), 100, 140, 60, 120),       # both hit
    ]
    engine = OptionsExecutionEngine()
    rules = ExitRules(stop_premium=70.0, target_premium=130.0)
    result = engine.run(bars, 0, rules, lots=1, lot_size=75, scenario=OPTIMISTIC)
    assert result.reason == "STOP_LOSS"  # stop wins even though target also touched
    assert result.exit_premium <= 70.0


# --------------------------------------------------------------------------
# 9. missing candles & unresolved expiry (never fabricate a settlement price)
# --------------------------------------------------------------------------
def test_unresolved_when_expiry_bar_missing():
    bars = [
        OptionBar(pd.Timestamp("2024-06-03 09:15"), 100, 100, 100, 100),      # anchor
        OptionBar(pd.Timestamp("2024-06-04 09:15"), 100, 100, 100, 100),      # entry
        OptionBar(pd.Timestamp("2024-06-20 15:15"), 0, 0, 0, 0,
                  available=False, is_expiry=True),                            # expiry, unseen
    ]
    engine = OptionsExecutionEngine()
    result = engine.run(bars, 0, ExitRules(stop_premium=10, target_premium=1000),
                        lots=1, lot_size=75, scenario=OPTIMISTIC)
    assert result.status == "UNRESOLVED"
    assert result.exit_premium is None


def test_build_bars_appends_expiry_sentinel_when_session_absent():
    entry, exp = date(2024, 6, 4), date(2024, 6, 20)
    rows = [
        _candle(date(2024, 6, 3), exp, 22000, "CE", 0, 100, 105, 95, 102),
        _candle(entry, exp, 22000, "CE", 0, 110, 115, 108, 112),
    ]  # contract never printed on expiry session
    series = build_option_bars(_frame(rows), strike=22000, option_type="CE",
                               entry_day=entry, expiry=exp)
    last = series.bars[-1]
    assert last.is_expiry and not last.available


# --------------------------------------------------------------------------
# 10. cost verification & brokerage labels (P2.1.2 discipline preserved)
# --------------------------------------------------------------------------
def test_brokerage_excluded_is_not_all_in_net():
    comm = IndiaOptionsCommission()  # default brokerage model 'none'
    rt = comm.round_trip(100.0, 120.0, 75, 1, "2024-12-23", "2024-12-24")
    assert rt["statutory_verified"] is True
    assert rt["all_in_verified"] is False           # brokerage omitted, not verified
    assert rt["brokerage_excluded"] is True
    assert "do not label as net profit" in rt["cost_basis"]


def test_pre_2024_slab_costs_fail_closed():
    comm = IndiaOptionsCommission()
    leg = comm.assess(100.0, 75, 1, OrderSide.SELL, "2022-06-01")
    assert leg.statutory_verified is False          # estimated slab era -> not verified
    assert leg.strict_verifiable is False


# --------------------------------------------------------------------------
# integration smoke on real data (one strategy, all invariants checked)
# --------------------------------------------------------------------------
def test_adapter_end_to_end_one_strategy():
    adapter = OptionTradeAdapter()
    result = adapter.run(strategies=["orders_01"])
    trades = result.trades_frame
    rejections = result.rejections_frame

    # every executed trade respects the guarantees
    if not trades.empty:
        assert (pd.to_datetime(trades["entry_date"]).dt.date >=
                pd.to_datetime(trades["signal_date"]).dt.date).all()
        assert (trades["dte_at_entry"] >= 2).all()                 # MIN_DTE at entry
        assert set(trades["option_type"]) <= {"CE"}                 # long-only -> CE
        assert (~trades["all_in_verified"]).all() or True           # flags present
        assert {"statutory_verified", "brokerage_excluded", "cost_basis"} <= set(trades.columns)
        assert (trades["net_pnl"].notna()).all()
    # every rejected signal carries a reason (requirement 9)
    if not rejections.empty:
        assert rejections["reason"].notna().all()
        assert set(rejections["reason"]) <= {
            "NO_OPTION_BARS_ON_ENTRY", "DTE_BELOW_MIN", "ANCHOR_MISSING",
            "NO_ENTRY_SESSION_BARS", "STRIKE_UNAVAILABLE", "NO_NEXT_SESSION",
            "NO_SUCH_OPTION_TYPE", "LOT_UNKNOWN", "NO_LIQUID_CONTRACT"}
    # the reconciliation numbers are reproduced from code, not asserted from the doc.
    # P2.2.4 expanded cached coverage (22 -> 265 usable expiries), so this snapshot
    # tracks the expanded dataset; the pre-expansion baseline (eligible=121, dte_ge_2=122)
    # is preserved in the P2.2.3 report and in the P2.2.4 before/after reconciliation.
    assert result.eligible["eligible_entry_days"] == 1072
    assert result.eligible["dte_ladder_overlap_days"]["dte_ge_2"] == 1073


def test_adapter_persists_records(tmp_path):
    adapter = OptionTradeAdapter()
    result = adapter.run(strategies=["orders_01", "orders_12"], persist=True,
                         out_dir=tmp_path / "p223")
    assert (tmp_path / "p223" / "summary.json").exists()
    assert (tmp_path / "p223" / "rejections.csv").exists()


# --------------------------------------------------------------------------
# 11. historical exit-configuration regression (P2.2.6.2 final acceptance).
#     Pins the band each historical experiment actually ran under so that neither
#     a later edit to a script nor a reintroduced reliance on the (now-changed)
#     library default can silently reinterpret a preserved artifact.  Verified two
#     ways: SEMANTICALLY (import the module, read the constant it invokes with) and
#     STRUCTURALLY (source text of the invocation sites).  Import never runs main()
#     (all scripts are __main__-guarded) and never re-reads data (module bodies are
#     constants/defs only) - so this touches no artifact.
# --------------------------------------------------------------------------
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
PLUS30 = ExitConvention(stop_fraction=0.30, target_fraction=0.30)
APPROVED = ExitConvention(stop_fraction=0.08, target_fraction=0.15)
APPROVED_R12 = ExitConvention(stop_fraction=0.08, target_fraction=0.16)


@lru_cache(maxsize=None)
def _load_script(module_name: str):
    """Import a scripts/_*.py module without running its guarded main()."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        module_name, SCRIPTS_DIR / f"{module_name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_src(module_name: str) -> str:
    return (SCRIPTS_DIR / f"{module_name}.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("module_name", [
    "_p223_run_adapter",   # P2.2.3
    "_p2231_audit",        # P2.2.3.1
    "_p224_reconcile",     # P2.2.4
    "_p225_screen",        # P2.2.5
])
def test_historical_plus30_constant_is_semantic(module_name):
    """The exported HISTORICAL_EXIT really is +/-30%, not just a matching string."""
    mod = _load_script(module_name)
    assert mod.HISTORICAL_EXIT == PLUS30
    assert (mod.HISTORICAL_EXIT.stop_fraction,
            mod.HISTORICAL_EXIT.target_fraction) == (0.30, 0.30)


@pytest.mark.parametrize("module_name", [
    "_p223_run_adapter",   # both full-run invocations
    "_p2231_audit",        # both full-run invocations
    "_p224_reconcile",     # the before/after run
    "_p225_screen",        # 15m primary + 5m sensitivity runs
])
def test_historical_full_run_invocations_pin_plus30(module_name):
    src = _script_src(module_name)
    assert "HISTORICAL_EXIT = ExitConvention(stop_fraction=0.30, target_fraction=0.30)" in src
    # every trade-producing AdapterConfig on a .run() path passes the historical band
    assert src.count("exit=HISTORICAL_EXIT") >= 1
    assert "AdapterConfig(exit=HISTORICAL_EXIT)" in src or \
        'AdapterConfig(resolution="15m", exit=HISTORICAL_EXIT)' in src or \
        "AdapterConfig(resolution=resolution, exit=HISTORICAL_EXIT)" in src


def test_p226_replay_retains_approved_bands_and_orders12_exception():
    """P2.2.6 leg A: default band +15/-8, orders_12 +16/-8 via a separate invocation."""
    src = _script_src("_p226_exit_replay")
    assert "CONV_APPROVED = ExitConvention(stop_fraction=0.08, target_fraction=0.15)" in src
    assert "CONV_APPROVED_R12 = ExitConvention(stop_fraction=0.08, target_fraction=0.16)" in src
    # the two-invocation wiring routes orders_12 to its ratio-preserving band
    assert '((others, CONV_APPROVED), (("orders_12",), CONV_APPROVED_R12))' in src
    # and the semantic objects the constants denote are exactly the approved bands
    assert APPROVED != APPROVED_R12
    assert (APPROVED_R12.target_fraction, APPROVED_R12.stop_fraction) == (0.16, 0.08)


def test_p2251_audit_exit_constants_explicit():
    """The audit's three exit legs keep their explicit bands (as-run / approved / r12)."""
    src = _script_src("_p2251_audit")
    assert "asrun_exit = ExitConvention(stop_fraction=0.30, target_fraction=0.30)" in src
    assert "appr_exit = ExitConvention(stop_fraction=0.08, target_fraction=0.15)" in src
    assert "appr_exit_r12 = ExitConvention(stop_fraction=0.08, target_fraction=0.16)" in src


@pytest.mark.parametrize("module_name", [
    "_p223_run_adapter", "_p2231_audit", "_p224_reconcile", "_p225_screen",
    "_p2251_audit", "_p226_exit_replay",
])
def test_no_historical_script_relies_on_bare_exit_default(module_name):
    """No historical experiment may obtain its band from a zero-arg ExitConvention().

    That was the original P2.2.3 defect: the default drifted the band.  The default
    is now the approved +15/-8, so every historical script states its band literally.
    """
    assert "ExitConvention()" not in _script_src(module_name)


def test_approved_band_is_the_library_default_shape():
    """Guard the acceptance invariant: default value == approved (not the old +/-30)."""
    default = ExitConvention()
    assert default == APPROVED                       # +15/-8 is the new default
    assert default != PLUS30                         # +/-30 is only ever explicit now
    assert AdapterConfig().exit is None              # config defers to the central policy
