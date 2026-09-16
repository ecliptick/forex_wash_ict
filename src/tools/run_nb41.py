#!/usr/bin/env python
"""nb41 - re-run the nb40 EV-by-tier study with the NEW ``fvg_drop_tiers=['D']`` knob on.

This validates the promotion of the A/B/C/D classifier from nb40
into ``src/core/ict_signals.py`` + the ``fvg_drop_tiers`` knob in
``TrendStrategyParams``.

Same day sample + same ICT config as nb40, but with the drop-D
filter ON. Expected:
  * ``n_fvg_tier_d`` > 0 (D-tier zones are detected).
  * ``n_fvg_tier_dropped`` ~ n_fvg_tier_d (all D-tier zones dropped).
  * EV/trade improvement vs nb40 baseline ($-0.056) by ~$131/day.
"""
# %%
from __future__ import annotations
import os
import sys
import time
from pathlib import Path
os.environ.setdefault('MPLBACKEND', 'Agg')
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pds
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.core import TrendStrategyParams
from src.backtest import run_ict_backtest
from src.core.ict_signals import detect_fvg
from src.core.market_structure import detect_market_structure
# DO NOT ``from src.tools.run_nb40 import _classify_all`` -- that runs
# the whole run_nb40.py top-level (which prints "Sampled 60 days" and
# sets ``n_sample = min(60, ...)`` in the module namespace). Instead,
# inline the classifier functions below. (TODO: refactor the classifier
# into a proper helper module so we don't have to copy it.)

# Tier classifier knobs (match nb38 / run_nb40)
TIER_A_DISPLACEMENT_RATIO = 2.0
TIER_AB_DISPLACEMENT_RATIO = 1.5
TIER_A_MIN_DEPTH = 0.50
TIER_AB_MIN_DEPTH = 0.25
D_PIERCED_AND_INVERTED_MAX_AGE_BARS = 600


def _classify_zone(z, body_size, c1_body, c3_body, struct_dir):
    if (z.pierced_bar >= 0
            and z.inverted_bar >= 0
            and (z.inverted_bar - z.pierced_bar)
                <= D_PIERCED_AND_INVERTED_MAX_AGE_BARS):
        return 'D'
    bigger_outer = max(c1_body, c3_body)
    disp_ratio = (body_size / bigger_outer) if bigger_outer > 0 else 0.0
    depth_ok_a = z.mitigated_depth_pct >= TIER_A_MIN_DEPTH
    depth_ok_b = z.mitigated_depth_pct >= TIER_AB_MIN_DEPTH
    disp_ok_a = disp_ratio >= TIER_A_DISPLACEMENT_RATIO
    disp_ok_b = disp_ratio >= TIER_AB_DISPLACEMENT_RATIO
    struct_ok = struct_dir == z.direction
    if disp_ok_a and depth_ok_a and struct_ok:
        return 'A'
    if disp_ok_b and depth_ok_b:
        return 'B'
    return 'C'


def _classify_all(zones, body_arr, trend_per_1s):
    """Return dict[zone_id] -> tier."""
    tier_by_id = {}
    for z in zones:
        if z.trigger_bar < 2 or z.trigger_bar >= len(body_arr) - 1:
            continue
        c1_body = float(body_arr[z.trigger_bar - 2])
        c3_body = float(body_arr[z.trigger_bar])
        c2_body = float(body_arr[z.trigger_bar - 1])
        struct_dir_at = int(trend_per_1s[z.trigger_bar - 1])
        tier_by_id[id(z)] = _classify_zone(
            z, c2_body, c1_body, c3_body, struct_dir_at,
        )
    return tier_by_id

# %%
# Config
N_DAYS = 30  # hardcoded: 30 days (change to 200 for full run)
assert N_DAYS == 30, f"N_DAYS must be 30, got {N_DAYS}"
RNG_SEED = 20260915
MIN_BARS_PER_DAY = 30_000
NS_PER_DAY = 86_400 * 1_000_000_000

