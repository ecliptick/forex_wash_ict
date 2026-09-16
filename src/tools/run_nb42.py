#!/usr/bin/env python
"""nb42 — combined-rules backtest.

Measures the EV/trade impact of combining the 5 new rules that
address user feedback on the soft-stop / mitigation lifecycle:

  Rule 1: fvg_body_only_mitigation       (body-only mitigation detection)
  Rule 2: fvg_body_only_invalidation     (body-only inversion/pierce detection)
  Rule 3: fvg_ifvg_min_inversion_age_secs (skip iFVG entries on immediate flips)
  Rule 4: fvg_drop_tiers=['D']            (drop D-tier zones — already wired)
  Rule 5: fvg_inv_trade_enabled           (open inverse trade on inversion)

Compares 6 configurations on the same days:

  BASELINE        : all knobs OFF (legacy)
  R1: body-mit    : rule 1 only
  R2: body-inv    : rule 2 only
  R3: iFVG-age    : rule 3 only
  R4: drop-D      : rule 4 only (already validated in nb41)
  R5: inv-trade   : rule 5 only
  ALL             : all 5 rules together

Output:
  - notebooks/nb42_per_config.csv   per-config totals (n, EV/trade, total PnL)
  - notebooks/nb42_per_day.csv      per-day breakdown
"""
# %%
from __future__ import annotations
import os
import sys
import time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

os.environ.setdefault('MPLBACKEND', 'Agg')
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pds
import pyarrow.parquet as pq

NS_PER_DAY = 86_400_000_000_000

# Knobs
N_DAYS = 200          # 200 days at 0.14s/day fits in ~30s total
RNG_SEED = 20260915
MIN_BARS_PER_DAY = 30_000
N_WORKERS = 1         # serial — Windows spawn + pyarrow is fragile

# %%
# Repo root
def _find_root():
    here = Path('.').resolve()
    for p in [here, *here.parents]:
        if (p / 'src' / 'core' / 'ict_signals.py').is_file():
            return p
    raise RuntimeError('no repo root')

ROOT = _find_root()
sys.path.insert(0, str(ROOT))
from src.core.ict_signals import compute_simple_atr
from src.core.market_structure import detect_market_structure
from src.core.ict_strategy import TrendStrategyParams
from src.backtest.ict_backtest import run_ict_backtest

# %%
# Data
data_dir = ROOT / 'data'
if not (data_dir / 'XAUUSD_S1_1y.parquet').exists():
    for ancestor in [ROOT.parent, *ROOT.parent.parents]:
        sibling = ancestor / 'data'
        if (sibling / 'XAUUSD_S1_1y.parquet').exists():
            data_dir = sibling
            break
parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'

# Enumerate Mon-Fri days
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

# %%
# Param builders
def _make_baseline():
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
        # All new knobs OFF in baseline
        fvg_body_only_mitigation=False,
        fvg_body_only_invalidation=False,
        fvg_ifvg_min_inversion_age_secs=0,
        fvg_drop_tiers=[],
        fvg_inv_trade_enabled=False,
    )

CONFIGURATIONS = {
    "BASELINE":   {},
    "R1_body_mit": {"fvg_body_only_mitigation": True},
    "R2_body_inv": {"fvg_body_only_invalidation": True},
    "R3_ifvg_age": {"fvg_ifvg_min_inversion_age_secs": 60},
    "R4_drop_D":   {"fvg_drop_tiers": ["D"]},
    "R5_inv_trade": {"fvg_inv_trade_enabled": True},
}

def _make_config(name: str) -> TrendStrategyParams:
    p = _make_baseline()
    for k, v in CONFIGURATIONS[name].items():
        setattr(p, k, v)
    return p

ALL_CONFIG = "ALL"
def _make_all() -> TrendStrategyParams:
    p = _make_baseline()
    p.fvg_body_only_mitigation = True
    p.fvg_body_only_invalidation = True
    p.fvg_ifvg_min_inversion_age_secs = 60
    p.fvg_drop_tiers = ["D"]
    p.fvg_inv_trade_enabled = True
    return p

# %%
# Per-day backtest loop — each configuration, each day
dataset = pds.dataset(str(parquet_path), format='parquet')
all_runs = []   # (config, day, n_trades, pnl, ev)
day_results = []  # (config, day, pnl)

t_start = time.time()
configs = list(CONFIGURATIONS.keys()) + [ALL_CONFIG]
nb_dir = ROOT / 'notebooks'
csv_path = nb_dir / 'nb42_per_day.csv'

