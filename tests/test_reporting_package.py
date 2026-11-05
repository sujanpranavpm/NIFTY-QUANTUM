"""P2 stage 7 - reporting-package behaviour (directive Part 9).

Reporting is a *view*: nothing here recomputes a statistic, so the same frame rendered
to a table, a label or a CSV cannot disagree.  These tests pin the three guarantees that
make that true - a value is formatted by its column's declared kind, ``format_frame``
returns a rendered copy and never mutates its source, and ``write_frame`` writes nothing
for an empty frame (an absent file is the honest "nothing was computed here").
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from nifty_quant.reporting import format_frame, format_value, write_frame
from nifty_quant.reporting.formatting import MISSING, header_label, is_money, is_percent


# ---------------------------------------------------------------------------
# format_value - the single place a number becomes a string
# ---------------------------------------------------------------------------
def test_a_percent_column_is_rendered_as_a_percentage():
    assert format_value(0.1234, "total_return") == "12.3%"
    assert is_percent("total_return") and is_percent("max_drawdown")


def test_a_money_column_is_rendered_with_rupee_style_grouping():
    assert format_value(100000.0, "final_equity") == "100,000"
    assert is_money("final_equity")


def test_a_count_and_a_ratio_use_their_own_precision():
    assert format_value(1234.0, "n_trades") == "1,234"
    assert format_value(1.234, "sharpe") == "1.23"


@pytest.mark.parametrize("value", [None, float("nan"), math.inf, -math.inf, pd.NaT])
def test_a_missing_or_unusable_number_becomes_the_missing_glyph(value):
    assert format_value(value, "sharpe") == MISSING


def test_non_numeric_values_pass_through_unchanged():
    assert format_value("orders_01", "strategy_id") == "orders_01"
    assert format_value(True, "any") == "yes"
    assert format_value(False, "any") == "no"
    assert format_value(pd.Timestamp("2026-01-05 09:15:00"), "day") == "2026-01-05"


def test_an_unclassified_float_falls_back_to_four_significant_digits():
    assert format_value(3.14159, "") == "3.142"


def test_header_labels_advertise_their_units():
    assert header_label("final_equity").endswith("(₹)")
    assert header_label("total_return").endswith("(%)")
    assert header_label("strategy_id") == "strategy id"


# ---------------------------------------------------------------------------
# format_frame - a rendered copy, never a mutated source
# ---------------------------------------------------------------------------
def test_format_frame_does_not_mutate_the_source_frame():
    source = pd.DataFrame([{"strategy_id": "orders_01", "total_return": 0.1234,
                            "final_equity": 100000.0}])
    rendered = format_frame(source)
    # the source keeps its numeric dtypes; only the returned copy is text
    assert source["total_return"].dtype.kind == "f"
    assert source["final_equity"].dtype.kind == "f"
    assert set(rendered.columns) >= {"total return (%)", "final equity (₹)", "strategy id"}


def test_format_frame_drops_columns_that_were_entirely_missing():
    source = pd.DataFrame([{"strategy_id": "orders_01", "all_missing": float("nan"),
                            "n_trades": 12}])
    rendered = format_frame(source)
    assert "all missing" not in rendered.columns
    assert "strategy id" in rendered.columns      # kept even though single-row


def test_format_frame_of_an_empty_input_is_empty():
    assert format_frame(pd.DataFrame()).empty
    assert format_frame(None).empty


# ---------------------------------------------------------------------------
# write_frame - nothing computed means nothing written
# ---------------------------------------------------------------------------
def test_write_frame_writes_no_file_for_an_empty_frame(tmp_path):
    path = tmp_path / "empty.csv"
    assert write_frame(pd.DataFrame(), path) is None
    assert not path.exists()


def test_write_frame_writes_a_reloaded_round_trip(tmp_path):
    path = tmp_path / "nested" / "metrics.csv"
    frame = pd.DataFrame([{"strategy_id": "orders_01", "sharpe": 1.5}])
    written = write_frame(frame, path)
    assert written == path and path.exists()
    reloaded = pd.read_csv(path)
    assert reloaded.loc[0, "strategy_id"] == "orders_01"
    assert float(reloaded.loc[0, "sharpe"]) == pytest.approx(1.5)
