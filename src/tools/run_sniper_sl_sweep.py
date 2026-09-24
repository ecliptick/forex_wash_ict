"""Sniper SL zone_mult sensitivity sweep (added 2026-09-18, v14).

User directive (2026-09-18):
    "current sniper mode has very tight SLs, it could mean that we get
     stopped out in live, and trades are also very sparse. do a study on
     how much we can increase SL without impacting profits much but also
     give us the insurance in live trading"

The v7 SNIPER recipe sets ``fvg_inv_trade_sl_zone_mult=1.0`` — SL is
1× the iFVG zone width. On 1s XAUUSD that's typically $0.30-$0.80,
which is roughly the same scale as a 1s bar's noise range. Live
execution has slippage + spread widening + the broker's quote delay;
this means a SL that's mathematically "1× zone width" gets
systematically worse in production.

The hypothesis: **widening the SL multiplier (1× → 1.5× → 2× → 3×
→ 5× → 8×) gives live-trading insurance at a modest EV cost**.

Configs (all on v7 SNIPER base; only ``fvg_inv_trade_sl_zone_mult`` varies):

  | config   | sl_zone_mult | interpretation                          |
  |----------|-------------:|-----------------------------------------|
  | SL_1.0x  |         1.0  | v7 canonical (1× zone width, tight)     |
  | SL_1.5x  |         1.5  | modest insurance (+50% SL)              |
  | SL_2.0x  |         2.0  | 2× zone width (typical insurance)       |
  | SL_3.0x  |         3.0  | 3× zone width (matches ~3s bar range)   |
  | SL_5.0x  |         5.0  | wide insurance                          |
  | SL_8.0x  |         8.0  | very wide insurance (overkill ceiling)  |

TP is fixed at the v7 canonical 22.0 (see v11 "TP-monotonicity is
a plateau from TP=12 to TP=22"; widening SL only, not TP, isolates
the SL insurance effect).

Usage:
    cd c:/coding/ict_tier_v2
    python src/tools/run_sniper_sl_sweep.py 20    # DRY_RUN
    python src/tools/run_sniper_sl_sweep.py 100   # full 100-day report
    python src/tools/run_sniper_sl_sweep.py       # default N=20 (dry_run)

Outputs:
    notebooks/sniper_sl_dry_per_day.csv    (when N_DAYS<=20)
    notebooks/sniper_sl_dry_summary.csv
    notebooks/sniper_sl_full_per_day.csv   (when N_DAYS>20)
    notebooks/sniper_sl_full_summary.csv

The DRY/EXTEND pattern mirrors run_param_sweep.py: the dry-run
caps at 20 days (~30s, fast sanity check) and the full run caps
at 100 days (~3 min). Both use seed 20260917 (the v6/v7/v8/v9/v10
baseline seed) for direct comparability.
"""
from __future__ import annotations

import contextlib
import io
import logging
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
N_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 20
RNG_SEED = 20260917  # v6/v7/v8/v9/v10 baseline seed
MIN_BARS_PER_DAY = 30_000
DRY_RUN_THRESHOLD = 20


def _find_root():
    here = Path('.').resolve()
    for p in [here, *here.parents]:
        if (p / 'src' / 'core' / 'ict_signals.py').is_file():
            return p
    raise RuntimeError('no repo root (cannot find src/core/ict_signals.py)')


ROOT = _find_root()
sys.path.insert(0, str(ROOT))
os.environ.setdefault('MPLBACKEND', 'Agg')

_log = logging.getLogger('sniper_sl_sweep')

from src.core.optimal_config import optimal_params  # noqa: E402
from src.backtest.ict_backtest import run_ict_backtest  # noqa: E402


# ── Config grid ──────────────────────────────────────────────────────────
# (name, sl_zone_mult). All other knobs are at v7 SNIPER canonical.
CONFIGURATIONS: dict[str, float] = {
    "SL_1.0x": 1.0,   # v7 canonical baseline
    "SL_1.5x": 1.5,
    "SL_2.0x": 2.0,
    "SL_3.0x": 3.0,
    "SL_5.0x": 5.0,
    "SL_8.0x": 8.0,
}


def _cfg_name(sl_mult: float) -> str:
    """Stable config name (e.g. SL_1x, SL_1_5x, SL_2x)."""
    return f"SL_{sl_mult:g}x".replace('.', '_')


def _make_config(sl_mult: float):
    p = optimal_params()
    p.fvg_inv_trade_sl_zone_mult = sl_mult
    return p


