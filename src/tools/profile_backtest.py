"""Profile the backtest hot path.

Times the major phases:
  - Data load + day enumeration
  - Signal generation (per-day)
  - Main bar loop (per-day)
  - Trade-construction & metrics

Run: python src/tools/profile_backtest.py 30
"""
from __future__ import annotations
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pds
import pyarrow.parquet as pq

NS_PER_DAY = 86_400_000_000_000

N_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
RNG_SEED = 20260917
MIN_BARS_PER_DAY = 30_000


def _find_root():
    here = Path('.').resolve()
    for p in [here, *here.parents]:
        if (p / 'src' / 'core' / 'ict_signals.py').is_file():
            return p
    raise RuntimeError('no repo root')


ROOT = _find_root()
sys.path.insert(0, str(ROOT))
os.environ.setdefault('MPLBACKEND', 'Agg')

from src.core.ict_strategy import TrendStrategyParams
from src.backtest.ict_backtest import run_ict_backtest


# ── Data ──────────────────────────────────────────────────────────────
data_dir = ROOT / 'data'
parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'

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
print(f'Day enumeration: {time.time() - t0:.2f}s, {len(unique_days):,} days')

rng = np.random.default_rng(RNG_SEED)
n_sample = min(N_DAYS, len(unique_days))
sample_days = rng.choice(unique_days, size=n_sample, replace=False)
sample_days.sort()

dataset = pds.dataset(str(parquet_path), format='parquet')
t_pre = time.time()
day_data = {}
for day_id in sample_days:
    viz_start = pd.Timestamp(int(day_id) * NS_PER_DAY, unit='ns', tz='UTC')
    viz_end = viz_start + pd.Timedelta(days=1)
    table = dataset.to_table(
        columns=['time', 'open', 'high', 'low', 'close'],
        filter=(pds.field('time') >= viz_start) & (pds.field('time') < viz_end),
    )
    if len(table) < MIN_BARS_PER_DAY:
        continue
    df_day = table.to_pandas().sort_values('time').reset_index(drop=True)
    day_data[str(viz_start.date())] = df_day
load_time = time.time() - t_pre
print(f'Data load: {load_time:.2f}s, {len(day_data)} days')
print(f'Total bars: {sum(len(d) for d in day_data.values()):,}')


# ── Profile phases per day ───────────────────────────────────────────
p = TrendStrategyParams(
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
    ms_boost_conviction=1.0,
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
    fvg_body_only_mitigation=False,
    fvg_body_only_invalidation=False,
    fvg_ifvg_min_inversion_age_secs=0,
    fvg_inv_trade_enabled=True,
    gate_on_gmma_bias=False,
    layer_lifetime_secs=7200,
    bos_choch_ignore_invert_when_aligned=True,
    bos_choch_memory_n_events=5,
    fvg_min_lifetime_secs=3,
)

import cProfile
import pstats
import io as _io

print(f'\nProfiling run_ict_backtest on {len(day_data)} days ...')
profile = cProfile.Profile()
profile.enable()
total_t0 = time.time()
total_trades = 0
for day_label, df_day in day_data.items():
    res = run_ict_backtest(df_day, p, strategy_label='profile')
    total_trades += len(res.trades)
elapsed = time.time() - total_t0
profile.disable()
print(f'  Done in {elapsed:.2f}s, {total_trades} trades')
print(f'  Throughput: {elapsed/len(day_data)*1000:.0f} ms/day, '
      f'{sum(len(d) for d in day_data.values())/elapsed:,.0f} bars/sec')

# Top 30 functions by cumulative time
stream = _io.StringIO()
stats = pstats.Stats(profile, stream=stream).sort_stats('cumulative')
stats.print_stats(40)
print(stream.getvalue())

# Top by tottime (self time)
stream = _io.StringIO()
stats = pstats.Stats(profile, stream=stream).sort_stats('tottime')
stats.print_stats(30)
print('\n--- TOP BY SELF TIME ---')
print(stream.getvalue())
