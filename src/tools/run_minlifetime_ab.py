"""fvg_min_lifetime_secs A/B test.

Per AGENTS.md 8th behaviour (2026-09-17): ~9% of FVGs on 1s XAUUSD get
mitigated/inverted on bar+1 (the market fills the gap faster than any
entry could react). The surviving knob `fvg_min_lifetime_secs` drops
zones whose FIRST end event (mitigated / inverted / superseded /
played-out / structure-invalidated) fires within N wall-clock seconds
of the zone's trigger_bar.

This script A/B tests the knob on the current optimal config
(INV_TRADE on). It also crosses with INV_TRADE off to see whether the
filter helps baseline too.

Configs tested:
  1. INV_0     — INV_TRADE + lifetime=0s   (current optimal, no filter)
  2. INV_3     — INV_TRADE + lifetime=3s
  3. INV_4     — INV_TRADE + lifetime=4s   (the user's request)
  4. INV_5     — INV_TRADE + lifetime=5s
  5. INV_10    — INV_TRADE + lifetime=10s
  6. INV_30    — INV_TRADE + lifetime=30s
  7. BASE_4    — BASELINE + lifetime=4s   (cross-check: does the filter
                                          help the un-INV_TRADE config?)
  8. BASE_10   — BASELINE + lifetime=10s

Output:
  - notebooks/minlifetime_per_day.csv   per-day breakdown
  - notebooks/minlifetime_summary.csv   per-config totals
  - stdout summary table

Usage:
  python src/tools/run_minlifetime_ab.py [N_DAYS]
"""
from __future__ import annotations
import os
import sys
import time
import io
import contextlib
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pds
import pyarrow.parquet as pq

NS_PER_DAY = 86_400_000_000_000

N_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
RNG_SEED = 20260917  # same fresh seed for comparability
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


# ── Configs ─────────────────────────────────────────────────────────────
def _common() -> TrendStrategyParams:
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
        fvg_inv_trade_enabled=False,
        gate_on_gmma_bias=False,
        layer_lifetime_secs=7200,
        bos_choch_ignore_invert_when_aligned=True,
        bos_choch_memory_n_events=5,
        fvg_min_lifetime_secs=0,
    )


CONFIGURATIONS = {
    # INV_TRADE on (current optimal)
    "INV_0":   {"fvg_inv_trade_enabled": True, "fvg_min_lifetime_secs": 0},
    "INV_3":   {"fvg_inv_trade_enabled": True, "fvg_min_lifetime_secs": 3},
    "INV_4":   {"fvg_inv_trade_enabled": True, "fvg_min_lifetime_secs": 4},
    "INV_5":   {"fvg_inv_trade_enabled": True, "fvg_min_lifetime_secs": 5},
    "INV_10":  {"fvg_inv_trade_enabled": True, "fvg_min_lifetime_secs": 10},
    "INV_30":  {"fvg_inv_trade_enabled": True, "fvg_min_lifetime_secs": 30},
    # Cross-check on baseline (INV_TRADE off)
    "BASE_4":  {"fvg_inv_trade_enabled": False, "fvg_min_lifetime_secs": 4},
    "BASE_10": {"fvg_inv_trade_enabled": False, "fvg_min_lifetime_secs": 10},
}


def _make_config(name: str) -> TrendStrategyParams:
    p = _common()
    for k, v in CONFIGURATIONS[name].items():
        setattr(p, k, v)
    return p


# ── Data loading ────────────────────────────────────────────────────────
data_dir = ROOT / 'data'
if not (data_dir / 'XAUUSD_S1_1y.parquet').exists():
    for ancestor in [ROOT.parent, *ROOT.parent.parents]:
        sibling = ancestor / 'data'
        if (sibling / 'XAUUSD_S1_1y.parquet').exists():
            data_dir = sibling
            break
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
print(f'Day enumeration: {time.time() - t0:.1f}s, {len(unique_days):,} Mon-Fri days',
      flush=True)

