"""Quick ATR scale sanity check.

Tells us typical ATR(1200) value (in USD) for 1s XAUUSD, plus
the equivalent in pips.
"""
from __future__ import annotations
import time
import sys
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

NS_PER_DAY = 86_400_000_000_000


def _find_root():
    here = Path('.').resolve()
    for p in [here, *here.parents]:
        if (p / 'src' / 'core' / 'ict_signals.py').is_file():
            return p
    raise RuntimeError('no repo root')


ROOT = _find_root()

sys.path.insert(0, str(ROOT))

os.environ.setdefault('MPLBACKEND', 'Agg')

from src.core.ict_signals import compute_simple_atr

data_dir = ROOT / 'data'
parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'

t0 = time.time()
df = pd.read_parquet(str(parquet_path))
print(f'load: {time.time() - t0:.1f}s, {len(df):,} bars')

t_ns = df['time'].astype(np.int64).to_numpy() * 1_000_000
days = t_ns // NS_PER_DAY
unique_days = np.unique(days)
print(f'{len(unique_days)} days')

# Sample 30 random days to get a representative view
rng = np.random.default_rng(42)
sample = rng.choice(unique_days, size=30, replace=False)

atr_1200_samples = []
atr_300_samples = []
atr_60_samples = []
bar_range_samples = []

for d in sample:
    mask = days == d
    df_day = df.loc[mask]
    if len(df_day) < 1500:
        continue
    h = df_day['high'].to_numpy().astype(np.float64)
    l = df_day['low'].to_numpy().astype(np.float64)
    c = df_day['close'].to_numpy().astype(np.float64)
    a1200 = compute_simple_atr(h, l, c, length=1200)
    a300 = compute_simple_atr(h, l, c, length=300)
    a60 = compute_simple_atr(h, l, c, length=60)
    # Take a mid-day slice (skip first 30 min) so ATR is populated
    atr_1200_samples.append(np.nanmean(a1200[1800:]))
    atr_300_samples.append(np.nanmean(a300[1800:]))
    atr_60_samples.append(np.nanmean(a60[1800:]))
    bar_range_samples.append(np.mean(h[1800:] - l[1800:]))

print(f'\nATR(1200) [20-min window]: mean = ${np.mean(atr_1200_samples):.3f}, '
      f'median = ${np.median(atr_1200_samples):.3f}, '
      f'p10 = ${np.percentile(atr_1200_samples, 10):.3f}, '
      f'p90 = ${np.percentile(atr_1200_samples, 90):.3f}')
print(f'ATR(300) [5-min window]:   mean = ${np.mean(atr_300_samples):.3f}, '
      f'median = ${np.median(atr_300_samples):.3f}')
print(f'ATR(60) [1-min window]:    mean = ${np.mean(atr_60_samples):.3f}, '
      f'median = ${np.median(atr_60_samples):.3f}')
print(f'Mean 1s bar range:         mean = ${np.mean(bar_range_samples):.4f}')

print(f'\nReference scales:')
print(f'  Current sl_usd default: $0.80')
print(f'  Current tp_usd default: $1.80')
print(f'  Current zone-anchor SL: tiny (zone edge to fill, ~$0.05-$0.30)')
print(f'  sl_atr_mult default: 0.25  (UNUSED for SL — only for TP rules 2/3/4)')
print(f'  tp_atr_mult default: 0.55  (used for TP rules 3/4 only)')
print(f'\nWhat ATR(1200) × 0.5 would give: ${np.mean(atr_1200_samples) * 0.5:.3f}')
print(f'What ATR(300) × 1.0 would give:  ${np.mean(atr_300_samples) * 1.0:.3f}')
print(f'What ATR(60)  × 0.5 would give:  ${np.mean(atr_60_samples) * 0.5:.3f}')
