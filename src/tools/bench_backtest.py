"""Run-time benchmark (no profiler overhead).

Measures wall-clock and bars/sec for run_ict_backtest over
N days. Compares to prior runs via stdout log.

Usage: python src/tools/bench_backtest.py 60
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
N_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
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
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

from src.core.ict_strategy import TrendStrategyParams
from src.backtest.ict_backtest import run_ict_backtest


# ── Data ──────────────────────────────────────────────────────────────
data_dir = ROOT / 'data'
parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'

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
print(f'Load: {load_time:.2f}s, {len(day_data)} days, '
      f'{sum(len(d) for d in day_data.values()):,} bars')

# ── Run ×3 (best-of-3 to smooth noise) ───────────────────────────────
p = TrendStrategyParams(
    signal_source='fvg', additional_sources=['ifvg'],
    fvg_resample_secs=60, fvg_min_zone_usd=0.30,
    fvg_supersede_on_new=True,
    fvg_min_lifetime_secs=3,
    fvg_inv_trade_enabled=True,
    bos_choch_ignore_invert_when_aligned=True,
)

best = float('inf')
for run_n in range(3):
    t0 = time.time()
    n_trades = 0
    for day_label, df_day in day_data.items():
        res = run_ict_backtest(df_day, p, strategy_label=f'bench{run_n}')
        n_trades += len(res.trades)
    elapsed = time.time() - t0
    if elapsed < best:
        best = elapsed
        best_n = n_trades
    print(f'Run {run_n+1}: {elapsed:.2f}s  ({elapsed/len(day_data)*1000:.0f} ms/day, '
          f'{sum(len(d) for d in day_data.values())/elapsed:,.0f} bars/sec, '
          f'{n_trades} trades)')

n_bars = sum(len(d) for d in day_data.values())
print(f'\nBEST: {best:.2f}s')
print(f'      {best/len(day_data)*1000:.0f} ms/day')
print(f'      {n_bars/best:,.0f} bars/sec')
print(f'      {best_n} trades across {len(day_data)} days')