rng = np.random.default_rng(RNG_SEED)
n_sample = min(N_DAYS, len(unique_days))
sample_days = rng.choice(unique_days, size=n_sample, replace=False)
sample_days.sort()
print(f'Sampled {n_sample} days with seed {RNG_SEED}', flush=True)

dataset = pds.dataset(str(parquet_path), format='parquet')
print('Pre-loading all days...', flush=True)
t_pre = time.time()
day_data: dict[str, pd.DataFrame] = {}
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
print(f'Pre-load: {time.time() - t_pre:.1f}s, {len(day_data)} days', flush=True)


def _run_one(cfg_name: str, day_label: str, df_day: pd.DataFrame) -> dict:
    p = _make_config(cfg_name)
    buf = io.StringIO()
    err = ""
    try:
        with contextlib.redirect_stdout(buf):
            res = run_ict_backtest(df_day, p, strategy_label=f'minlt_{cfg_name}')
    except Exception as e:
        err = str(e)
        res = None
    if res is None:
        return {
            'config': cfg_name, 'day': day_label, 'n_trades': 0,
            'pnl_total': 0.0, 'ev_per_trade': 0.0, 'n_fills': 0,
            'n_signals_emitted': 0, 'n_signals_consumed': 0,
            'n_soft_stops': 0, 'n_inv_trades_submitted': 0,
            'n_inv_trades_filled': 0,
            'error': err,
        }
    n_t = len(res.trades)
    pnl = float(sum(t.pnl_usd for t in res.trades))
    ev = pnl / n_t if n_t > 0 else 0.0
    fill_rate = res.n_fills / max(1, res.n_signals_emitted * p.num_layers)
    return {
        'config': cfg_name, 'day': day_label, 'n_trades': n_t,
        'pnl_total': pnl, 'ev_per_trade': ev,
        'n_fills': res.n_fills,
        'n_signals_emitted': res.n_signals_emitted,
        'n_signals_consumed': res.n_signals_consumed,
        'n_soft_stops': res.n_soft_stops,
        'n_inv_trades_submitted': res.n_inv_trades_submitted,
        'n_inv_trades_filled': res.n_inv_trades_filled,
        'error': err,
    }


# ── Run loop ────────────────────────────────────────────────────────────
configs = list(CONFIGURATIONS.keys())
nb_dir = ROOT / 'notebooks'
csv_path = nb_dir / 'minlifetime_per_day.csv'
total_jobs = len(configs) * len(day_data)
print(f'\nRunning {total_jobs} backtests ({len(configs)} configs x {len(day_data)} days)...',
      flush=True)

t_start = time.time()
all_runs = []
job_idx = 0
for cfg_name in configs:
    print(f'\n=== {cfg_name} ===', flush=True)
    for day_label, df_day in day_data.items():
        job_idx += 1
        if job_idx % 30 == 0:
            elapsed = time.time() - t_start
            eta = elapsed * (total_jobs - job_idx) / job_idx
            print(f'  [{job_idx}/{total_jobs}] {cfg_name} {day_label} '
                  f'(elapsed {elapsed:.1f}s, ETA {eta:.1f}s)', flush=True)
        result = _run_one(cfg_name, day_label, df_day)
        all_runs.append(result)

elapsed = time.time() - t_start
print(f'\nAll backtests done in {elapsed:.1f}s ({elapsed/total_jobs:.3f}s per backtest)',
      flush=True)

# ── Per-day CSV ────────────────────────────────────────────────────────
df_runs = pd.DataFrame(all_runs)
df_runs.to_csv(csv_path, index=False)
print(f'\nPer-day CSV: {csv_path}')

# ── Per-config summary ──────────────────────────────────────────────────
agg_cols = ['n_trades', 'pnl_total', 'n_fills', 'n_signals_emitted',
            'n_signals_consumed', 'n_soft_stops', 'n_inv_trades_submitted',
            'n_inv_trades_filled']