# Pre-load all sampled days ONCE — re-use across configurations.
# ~3.5MB per day × 30 days = ~100MB resident, well within RAM.
print('Pre-loading all days...', flush=True)
t_pre = time.time()
day_data: dict[str, pd.DataFrame] = {}
for di, day_id in enumerate(sample_days):
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
print(f'Pre-load: {time.time() - t_pre:.1f}s, {len(day_data)} days', flush=True)

# Helper to run a single (config, day) pair.
def _run_one(cfg_name: str, day_label: str, df_day: pd.DataFrame) -> dict | None:
    p = _make_all() if cfg_name == ALL_CONFIG else _make_config(cfg_name)
    try:
        res = run_ict_backtest(df_day, p, strategy_label=f'nb42_{cfg_name}')
    except Exception as e:
        return {'config': cfg_name, 'day': day_label, 'n_trades': 0, 'pnl_total': 0.0,
                'ev_per_trade': 0.0, 'n_fills': 0, 'n_soft_stops': 0,
                'n_inv_trades_submitted': 0, 'n_inv_trades_filled': 0,
                'n_ifvg_age_dropped': 0, 'n_body_mitigations': 0,
                'n_body_inversions': 0, 'n_fvg_tier_dropped': 0,
                'error': str(e)}
    n_t = len(res.trades)
    pnl = float(sum(t.pnl_usd for t in res.trades))
    ev = pnl / n_t if n_t > 0 else 0.0
    return {
        'config': cfg_name, 'day': day_label,
        'n_trades': n_t, 'pnl_total': pnl, 'ev_per_trade': ev,
        'n_fills': res.n_fills, 'n_soft_stops': res.n_soft_stops,
        'n_inv_trades_submitted': res.n_inv_trades_submitted,
        'n_inv_trades_filled': res.n_inv_trades_filled,
        'n_ifvg_age_dropped': res.n_ifvg_age_dropped,
        'n_body_mitigations': res.n_body_mitigations,
        'n_body_inversions': res.n_body_inversions,
        'n_fvg_tier_dropped': res.n_fvg_tier_dropped,
        'error': '',
    }

for cfg_idx, cfg_name in enumerate(configs):
    print(f'\n=== Config: {cfg_name} ({cfg_idx+1}/{len(configs)}) ===', flush=True)
    t_cfg = time.time()
    rows_this_cfg = []
    if N_WORKERS > 1:
        with ProcessPoolExecutor(max_workers=N_WORKERS) as ex:
            futures = {
                ex.submit(_run_one, cfg_name, label, df): label
                for label, df in day_data.items()
            }
            done = 0
            for fut in as_completed(futures):
                r = fut.result()
                if r is not None:
                    rows_this_cfg.append(r)
                done += 1
                if done % 10 == 0:
                    print(f'  [{cfg_name}] {done}/{len(day_data)} days done '
                          f'({time.time()-t_cfg:.0f}s)', flush=True)
    else:
        for di, (label, df_day) in enumerate(day_data.items()):
            r = _run_one(cfg_name, label, df_day)
            if r is not None:
                rows_this_cfg.append(r)
            if (di + 1) % 10 == 0:
                print(f'  [{cfg_name}] {di+1}/{len(day_data)} days done '
                      f'({time.time()-t_cfg:.0f}s)', flush=True)

    all_runs.extend(rows_this_cfg)
    # Persist after each config — partial results safe.
    pd.DataFrame(all_runs).to_csv(csv_path, index=False)
    print(f'  Config {cfg_name} took {time.time() - t_cfg:.1f}s, {len(all_runs)} total rows', flush=True)

print(f'\nTotal runtime: {time.time() - t_start:.1f}s')

# %%
# Persist per-config totals
df_runs = pd.DataFrame(all_runs)
df_runs.to_csv(csv_path, index=False)
print(f'Per-day CSV written: {len(df_runs)} rows')

# Aggregate per-config
agg = df_runs.groupby('config').agg(
    n_days=('day', 'nunique'),
    n_trades=('n_trades', 'sum'),
    pnl_total=('pnl_total', 'sum'),
).reset_index()
agg['ev_per_trade'] = agg['pnl_total'] / agg['n_trades']
agg['trades_per_day'] = agg['n_trades'] / agg['n_days']
# Compute delta vs BASELINE
baseline_ev = float(agg.loc[agg['config'] == 'BASELINE', 'ev_per_trade'].iloc[0])
baseline_pnl = float(agg.loc[agg['config'] == 'BASELINE', 'pnl_total'].iloc[0])
agg['delta_ev'] = agg['ev_per_trade'] - baseline_ev
agg['delta_pnl'] = agg['pnl_total'] - baseline_pnl
agg.to_csv(nb_dir / 'nb42_per_config.csv', index=False)

print('\n=== Per-config summary ===')
print(agg.to_string(index=False))