# %% [markdown]
# ### Find the parquet
#
# %%
data_dir = ROOT / 'data'
if not (data_dir / 'XAUUSD_S1_1y.parquet').exists():
    for ancestor in [ROOT.parent, *ROOT.parent.parents]:
        sibling = ancestor / 'data'
        if (sibling / 'XAUUSD_S1_1y.parquet').exists():
            data_dir = sibling
            break
parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'
dataset = pds.dataset(str(parquet_path), format='parquet')

# Enumerate Mon-Fri days.
t0 = time.time()
t_table_ms = pq.read_table(str(parquet_path), columns=['time']).cast(
    pa.schema([pa.field('time', pa.int64())])
)
ms = t_table_ms.column('time').to_numpy(zero_copy_only=False).astype(np.int64)
t_np = ms * 1_000_000
all_days = np.unique(t_np // NS_PER_DAY)
day_dows = np.array([
    pd.Timestamp(int(d) * NS_PER_DAY, unit='ns', tz='UTC').dayofweek
    for d in all_days
])
unique_days = all_days[day_dows < 5]
print(f'Day enumeration: {time.time() - t0:.1f}s, {len(unique_days):,} Mon-Fri days')
rng = np.random.default_rng(RNG_SEED)
n_sample = min(N_DAYS, len(unique_days))
sample_days = rng.choice(unique_days, size=n_sample, replace=False)
sample_days.sort()
print(f'Sampled {n_sample} days')

# %% [markdown]
# ### Per-day backtest loop
#
# %%
# Backtest config: same as nb40 EXCEPT ``fvg_drop_tiers=['D']``
# (default now, but make it explicit for this A/B test).
def _make_params():
    return TrendStrategyParams(
        signal_source='fvg',
        additional_sources=['ifvg'],
        fvg_resample_secs=60,
        fvg_min_zone_usd=0.30,
        num_layers=3,
        inverse_breadth=True,
        invalidation_sl_usd=0.05,
        invalidation_buffer_usd=0.02,
        use_market_structure=True,
        ms_min_conviction=0.0,
        ms_boost_conviction=1.5,
        use_atr_scaling=True,
        atr_len=1200,
        sl_atr_mult=0.25, tp_atr_mult=0.55,
        sl_usd=0.80, tp_usd=1.80, lots=0.01,
        fvg_require_retest_to_invert=True,
        fvg_invalidation_min_pierce_usd=0.05,
        fvg_invalidation_min_consecutive_bars=2,
        fvg_supersede_on_new=True,
        renko_drive_invalidation=False,
        fvg_sweep_enabled=False,
        fvg_invalidate_on_structure=False,
        # NEW (2026-09-15): A/B/C/D tier drop-knob.
        fvg_drop_tiers=['D'],
        fvg_emit_tier_metadata=True,
    )


def _compute_zones_for_day(viz_start, viz_end):
    table = dataset.to_table(
        columns=['time', 'open', 'high', 'low', 'close', 'tickv'],
        filter=(pds.field('time') >= viz_start) & (pds.field('time') < viz_end),
    )
    n_bars = len(table)
    if n_bars < MIN_BARS_PER_DAY:
        return None
    df_day = table.to_pandas().sort_values('time').reset_index(drop=True)
    df_day = df_day.rename(columns={'tickv': 'volume'})
    opens = df_day['open'].to_numpy()
    highs = df_day['high'].to_numpy()
    lows = df_day['low'].to_numpy()
    closes = df_day['close'].to_numpy()
    return df_day


all_trades = []
day_summary = []
skipped_days = 0
t_start = time.time()
total_dropped = 0
total_a = total_b = total_c = total_d = 0
total_signals = 0
total_consumed = 0

for di, day_id in enumerate(sample_days):
    viz_start = pd.Timestamp(int(day_id) * NS_PER_DAY, unit='ns', tz='UTC')
    viz_end = viz_start + pd.Timedelta(days=1)
    df_day = _compute_zones_for_day(viz_start, viz_end)
    if df_day is None:
        skipped_days += 1
        continue
    p = _make_params()
    try:
        res = run_ict_backtest(df_day, p, strategy_label='nb41_dropD')
    except Exception as e:
        print(f'  [{di+1}/{n_sample}] day={viz_start.date()} BACKTEST ERROR: {e}')
        skipped_days += 1
        continue
    total_dropped += res.n_fvg_tier_dropped
    total_a += res.n_fvg_tier_a
    total_b += res.n_fvg_tier_b
    total_c += res.n_fvg_tier_c
    total_d += res.n_fvg_tier_d
    total_signals += res.n_signals_emitted
    total_consumed += res.n_signals_consumed
    for t in res.trades:
        all_trades.append({
            'day_date': str(viz_start.date()),
            'src': t.entry_triggered_by,
            'direction': int(t.direction),
            'entry_price': float(t.entry_price),
            'exit_price': float(t.exit_price),
            'pnl_usd': float(t.pnl_usd),
            'exit_reason': t.exit_reason,
            'lots': float(t.lots),
            'hold_secs': float(t.hold_secs),
            'layer_idx': int(t.layer_idx),
        })
    day_summary.append({
        'day_date': str(viz_start.date()),
        'n_trades': len(res.trades),
        'n_dropped': res.n_fvg_tier_dropped,
        'n_a': res.n_fvg_tier_a,
        'n_b': res.n_fvg_tier_b,
        'n_c': res.n_fvg_tier_c,
        'n_d': res.n_fvg_tier_d,
    })
    if (di + 1) % 10 == 0 or (di + 1) == n_sample:
        elapsed = time.time() - t_start
        last = day_summary[-1]
        print(f'  [{di+1:>3d}/{n_sample}] day={last["day_date"]} '
              f'trades={last["n_trades"]} dropped={last["n_dropped"]} '
              f'tiers={last["n_a"]}/{last["n_b"]}/{last["n_c"]}/{last["n_d"]} '
              f'elapsed={elapsed:.1f}s', flush=True)

# %% [markdown]
# ### Aggregate stats
#
# %%
print(f'\nSkipped days: {skipped_days}')
print(f'Total trades: {len(all_trades):,}')
print(f'Total signals emitted: {total_signals:,}')
print(f'Total signals consumed: {total_consumed:,}')
print(f'Total D-tier zones dropped: {total_dropped:,}')
print(f'Tier distribution (across {N_DAYS} days): '
      f'A={total_a} B={total_b} C={total_c} D={total_d} '
      f'(sum = {total_a + total_b + total_c + total_d})')

trades_df = pd.DataFrame(all_trades)
days_df = pd.DataFrame(day_summary)
trades_df.to_csv(ROOT / 'notebooks' / 'nb41_trades.csv', index=False)
days_df.to_csv(ROOT / 'notebooks' / 'nb41_day_summary.csv', index=False)

if len(trades_df) > 0:
    pnl = trades_df['pnl_usd']
    ev = float(pnl.mean())
    total_pnl = float(pnl.sum())
    tp = (trades_df['exit_reason'] == 'tp').mean() * 100
    sl = (trades_df['exit_reason'] == 'sl').mean() * 100
    inv = (trades_df['exit_reason'] == 'inv').mean() * 100
    print(f'\n=== Final EV/trade (drop-D on, {N_DAYS} days) ===')
    print(f'  n_trades={len(trades_df)}  total_pnl=${total_pnl:+8.2f}  '
          f'EV/trade=${ev:+7.4f}')
    print(f'  TP={tp:.1f}%  SL={sl:.1f}%  inv={inv:.1f}%')

print(f'\nTotal runtime: {time.time() - t_start:.1f}s')
