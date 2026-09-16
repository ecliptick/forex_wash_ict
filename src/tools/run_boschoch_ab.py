"""BoS/CHoCH history intervention A/B test.

The alpha v3 study tested `BOS_GATE_*` configs that referenced
`bos_choch_directional_gate`, but that knob was REMOVED 2026-09-16
(the configs are silently no-ops now). The surviving BoS/CHoCH
interventions are:

1. `ms_min_conviction` — drop trades with conviction < threshold
   (the conviction score in [0, 1.5] is computed by
   `structure_conviction` and is informed by recent BoS/CHoCH events).
2. `ms_boost_conviction` — widen TP on conviction > 1.0 entries
   (was tested as BOS_BOOST_ONLY in v3 = no effect).
3. `bos_choch_memory_n_events` — controls how many recent BoS/CHoCH
   events the alignment memory retains (default 5).
4. `bos_choch_ignore_invert_when_aligned` — ON by default, only
   affects the inversion soft-stop (not a signal gate).

This script A/B tests the surviving knobs in isolation against the
current optimal config (INV_TRADE on BASELINE) to see whether any
BoS/CHoCH-based intervention improves EV/trade.

Configs tested:
  1. OPTIMAL      — INV_TRADE (current best from previous run)
  2. CONV_05      — ms_min_conviction=0.5 (drop CHoCH-against)
  3. CONV_07      — ms_min_conviction=0.7 (stricter)
  4. BOOST_15     — ms_boost_conviction=1.5 (TP widen on aligned)
  5. MEM_3        — bos_choch_memory_n_events=3
  6. MEM_10       — bos_choch_memory_n_events=10
  7. COMBO        — INV_TRADE + CONV_05 + BOOST_15 + MEM_10

Output:
  - notebooks/boschoch_per_day.csv   per-day breakdown
  - notebooks/boschoch_summary.csv   per-config totals
  - stdout summary table

Usage:
  python src/tools/run_boschoch_ab.py [N_DAYS]
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
RNG_SEED = 20260917  # same fresh seed as run_optimal.py for comparability
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
    """v2 BASELINE defaults + INV_TRADE (the current optimal)."""
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
        ms_max_boost_age_bars=60,
        ms_choch_caution_age_bars=60,
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
    )


CONFIGURATIONS = {
    # 1. OPTIMAL — current best from run_optimal.py
    "OPTIMAL": {},
    # 2. CONV_05 — drop trades with conviction < 0.5
    "CONV_05": {"ms_min_conviction": 0.5},
    # 3. CONV_07 — stricter conviction floor
    "CONV_07": {"ms_min_conviction": 0.7},
    # 4. BOOST_15 — widen TP on conviction > 1.0 entries
    "BOOST_15": {"ms_boost_conviction": 1.5},
    # 5. MEM_3 — tighter BoS/CHoCH memory window
    "MEM_3": {"bos_choch_memory_n_events": 3},
    # 6. MEM_10 — wider BoS/CHoCH memory window
    "MEM_10": {"bos_choch_memory_n_events": 10},
    # 7. COMBO — conviction filter + boost + wide memory
    "COMBO": {
        "ms_min_conviction": 0.5,
        "ms_boost_conviction": 1.5,
        "bos_choch_memory_n_events": 10,
    },
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
            res = run_ict_backtest(df_day, p, strategy_label=f'boschoch_{cfg_name}')
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
            'n_alignment_skipped': 0,
            'n_entry_aligned': 0, 'n_entry_opposed': 0,
            'n_entry_unknown': 0,
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
        'n_alignment_skipped': res.n_alignment_skipped_inversions,
        'n_entry_aligned': res.n_entry_alignment_aligned,
        'n_entry_opposed': res.n_entry_alignment_opposed,
        'n_entry_unknown': res.n_entry_alignment_unknown,
        'error': err,
    }


# ── Run loop ────────────────────────────────────────────────────────────
configs = list(CONFIGURATIONS.keys())
nb_dir = ROOT / 'notebooks'
csv_path = nb_dir / 'boschoch_per_day.csv'
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
            'n_inv_trades_filled', 'n_alignment_skipped',
            'n_entry_aligned', 'n_entry_opposed', 'n_entry_unknown']
summary = df_runs.groupby('config')[agg_cols].sum().reset_index()
summary['ev_per_trade'] = summary['pnl_total'] / summary['n_trades'].clip(lower=1)
summary['pnl_per_day'] = summary['pnl_total'] / len(day_data)
summary['trades_per_day'] = summary['n_trades'] / len(day_data)
summary['fill_rate'] = summary['n_fills'] / (summary['n_signals_emitted'] * 3).clip(lower=1)
# Entry-alignment population fractions
total_entries = (summary['n_entry_aligned'] + summary['n_entry_opposed']
                 + summary['n_entry_unknown']).clip(lower=1)
summary['pct_aligned'] = 100 * summary['n_entry_aligned'] / total_entries
summary['pct_opposed'] = 100 * summary['n_entry_opposed'] / total_entries

summary = summary.sort_values('ev_per_trade', ascending=False)
summary_path = nb_dir / 'boschoch_summary.csv'
summary.to_csv(summary_path, index=False)
print(f'Summary CSV: {summary_path}')

# ── Pretty-print ────────────────────────────────────────────────────────
print('\n' + '=' * 120)
print(f'BOS/CHOCH HISTORY A/B — {len(day_data)} Mon-Fri UTC days, seed {RNG_SEED}')
print('=' * 120)
hdr = (f'{"config":<12} {"trades":>7} {"EV":>10} {"PnL/day":>10} '
       f'{"tr/day":>7} {"fills":>6} {"sigs":>5} {"soft":>5} {"inv_tr":>6} '
       f'{"skip_inv":>8} {"%align":>6} {"ev>0?":>6}')
print(hdr)
print('-' * 120)
for _, row in summary.iterrows():
    positive_ev = 'YES' if row['ev_per_trade'] > 0 else 'no'
    print(f'{row["config"]:<12} {int(row["n_trades"]):>7} '
          f'${row["ev_per_trade"]:>+9.4f} ${row["pnl_per_day"]:>+9.2f} '
          f'{row["trades_per_day"]:>7.1f} {int(row["n_fills"]):>6} '
          f'{int(row["n_signals_emitted"]):>5} {int(row["n_soft_stops"]):>5} '
          f'{int(row["n_inv_trades_filled"]):>6} '
          f'{int(row["n_alignment_skipped"]):>8} '
          f'{row["pct_aligned"]:>5.1f}% '
          f'{positive_ev:>6}')

# Delta vs OPTIMAL
optimal_row = summary[summary['config'] == 'OPTIMAL'].iloc[0]
optimal_pnl = optimal_row['pnl_total']
print(f'\nDelta-PnL vs OPTIMAL (gross=${optimal_pnl:.2f} over {len(day_data)} days):')
print(f'  OPTIMAL trades={int(optimal_row["n_trades"])}, EV/trade=${optimal_row["ev_per_trade"]:+.4f}')
print(f'  {"config":<12} {"dEV/trade":>10} {"dPnL/day":>10} {"dtrades":>8} {"positive_EV":>10}')
results = []
for _, row in summary.iterrows():
    if row['config'] == 'OPTIMAL':
        continue
    delta_pnl = row['pnl_total'] - optimal_pnl
    delta_ev = row['ev_per_trade'] - optimal_row['ev_per_trade']
    delta_n = int(row['n_trades'] - optimal_row['n_trades'])
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
    print(f'  {r["config"]:<12} ${r["delta_ev_per_trade"]:>+9.4f} '
          f'${r["delta_pnl_per_day"]:>+9.2f} {r["delta_n_trades"]:+8d} {flag:>10}')

print()
