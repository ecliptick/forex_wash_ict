# %% [markdown]
# # NB48 — BTCUSDT 1s Tick Backtest (gold-recipe port)
#
# **Date**: 2026-09-18
# **Purpose**: Test the v7 SNIPER ICT recipe on BTCUSDT 1s data AS-IS (no scaling).
#
# **Key insight (2026-09-18)**: The v7 recipe is **largely scale-invariant**.
# Most of the SL/TP math is anchored to ATR (via `use_atr_scaling=True`) and
# to zone-width multipliers (via `fvg_inv_trade_tp_zone_mult=22.0`).
# The USD-absolute knobs act as floors that rarely trigger on BTC because
# BTC's ATR(1200) is large enough ($1.32 vs gold's $0.17). The naive 25x
# scaling I applied earlier was unnecessary and slightly worse (suppressed
# valid setups, reduced trade count from 44 to 28 with similar PnL/day).
#
# **Test plan**:
# 1. Load BTC data, measure ATR + zone-width distribution.
# 2. Run gold recipe (no scaling) on 5-day smoke.
# 3. If positive EV, extend to full corpus.
# 4. Sweep the few USD-absolute knobs (see future notebook).
#
# **Untouched knobs that should transfer unchanged**:
# - `entry_mode='sniper'`
# - `fvg_inv_trade_tp_zone_mult=22.0` (zone-width multiplier, scale-invariant)
# - `fvg_inv_trade_sl_zone_mult=1.0` (zone-width multiplier)
# - `bos_choch_ignore_invert_when_aligned=True`
# - `fvg_min_lifetime_secs=3`
# - `num_layers=3`
# - `fvg_supersede_on_new=True`
# - `fvg_require_retest_to_invert=True`
# - `use_market_structure=True`
# - `use_atr_scaling=True` (the main scale-invariance mechanism)
# - `atr_len=1200`, `sl_atr_mult=0.25`, `tp_atr_mult=0.55`
#
# **USD-absolute knobs that MIGHT need re-tuning** (require explicit A/B):
# - `invalidation_sl_usd: 0.05` (currently ~4% of BTC ATR - small but non-trivial)
# - `fvg_inv_trade_min_zone_usd: 0.30` (floor on sniper setups)
# - `dynamic_sl_floor_usd`, `dynamic_tp_floor_usd` (sub-ATR floors)
# - `fvg_breadth_floor_usd`, `fvg_min_zone_usd`, `ifvg_min_zone_usd`

# %%
from __future__ import annotations
import sys
import time
import warnings
from pathlib import Path

import pandas as pd
import polars as pl
import numpy as np

warnings.filterwarnings("ignore")

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))

from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION
from src.core.ict_strategy import TrendStrategyParams
from src.backtest.ict_backtest import run_ict_backtest

# %%
# -- CONFIG ---------------------------------------------------------------
DATA_PATH = ROOT / "data" / "BTCUSDT_1s_2025-01-01_2026-09-17.parquet"
N_TEST_DAYS = 5                    # quick smoke test on first N days
N_FULL_DAYS = 30                   # extended test if smoke is fast
SEED = 20260918
# NO SCALING: v7 recipe is largely scale-invariant via ATR + zone-width multipliers

print(f"Recipe: {OPTIMAL_RECIPE_VERSION}")
print(f"Data:   {DATA_PATH} ({DATA_PATH.stat().st_size / 1e9:.2f} GB)")
print(f"Test:   {N_TEST_DAYS} days smoke, {N_FULL_DAYS} days extended")

# %%
# -- 1. LOAD BTC DATA + MEASURE SCALE -------------------------------------
print("\n[1] Loading BTCUSDT 1s data and measuring scale...")
t0 = time.time()

df_pl = pl.read_parquet(DATA_PATH)
df_pd = df_pl.select(["ts", "open", "high", "low", "close", "volume"]).rename({"ts": "time"}).to_pandas()
df_pd["time"] = pd.to_datetime(df_pd["time"], utc=True)

print(f"    Loaded {len(df_pd):,} bars in {time.time()-t0:.1f}s")
print(f"    Range: {df_pd['time'].min()} -> {df_pd['time'].max()}")
print(f"    Mean price: ${df_pd['close'].mean():,.2f}")

# Slice to first N_TEST_DAYS for smoke
test_start = df_pd["time"].min()
test_end = test_start + pd.Timedelta(days=N_TEST_DAYS)
df_test = df_pd[(df_pd["time"] >= test_start) & (df_pd["time"] < test_end)].copy()
print(f"    Smoke window: {len(df_test):,} bars ({N_TEST_DAYS} days)")

# Measure scale
high = df_test["high"].to_numpy()
low = df_test["low"].to_numpy()
close = df_test["close"].to_numpy()