def _run_one(sl_mult: float, day_label: str, df_day: pd.DataFrame) -> dict:
    cfg_name = _cfg_name(sl_mult)
    p = _make_config(sl_mult)
    buf = io.StringIO()
    err = ""
    try:
        with contextlib.redirect_stdout(buf):
            res = run_ict_backtest(df_day, p, strategy_label=f"sl_sweep_{cfg_name}")
    except Exception as e:
        err = str(e)
        res = None

    base_row = {
        'config': cfg_name,
        'sl_zone_mult': sl_mult,
        'day': day_label,
        'n_trades': 0,
        'pnl_total': 0.0,
        'ev_per_trade': 0.0,
        'n_sniper_submitted': 0,
        'n_sniper_triggered': 0,
        'n_sniper_cancelled': 0,
        'n_sniper_expired': 0,
        'n_soft_stops': 0,
        'n_inversions': 0,
        'n_signals_emitted': 0,
        'n_fills': 0,
        'n_sl_exits': 0,
        'n_tp_exits': 0,
        'n_inv_exits': 0,
        'n_eod_exits': 0,
        'n_subsec_sl': 0,
        'pct_subsec_sl': 0.0,
        'median_sl_dist_usd': 0.0,
        'median_tp_dist_usd': 0.0,
        'median_hold_secs': 0.0,
        'avg_win': 0.0,
        'avg_loss': 0.0,
        'profit_factor': 0.0,
        'win_rate': 0.0,
        'max_win': 0.0,
        'max_loss': 0.0,
        'error': err,
    }
    if res is None or len(res.trades) == 0:
        return base_row

    n_t = len(res.trades)
    pnl_total = float(sum(t.pnl_usd for t in res.trades))
    ev = pnl_total / n_t
    wins = [t.pnl_usd for t in res.trades if t.pnl_usd > 0]
    losses = [t.pnl_usd for t in res.trades if t.pnl_usd <= 0]
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    win_rate = len(wins) / n_t
    profit_factor = (
        sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float('inf')
    )

    exit_reasons = [t.exit_reason for t in res.trades]
    n_sl = sum(1 for r in exit_reasons if r == 'sl')
    n_tp = sum(1 for r in exit_reasons if r == 'tp')
    n_inv = sum(1 for r in exit_reasons if r == 'inv')
    n_eod = sum(1 for r in exit_reasons if r == 'eod')

    holds = np.array([t.hold_secs for t in res.trades], dtype=np.float64)
    sl_dists = np.array([t.stop_usd for t in res.trades], dtype=np.float64)
    tp_dists = np.array([t.target_usd for t in res.trades], dtype=np.float64)
    pnls = np.array([t.pnl_usd for t in res.trades], dtype=np.float64)

    subsec_sl_mask = (
        np.array([r == 'sl' for r in exit_reasons])
        & (holds <= 1.0)
    )
    n_subsec_sl = int(subsec_sl_mask.sum())

    return {
        **base_row,
        'config': cfg_name,
        'sl_zone_mult': sl_mult,
        'day': day_label,
        'n_trades': n_t,
        'pnl_total': pnl_total,
        'ev_per_trade': ev,
        'n_sniper_submitted': res.n_sniper_submitted,
        'n_sniper_triggered': res.n_sniper_triggered,
        'n_sniper_cancelled': res.n_sniper_cancelled,
        'n_sniper_expired': res.n_sniper_expired,
        'n_soft_stops': res.n_soft_stops,
        'n_inversions': res.n_inversions_detected,
        'n_signals_emitted': getattr(res, 'n_signals_emitted', 0),
        'n_fills': getattr(res, 'n_fills', 0),
        'n_sl_exits': n_sl,
        'n_tp_exits': n_tp,
        'n_inv_exits': n_inv,
        'n_eod_exits': n_eod,
        'n_subsec_sl': n_subsec_sl,
        'pct_subsec_sl': (n_subsec_sl / n_t * 100) if n_t > 0 else 0.0,
        'median_sl_dist_usd': float(np.median(sl_dists)),
        'median_tp_dist_usd': float(np.median(tp_dists)),
        'median_hold_secs': float(np.median(holds)),
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': profit_factor if np.isfinite(profit_factor) else 999.0,
        'win_rate': win_rate,
        'max_win': float(pnls.max()) if n_t > 0 else 0.0,
        'max_loss': float(pnls.min()) if n_t > 0 else 0.0,
        'error': err,
    }


# ── Day sampling (same as run_rr_redflag.py / run_param_sweep.py) ───────
data_dir = ROOT / 'data'
parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'

