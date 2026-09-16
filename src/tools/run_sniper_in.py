"""Sniper-in mode A/B test — v6+ Innovation #1 (added 2026-09-17).

Per AGENTS.md v6+ Innovation #1: defer FVG/iFVG signal entries until
the anchor zone is INVERTED, then enter the iFVG at the zone's
opposite edge. The hypothesis is that the inversion itself is the
tradeable move (nb39 finding), and the original FVG trade is the
loss-making step we want to skip.

Configs tested:
  1. IMMEDIATE     — entry_mode='immediate' (current v6 OPTIMAL)
  2. SNIPER        — entry_mode='sniper' (this innovation)
  3. SNIPER_300    — sniper with sniper_max_age_secs=300 (5 min cap)
  4. SNIPER_600    — sniper with sniper_max_age_secs=600 (10 min cap)
  5. SNIPER_INV    — sniper + INV_TRADE on (both modes stacked;
                     this tests whether the sniper path duplicates
                     or complements the INV_TRADE edge)

Output:
  - notebooks/sniper_per_day.csv   per-day breakdown
  - notebooks/sniper_summary.csv   per-config totals
  - stdout summary table

Usage:
  python src/tools/run_sniper_in.py [N_DAYS]
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

N_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 59
RNG_SEED = 20260917  # same fresh seed as v6 baselines
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
    """v6 OPTIMAL base — same as AGENTS.md optimal config."""
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
        fvg_min_lifetime_secs=3,  # the v6 winner
    )


# Sniper uses fvg_inv_trade_sl_zone_mult / _tp_zone_mult as its
# SL/TP multipliers (shared knobs — the trade shape is identical).
# We test four entry_mode configs, plus one INV_TRADE-stacked
# variant to see whether sniper mode duplicates or complements
# the INV_TRADE edge.
CONFIGURATIONS = {
    "IMMEDIATE":  {"entry_mode": "immediate"},  # v6 OPTIMAL (no INV_TRADE)
    "SNIPER":     {"entry_mode": "sniper"},
    "SNIPER_300": {"entry_mode": "sniper", "sniper_max_age_secs": 300},
    "SNIPER_600": {"entry_mode": "sniper", "sniper_max_age_secs": 600},
    "SNIPER_INV": {
        "entry_mode": "sniper",
        "fvg_inv_trade_enabled": True,  # both edges stacked
    },
}


def _make_config(name: str) -> TrendStrategyParams:
    p = _common()
    for k, v in CONFIGURATIONS[name].items():
        setattr(p, k, v)
    return p


# ── Data loading (pyarrow, same pattern as run_minlifetime_ab.py) ─────
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


# ── Run + summarize ─────────────────────────────────────────────────────
def _run_one(cfg_name: str, day_label: str, df_day: pd.DataFrame) -> dict:
    p = _make_config(cfg_name)
    buf = io.StringIO()
    err = ""
    try:
        with contextlib.redirect_stdout(buf):
            res = run_ict_backtest(df_day, p, strategy_label=f'sniper_{cfg_name}')
    except Exception as e:
        err = str(e)
        res = None
    if res is None:
        return {
            'config': cfg_name, 'day': day_label,
            'n_trades': 0, 'pnl_total': 0.0, 'ev_per_trade': 0.0,
            'n_sniper_submitted': 0, 'n_sniper_triggered': 0,
            'n_sniper_cancelled': 0, 'n_sniper_expired': 0,
            'n_inv_submitted': 0, 'n_inv_filled': 0,
            'n_soft_stops': 0, 'n_inversions': 0,
            'error': err,
        }
    n_t = len(res.trades)
    pnl = float(sum(t.pnl_usd for t in res.trades))
    ev = pnl / n_t if n_t > 0 else 0.0
    return {
        'config': cfg_name, 'day': day_label,
        'n_trades': n_t, 'pnl_total': pnl, 'ev_per_trade': ev,
        'n_sniper_submitted': res.n_sniper_submitted,
        'n_sniper_triggered': res.n_sniper_triggered,
        'n_sniper_cancelled': res.n_sniper_cancelled,
        'n_sniper_expired': res.n_sniper_expired,
        'n_inv_submitted': res.n_inv_trades_submitted,
        'n_inv_filled': res.n_inv_trades_filled,
        'n_soft_stops': res.n_soft_stops,
        'n_inversions': res.n_inversions_detected,
        'error': err,
    }


configs = list(CONFIGURATIONS.keys())
nb_dir = ROOT / 'notebooks'
csv_path = nb_dir / 'sniper_per_day.csv'
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
                  f'elapsed={elapsed:.1f}s eta={eta:.1f}s',
                  flush=True)
        row = _run_one(cfg_name, day_label, df_day)
        all_runs.append(row)

# ── Aggregate ───────────────────────────────────────────────────────────
df = pd.DataFrame(all_runs)
df.to_csv(csv_path, index=False)
print(f'\nPer-day CSV: {csv_path} ({len(df)} rows)', flush=True)

print(f'\n=== SNIPER-IN MODE A/B — {len(day_data)} Mon-Fri UTC days, '
      f'seed {RNG_SEED} ===', flush=True)
print('=' * 110)
hdr = (
    f'{"config":<10} {"trades":>7} {"EV/trade":>10} {"PnL/day":>10} '
    f'{"snip_sub":>9} {"snip_trg":>9} {"snip_cnc":>10} {"snip_exp":>9} '
    f'{"inv_sub":>8} {"inv_fil":>8} {"soft":>5} {"invs":>5}'
)
print(hdr)
print('-' * 110)
for cfg_name, g in df.groupby('config'):
    n_tr = int(g['n_trades'].sum())
    pnl = float(g['pnl_total'].sum())
    ev = pnl / n_tr if n_tr else 0.0
    pnl_day = pnl / len(day_data)
    print(
        f'{cfg_name:<10} {n_tr:>7} ${ev:>+8.4f} ${pnl_day:>+8.4f} '
        f'{int(g["n_sniper_submitted"].sum()):>9} '
        f'{int(g["n_sniper_triggered"].sum()):>9} '
        f'{int(g["n_sniper_cancelled"].sum()):>10} '
        f'{int(g["n_sniper_expired"].sum()):>9} '
        f'{int(g["n_inv_submitted"].sum()):>8} '
        f'{int(g["n_inv_filled"].sum()):>8} '
        f'{int(g["n_soft_stops"].sum()):>5} '
        f'{int(g["n_inversions"].sum()):>5}'
    )

print()
print(f'Total elapsed: {time.time() - t_start:.1f}s')

# ── Summary CSV ─────────────────────────────────────────────────────────
summary_rows = []
for cfg_name, g in df.groupby('config'):
    n_tr = int(g['n_trades'].sum())
    pnl = float(g['pnl_total'].sum())
    summary_rows.append({
        'config': cfg_name,
        'n_days': len(day_data),
        'trades': n_tr,
        'pnl_total': pnl,
        'ev_per_trade': pnl / n_tr if n_tr else 0.0,
        'pnl_per_day': pnl / len(day_data),
        'sniper_submitted': int(g['n_sniper_submitted'].sum()),
        'sniper_triggered': int(g['n_sniper_triggered'].sum()),
        'sniper_cancelled': int(g['n_sniper_cancelled'].sum()),
        'sniper_expired': int(g['n_sniper_expired'].sum()),
        'inv_submitted': int(g['n_inv_submitted'].sum()),
        'inv_filled': int(g['n_inv_filled'].sum()),
        'soft_stops': int(g['n_soft_stops'].sum()),
        'inversions': int(g['n_inversions'].sum()),
    })
summary_df = pd.DataFrame(summary_rows)
summary_df.to_csv(nb_dir / 'sniper_summary.csv', index=False)
print(f'Summary CSV: {nb_dir / "sniper_summary.csv"}')