summary = df_runs.groupby('config')[agg_cols].sum().reset_index()
summary['ev_per_trade'] = summary['pnl_total'] / summary['n_trades'].clip(lower=1)
summary['pnl_per_day'] = summary['pnl_total'] / len(day_data)
summary['trades_per_day'] = summary['n_trades'] / len(day_data)
summary['fill_rate'] = summary['n_fills'] / (summary['n_signals_emitted'] * 3).clip(lower=1)
summary['signals_per_day'] = summary['n_signals_emitted'] / len(day_data)

# Extract lifetime seconds and inv_trade flag for sorting
def _lifetime(s):
    return int(s.replace('INV_', '').replace('BASE_', '')) if s.replace('INV_', '').replace('BASE_', '').isdigit() else 0
summary['lifetime_secs'] = summary['config'].map({
    'INV_0': 0, 'INV_3': 3, 'INV_4': 4, 'INV_5': 5, 'INV_10': 10, 'INV_30': 30,
    'BASE_4': 4, 'BASE_10': 10,
})
summary['inv_trade'] = summary['config'].str.startswith('INV_')

# Sort by lifetime within each inv_trade group
summary = summary.sort_values(['inv_trade', 'lifetime_secs'],
                              ascending=[False, True]).reset_index(drop=True)
summary_path = nb_dir / 'minlifetime_summary.csv'
summary.to_csv(summary_path, index=False)
print(f'Summary CSV: {summary_path}')

# ── Pretty-print ────────────────────────────────────────────────────────
print('\n' + '=' * 110)
print(f'FVG MIN-LIFETIME FILTER A/B — {len(day_data)} Mon-Fri UTC days, seed {RNG_SEED}')
print('=' * 110)
hdr = (f'{"config":<10} {"lifetime":>9} {"trades":>7} {"EV":>10} {"PnL/day":>10} '
       f'{"tr/day":>7} {"fills":>6} {"sigs":>5} {"sigs/d":>7} {"ev>0?":>6}')
print(hdr)
print('-' * 110)
for _, row in summary.iterrows():
    positive_ev = 'YES' if row['ev_per_trade'] > 0 else 'no'
    print(f'{row["config"]:<10} {int(row["lifetime_secs"]):>8}s {int(row["n_trades"]):>7} '
          f'${row["ev_per_trade"]:>+9.4f} ${row["pnl_per_day"]:>+9.2f} '
          f'{row["trades_per_day"]:>7.1f} {int(row["n_fills"]):>6} '
          f'{int(row["n_signals_emitted"]):>5} {row["signals_per_day"]:>7.1f} '
          f'{positive_ev:>6}')

# Delta vs INV_0 (the no-filter baseline of the INV_TRADE branch)
inv0_row = summary[summary['config'] == 'INV_0'].iloc[0]
inv0_pnl = inv0_row['pnl_total']
print(f'\nDelta-PnL vs INV_0 (gross=${inv0_pnl:.2f} over {len(day_data)} days):')
print(f'  INV_0 trades={int(inv0_row["n_trades"])}, EV/trade=${inv0_row["ev_per_trade"]:+.4f}')
print(f'  {"config":<10} {"dEV/trade":>10} {"dPnL/day":>10} {"dtrades":>8} {"positive_EV":>10}')
results = []
for _, row in summary.iterrows():
    if row['config'] == 'INV_0':
        continue
    delta_pnl = row['pnl_total'] - inv0_pnl
    delta_ev = row['ev_per_trade'] - inv0_row['ev_per_trade']
    delta_n = int(row['n_trades'] - inv0_row['n_trades'])
    results.append({
        'config': row['config'],
        'delta_ev_per_trade': delta_ev,
        'delta_pnl_per_day': delta_pnl / len(day_data),
        'delta_n_trades': delta_n,
        'positive_ev': row['ev_per_trade'] > 0,
    })
results.sort(key=lambda r: r['delta_ev_per_trade'], reverse=True)
for r in results:
    flag = 'YES' if r['positive_ev'] else 'no'
    print(f'  {r["config"]:<10} ${r["delta_ev_per_trade"]:>+9.4f} '
          f'${r["delta_pnl_per_day"]:>+9.2f} {r["delta_n_trades"]:+8d} {flag:>10}')

print()