t0 = time.time()
# Read time as timestamp[ms] then convert to int64 ns for day bucketing
# (the parquet stores UTC timestamps as timestamp[ms])
t_table = pq.read_table(str(parquet_path), columns=['time'])
# timestamp[ms] → numpy datetime64[ns] → int64
times_ms = t_table.column('time').to_numpy()  # numpy datetime64[ms]
times_ns = times_ms.astype('datetime64[ns]').astype(np.int64)
all_days = np.unique(times_ns // NS_PER_DAY)
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
print(f'Pre-load: {time.time() - t_pre:.1f}s, {len(day_data)} days kept', flush=True)

# Decide output suffix
suffix = 'dry' if N_DAYS <= DRY_RUN_THRESHOLD else 'full'
nb_dir = ROOT / 'notebooks'
csv_per_day = nb_dir / f'sniper_sl_{suffix}_per_day.csv'
csv_summary = nb_dir / f'sniper_sl_{suffix}_summary.csv'

total_jobs = len(CONFIGURATIONS) * len(day_data)
print(f'\nRunning {total_jobs} backtests '
      f'({len(CONFIGURATIONS)} configs x {len(day_data)} days, '
      f'{suffix.upper()}_RUN @ N={N_DAYS})...', flush=True)

t_start = time.time()
all_runs = []
job_idx = 0
for cfg_name, sl_mult in CONFIGURATIONS.items():
    print(f'\n=== {cfg_name} (sl_zone_mult={sl_mult}) ===', flush=True)
    for day_label, df_day in day_data.items():
        job_idx += 1
        if job_idx % 30 == 0:
            elapsed = time.time() - t_start
            print(f'  [{job_idx}/{total_jobs}] '
                  f'{elapsed:.1f}s ({job_idx/elapsed:.1f} bt/s)', flush=True)
        row = _run_one(sl_mult, day_label, df_day)
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
for sl_mult in CONFIGURATIONS.values():
    cfg_name = _cfg_name(sl_mult)
    sub = per_day_df[per_day_df['config'] == cfg_name]
    sub_ok = sub[sub['n_trades'] > 0]
    n_trades = int(sub_ok['n_trades'].sum())
    pnl_total = float(sub['pnl_total'].sum())
    n_days_run = int(len(sub_ok))
    n_pos = int((sub_ok['pnl_total'] > 0).sum())
    n_neg = int((sub_ok['pnl_total'] < 0).sum())
    n_zero = int((sub_ok['pnl_total'] == 0).sum())
    ev_trade = pnl_total / n_trades if n_trades > 0 else 0.0
    pnl_per_day = pnl_total / max(1, len(sub_ok))
    se = sub_ok['pnl_total'].std() / np.sqrt(max(1, len(sub_ok)))
    t_stat = pnl_per_day / se if se > 0 else 0.0
    avg_subsec_sl = float(sub_ok['pct_subsec_sl'].mean()) if n_days_run > 0 else 0.0
    avg_med_hold = float(sub_ok['median_hold_secs'].mean()) if n_days_run > 0 else 0.0
    rows.append({
        'config': cfg_name,
        'sl_zone_mult': sl_mult,
        'n_days_ok': n_days_run,
        'n_trades': n_trades,
        'PnL_total': round(pnl_total, 2),
        'PnL_per_day': round(pnl_per_day, 4),
        'EV_per_trade': round(ev_trade, 4),
        't_stat': round(t_stat, 2),
        'positive_days': n_pos,
        'negative_days': n_neg,
        'zero_days': n_zero,
        'avg_pct_subsec_sl': round(avg_subsec_sl, 2),
        'avg_median_hold_secs': round(avg_med_hold, 2),
    })

summary_df = pd.DataFrame(rows)
summary_df.to_csv(csv_summary, index=False)
print(f'\nWrote {csv_summary}', flush=True)
print('\n' + summary_df.to_string(index=False), flush=True)


# ── Headline delta vs baseline (SL_1.0x) ────────────────────────────────
print('\n=== Delta vs SL_1.0x baseline ===')
baseline = summary_df[summary_df['config'] == _cfg_name(1.0)]
if not baseline.empty:
    base_pnl_per_day = float(baseline['PnL_per_day'].iloc[0])
    base_ev = float(baseline['EV_per_trade'].iloc[0])
    base_trades = int(baseline['n_trades'].iloc[0])
    base_subsec = float(baseline['avg_pct_subsec_sl'].iloc[0])
    print(f'SL_1.0x baseline: PnL/day=${base_pnl_per_day:+.4f}, '
          f'EV/trade=${base_ev:+.4f}, n_trades={base_trades}, '
          f'avg_pct_subsec_SL={base_subsec:.1f}%')
    print()
    for _, r in summary_df.iterrows():
        if r['config'] == _cfg_name(1.0):
            continue
        d_pnl_day = r['PnL_per_day'] - base_pnl_per_day
        d_ev = r['EV_per_trade'] - base_ev
        d_trades = r['n_trades'] - base_trades
        d_subsec = r['avg_pct_subsec_sl'] - base_subsec
        # "Tradeoff score" = ΔPnL/day relative to "how much SL widening"
        # Higher = more profit preserved per unit of widening
        print(f'  {r["config"]:<8} sl_mult={r["sl_zone_mult"]:.1f}x  '
              f'delta_PnL/day=${d_pnl_day:+.4f}  delta_EV/trade=${d_ev:+.4f}  '
              f'delta_trades={d_trades:+d}  delta_subsec_SL={d_subsec:+.1f}%  '
              f'tr/day={r["n_trades"]/max(1, r["n_days_ok"]):.1f}')