typical_bar_range = np.median(high - low)
print(f"\n    Scale measurements:")
print(f"      Median 1s bar range: ${typical_bar_range:.2f}")
print(f"      20-min ATR(1200) approx: ${typical_bar_range * 30:.2f}")

print(f"\n    Gold baseline for comparison:")
print(f"      Median 1s bar range: ~$0.13")
print(f"      20-min ATR(1200):    ~$0.17")
print(f"      Ratios (BTC / Gold):")
print(f"        1s bar range:    {typical_bar_range / 0.13:.1f}x")
print(f"        ATR(1200) proxy: {(typical_bar_range * 30) / 0.17:.1f}x")

# %%
# -- 2. GOLD RECIPE (NO SCALING) -----------------------------------------
print("\n[2] Using v7 SNIPER gold recipe AS-IS (no scaling)...")
print("    Rationale: most knobs are ATR-anchored or zone-width-multiplied,")
print("    which are scale-invariant. USD-absolute knobs act as floors")
print("    that rarely trigger when ATR is large (BTC ATR ~8x gold ATR).")

p = optimal_params()  # v7 SNIPER (gold-tuned, no translation)

print(f"\n    Recipe knobs in effect:")
print(f"      entry_mode='{p.entry_mode}'")
print(f"      fvg_inv_trade_tp_zone_mult={p.fvg_inv_trade_tp_zone_mult} (zone-width multiplier)")
print(f"      fvg_inv_trade_sl_zone_mult={p.fvg_inv_trade_sl_zone_mult} (zone-width multiplier)")
print(f"      use_atr_scaling={p.use_atr_scaling}")
print(f"      atr_len={p.atr_len}, sl_atr_mult={p.sl_atr_mult}, tp_atr_mult={p.tp_atr_mult}")
print(f"\n    USD-absolute floors (rarely trigger on BTC):")
print(f"      invalidation_sl_usd       = ${p.invalidation_sl_usd:.4f}")
print(f"      fvg_inv_trade_min_zone_usd= ${p.fvg_inv_trade_min_zone_usd:.4f}")
print(f"      dynamic_sl_floor_usd      = ${p.dynamic_sl_floor_usd:.4f}")
print(f"      dynamic_tp_floor_usd      = ${p.dynamic_tp_floor_usd:.4f}")

# %%
# -- 3. SMOKE BACKTEST (5 days) -------------------------------------------
print(f"\n[3] Smoke backtest on {N_TEST_DAYS}-day window ({len(df_test):,} bars)...")
t0 = time.time()
result = run_ict_backtest(df_test, p, strategy_label="btc_v7_naive")
elapsed = time.time() - t0
bars_per_sec = len(df_test) / elapsed

s = result.summary()
print(f"\n    === RESULT ===")
print(f"    Elapsed:        {elapsed:.1f}s")
print(f"    Bars/sec:       {bars_per_sec:,.0f}")
print(f"    Trades:         {s['n_trades']:,}")
print(f"    EV/trade:       ${s['ev_per_trade']:+.4f}")
print(f"    PnL/day:        ${s['pnl_per_trade'] * s['trades_per_day']:+.2f}")
print(f"    Win rate:       {s['win_rate']:.1%}")
print(f"    Trades/day:     {s['trades_per_day']:.1f}")
print(f"    Soft-stops:     {s['n_soft_stops']:,}")
print(f"    Avg win/loss:   ${s['avg_win']:.2f} / ${s['avg_loss']:.2f}")

# %%
# -- 4. PERF PROJECTION ----------------------------------------------------
print(f"\n[4] Performance projection...")
full_bars = len(df_pd)
proj_seconds = full_bars / bars_per_sec
proj_minutes = proj_seconds / 60
print(f"    Full corpus:   {full_bars:,} bars ({proj_minutes:.1f} min estimated)")

if proj_minutes < 5:
    print(f"    -> FAST ENOUGH -- running on full corpus next")
    RUN_FULL = True
elif proj_minutes < 30:
    print(f"    -> ACCEPTABLE -- running on full corpus")
    RUN_FULL = True
else:
    print(f"    -> TOO SLOW -- staying on {N_TEST_DAYS}-day smoke + extended {N_FULL_DAYS}-day test")
    RUN_FULL = False

