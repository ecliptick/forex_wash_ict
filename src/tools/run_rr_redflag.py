"""Red-flag sweep — SNIPER TP sensitivity check (added 2026-09-17).

After the bug fixes (limit-order fill, tiebreak, grace_secs, signal_id,
n_inversions), the v7 SNIPER TP=22 optimum needs re-validation. This
driver runs a focused multi-config sweep on a small random sample of
Mon-Fri UTC days from the 1y corpus:

    V7 canonical:          TP=22 (current optimum)
    Near-peak neighbors:   TP=20, TP=18, TP=15, TP=12
    Sanity floor:          TP=4, TP=1

The "red flag" hypothesis: TP=22's headline EV/trade came from a v7
walk-forward that was 56 days, then promoted to the canonical recipe
on a 654-day full corpus walk-forward. If TP=22 is actually a SINGLE
peak in a small N (overfit), the surrounding TP=20/18/15/12 values
should show monotonic or flat EV across the sweep. A peak that doesn't
survive a single TP-step downward is suspect.

Configs:
    1. SNIPER_TP22  — current v7 canonical recipe
    2. SNIPER_TP20  — TP-1 step
    3. SNIPER_TP18  — TP-2 step
    4. SNIPER_TP15  — TP-3 step (also tested in v9)
    5. SNIPER_TP12  — TP-4 step
    6. SNIPER_TP4   — tight TP (sanity reference, v7 walk-fwd reference)
    7. SNIPER_TP1   — 1:1 stress test (floor)

Usage:
    python src/tools/run_rr_redflag.py [N_DAYS]

Outputs:
    notebooks/rr_redflag_per_day.csv
    notebooks/rr_redflag_summary.csv
    stdout summary table
"""
from __future__ import annotations
import os, sys, time, io, contextlib
from pathlib import Path
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pds
import pyarrow.parquet as pq

NS_PER_DAY = 86_400_000_000_000
N_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
RNG_SEED = 20260917  # v6/v7/v8/v9 baseline seed
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

from src.core.optimal_config import optimal_params
from src.backtest.ict_backtest import run_ict_backtest


def _make_config(tp_mult: float):
    p = optimal_params()
    p.fvg_inv_trade_tp_zone_mult = tp_mult
    return p


def _run_one(tp_mult: float, day_label: str, df_day: pd.DataFrame) -> dict:
    p = _make_config(tp_mult)
    buf = io.StringIO()
    err = ""
    try:
        with contextlib.redirect_stdout(buf):
            res = run_ict_backtest(df_day, p, strategy_label=f'redflag_TP{tp_mult}')
    except Exception as e:
        err = str(e)
        res = None
    cfg_name = f'SNIPER_TP{tp_mult:g}'.replace('.', '_')
    if res is None:
        return {
            'config': cfg_name, 'tp_mult': tp_mult, 'day': day_label,
            'n_trades': 0, 'pnl_total': 0.0, 'ev_per_trade': 0.0,
            'n_sniper_submitted': 0, 'n_sniper_triggered': 0,
            'n_sniper_cancelled': 0, 'n_sniper_expired': 0,
            'n_soft_stops': 0, 'n_inversions': 0,
            'n_signals_emitted': 0, 'n_fills': 0,
            'error': err,
        }
    n_t = len(res.trades)
    pnl = float(sum(t.pnl_usd for t in res.trades))
    ev = pnl / n_t if n_t > 0 else 0.0
    return {
        'config': cfg_name, 'tp_mult': tp_mult, 'day': day_label,
        'n_trades': n_t, 'pnl_total': pnl, 'ev_per_trade': ev,
        'n_sniper_submitted': res.n_sniper_submitted,
        'n_sniper_triggered': res.n_sniper_triggered,
        'n_sniper_cancelled': res.n_sniper_cancelled,
        'n_sniper_expired': res.n_sniper_expired,
        'n_soft_stops': res.n_soft_stops,
        'n_inversions': res.n_inversions_detected,
        'n_signals_emitted': getattr(res, 'n_signals_emitted', 0),
        'n_fills': getattr(res, 'n_fills', 0),
        'error': err,
    }


TP_MULTS = [22.0, 20.0, 18.0, 15.0, 12.0, 4.0, 1.0]
configs = [f'SNIPER_TP{t:g}'.replace('.', '_') for t in TP_MULTS]

# ── Day sampling ─────────────────────────────────────────────────────────
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

nb_dir = ROOT / 'notebooks'
csv_per_day = nb_dir / 'rr_redflag_per_day.csv'
total_jobs = len(TP_MULTS) * len(day_data)
print(f'\nRunning {total_jobs} backtests ({len(TP_MULTS)} configs x '
      f'{len(day_data)} days)...', flush=True)

t_start = time.time()
all_runs = []
job_idx = 0
for cfg_idx, tp_mult in enumerate(TP_MULTS):
    cfg_name = configs[cfg_idx]
    print(f'\n=== {cfg_name} ===', flush=True)
    for day_label, df_day in day_data.items():
        job_idx += 1
        if job_idx % 30 == 0:
            elapsed = time.time() - t_start
            print(f'  [{job_idx}/{total_jobs}] '
                  f'{elapsed:.1f}s ({job_idx/elapsed:.1f} bt/s)', flush=True)
        row = _run_one(tp_mult, day_label, df_day)
        all_runs.append(row)

elapsed = time.time() - t_start
print(f'\nDone: {total_jobs} backtests in {elapsed:.1f}s '
      f'({total_jobs/elapsed:.2f} bt/s)', flush=True)

per_day_df = pd.DataFrame(all_runs)
per_day_df.to_csv(csv_per_day, index=False)
print(f'\nWrote {csv_per_day}', flush=True)


# ── Aggregate summary ────────────────────────────────────────────────────
print('\n=== Aggregate summary ===')
rows = []
for tp_mult in TP_MULTS:
    cfg_name = f'SNIPER_TP{tp_mult:g}'.replace('.', '_')
    sub = per_day_df[per_day_df['config'] == cfg_name]
    sub_ok = sub[sub['n_trades'] > 0]
    n_trades = int(sub_ok['n_trades'].sum())
    pnl_total = float(sub['pnl_total'].sum())
    n_days = int((sub_ok['pnl_total'] != 0).sum())
    n_pos = int((sub_ok['pnl_total'] > 0).sum())
    n_neg = int((sub_ok['pnl_total'] < 0).sum())
    ev_trade = pnl_total / n_trades if n_trades > 0 else 0.0
    pnl_per_day = pnl_total / max(1, len(sub_ok))
    se = sub_ok['pnl_total'].std() / np.sqrt(max(1, len(sub_ok)))
    t_stat = pnl_per_day / se if se > 0 else 0.0
    rows.append({
        'config': cfg_name,
        'tp_mult': tp_mult,
        'n_days_ok': len(sub_ok),
        'n_trades': n_trades,
        'PnL_total': round(pnl_total, 2),
        'PnL_per_day': round(pnl_per_day, 4),
        'EV_per_trade': round(ev_trade, 4),
        't_stat': round(t_stat, 2),
        'positive_days': n_pos,
        'negative_days': n_neg,
    })

summary_df = pd.DataFrame(rows)
csv_summary = nb_dir / 'rr_redflag_summary.csv'
summary_df.to_csv(csv_summary, index=False)
print(f'\nWrote {csv_summary}', flush=True)
print('\n' + summary_df.to_string(index=False), flush=True)
