import cProfile
import pstats

import pandas as pd

from nifty_quant.engine.backtest import run_backtest

bars = {"RELIANCE": pd.read_parquet("data/cache/1d/RELIANCE.parquet")}
profiler = cProfile.Profile()
profiler.enable()
run_backtest("orders_01", bars)
profiler.disable()
stats = pstats.Stats(profiler).sort_stats("cumulative")
stats.print_stats(22)