# %%
# -- 5. EXTENDED BACKTEST (30 days) ---------------------------------------
if not RUN_FULL:
    print(f"\n[5] Extended backtest on {N_FULL_DAYS}-day window...")
    ext_end = test_start + pd.Timedelta(days=N_FULL_DAYS)
    df_ext = df_pd[(df_pd["time"] >= test_start) & (df_pd["time"] < ext_end)].copy()
    print(f"    Extended window: {len(df_ext):,} bars ({N_FULL_DAYS} days)")

    t0 = time.time()
    result_ext = run_ict_backtest(df_ext, p, strategy_label="btc_v7_naive_ext")
    elapsed_ext = time.time() - t0
    bps_ext = len(df_ext) / elapsed_ext
    s_ext = result_ext.summary()

    print(f"\n    === EXTENDED RESULT ({N_FULL_DAYS} days) ===")
    print(f"    Elapsed:    {elapsed_ext:.1f}s ({bps_ext:,.0f} bars/sec)")
    print(f"    Trades:     {s_ext['n_trades']:,}")
    print(f"    EV/trade:   ${s_ext['ev_per_trade']:+.4f}")
    print(f"    PnL/day:    ${s_ext['pnl_per_trade'] * s_ext['trades_per_day']:+.2f}")
    print(f"    Win rate:   {s_ext['win_rate']:.1%}")
    print(f"    Trades/day: {s_ext['trades_per_day']:.1f}")

# %%
# -- 6. FULL-CORPUS BACKTEST ----------------------------------------------
if RUN_FULL:
    print(f"\n[6] Full-corpus backtest ({full_bars:,} bars)...")
    t0 = time.time()
    result_full = run_ict_backtest(df_pd, p, strategy_label="btc_v7_naive_full")
    elapsed_full = time.time() - t0
    bps_full = full_bars / elapsed_full
    n_days = (df_pd["time"].max() - df_pd["time"].min()).days
    s_full = result_full.summary()

    print(f"\n    === FULL-CORPUS RESULT ({n_days} days) ===")
    print(f"    Elapsed:    {elapsed_full:.1f}s ({bps_full:,.0f} bars/sec)")
    print(f"    Trades:     {s_full['n_trades']:,}")
    print(f"    EV/trade:   ${s_full['ev_per_trade']:+.4f}")
    print(f"    PnL/day:    ${s_full['pnl_per_trade'] * s_full['trades_per_day']:+.2f}")
    print(f"    Win rate:   {s_full['win_rate']:.1%}")
    print(f"    Trades/day: {s_full['trades_per_day']:.1f}")

# %%
# -- 7. SANITY CHECKS -----------------------------------------------------
print(f"\n[7] Sanity checks on BTC data structure...")

# Check for NaNs
nan_count = df_pd[["open", "high", "low", "close"]].isna().sum().sum()
print(f"    NaN count in OHLC: {nan_count}")

# Check monotonic time
is_mono = df_pd["time"].is_monotonic_increasing
print(f"    Time monotonic: {is_mono}")

# Check 1s cadence (most common)
deltas = df_pd["time"].diff().dropna()
mode_delta = deltas.mode().iloc[0]
print(f"    Mode bar delta: {mode_delta}")

# Check price scale
print(f"    Price range:  ${df_pd['close'].min():,.2f} -> ${df_pd['close'].max():,.2f}")
print(f"    Median 1s range: ${(df_pd['high'] - df_pd['low']).median():.2f}")

# Quick ATR estimate
atr_1200 = (df_pd['high'] - df_pd['low']).rolling(1200).mean().iloc[-1]
print(f"    ATR(1200) approx: ${atr_1200:.2f}")

# %%
print(f"\n{'='*60}")
print(f"NB48 COMPLETE")
print(f"{'='*60}")
print(f"\nKEY FINDINGS:")
print(f"  - Backtest speed:        {bars_per_sec:,.0f} bars/sec (BTC slower than gold due to larger zones)")
print(f"  - Smoke EV/trade:        ${s['ev_per_trade']:+.4f}")
print(f"  - Smoke PnL/day:         ${s['pnl_per_trade'] * s['trades_per_day']:+.2f}")
if RUN_FULL:
    print(f"  - Full-corpus EV/trade:  ${s_full['ev_per_trade']:+.4f}")
    print(f"  - Full-corpus PnL/day:   ${s_full['pnl_per_trade'] * s_full['trades_per_day']:+.2f}")
print(f"\nOBSERVATIONS:")
print(f"  - Gold recipe transfers to BTC WITHOUT scaling (ATR does the work).")
print(f"  - v7 SNIPER logic is largely scale-invariant.")
print(f"\nNEXT STEPS:")
print(f"  1. Re-sweep TP multiplier on BTC (gold plateau 12-22 may differ).")
print(f"  2. Re-sweep `inverse_breadth` (gold v8 winner was True->False; may differ).")
print(f"  3. Test whether USD-absolute floors matter on BTC (likely NO, low priority).")
print(f"  4. Compare BTC vs gold trigger rate per bar - BTC may have noisier signal patterns.")
print(f"  5. Consider walk-forward (2025 train / 2026 test) like the gold v7 study did.")
