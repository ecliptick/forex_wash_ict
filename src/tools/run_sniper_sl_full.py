"""Full-corpus walk-forward on the v16 SL sweep winner (2026-09-18).

The v16 N=100 sweep (``src/tools/run_sniper_sl_sweep.py``) found that
widening the sniper SL from 1.0× zone width to 2.0× / 3.0× / 5.0×
produces a +35-50% PnL/day improvement at N=100, with the peak at
SL=5.0× and a clean plateau from 2× to 5×. Recommendation was
**promote SL=2.0× to canonical pending full-corpus validation**.

This driver (v16a) runs the 4 most-likely candidates on the FULL
654-day XAUUSD corpus with walk-forward train/test split:

  | config                | SL_mult | TP_mult | role                       |
  |-----------------------|---------|---------|----------------------------|
  | SNIPER_22_SL_1x       |    1.0× |   22.0× | v7 canonical baseline      |
  | SNIPER_22_SL_2x       |    2.0× |   22.0× | v16 recommended (cleanest) |
  | SNIPER_22_SL_3x       |    3.0× |   22.0× | v16 plateau (high Sharpe)  |
  | SNIPER_22_SL_5x       |    5.0× |   22.0× | v16 empirical peak         |

Walk-forward split:
  - Train: 2024 + 2025 Mon-Fri days (~516 days)
  - Test:  2026 Mon-Fri days (~138 days)
  Reports per-config EV/trade on each half independently.

This validates:
  - **Overfitting**: does the +35% headline at N=100 hold up on N=654?
  - **Walk-forward robustness**: is SL=2× +EV on the 2026 holdout?
  - **Per-year/month consistency**: does any year degrade?
  - **Tail risk**: max DD, worst day, worst month

Total jobs: 4 configs × 660 days = ~2640 backtests (~9-12 min wall-clock
at 4-5 bt/s based on the v15 N=100 benchmark of 5.03 bt/s).

Outputs:
  - notebooks/sniper_sl_full_per_day.csv   per-day breakdown
  - notebooks/sniper_sl_full_summary.csv   per-(config, split) totals
  - stdout walk-forward table

Usage:
  python src/tools/run_sniper_sl_full.py
"""
from __future__ import annotations
import os, sys, time, io, contextlib
from pathlib import Path
import numpy as np
import pandas as pd
import pyarrow.dataset as pds
import pyarrow.parquet as pq

NS_PER_DAY = 86_400_000_000_000
MS_PER_DAY = 86_400_000
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

from src.core.optimal_config import optimal_params  # noqa: E402
from src.backtest.ict_backtest import run_ict_backtest  # noqa: E402


# ── Config grid ──────────────────────────────────────────────────────────
# (name, sl_zone_mult). All other knobs are at v7 SNIPER canonical
# (TP=22.0, lots=0.01, contract_size=100.0).
CONFIGURATIONS: dict[str, float] = {
    "SNIPER_22_SL_1x": 1.0,   # v7 canonical baseline
    "SNIPER_22_SL_2x": 2.0,   # v16 recommended (cleanest point on plateau)
    "SNIPER_22_SL_3x": 3.0,   # v16 plateau (highest Sharpe-like ratio)
    "SNIPER_22_SL_5x": 5.0,   # v16 empirical peak (highest PnL/day, tail risk)
}


def _make_config(sl_mult: float):
    p = optimal_params()
    p.fvg_inv_trade_sl_zone_mult = sl_mult
    return p


def _cfg_name(sl_mult: float) -> str:
    """Stable config name (e.g. SNIPER_22_SL_1x)."""
    return f"SNIPER_22_SL_{sl_mult:g}x".replace('.', '_')


def _run_one(sl_mult: float, day_label: str, df_day: pd.DataFrame) -> dict:
    cfg_name = _cfg_name(sl_mult)
    p = _make_config(sl_mult)
    buf = io.StringIO()
    err = ""
    try:
        with contextlib.redirect_stdout(buf):
            res = run_ict_backtest(df_day, p, strategy_label=f"sl_full_{cfg_name}")
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
        'n_sl_exits': 0,
        'n_tp_exits': 0,
        'n_inv_exits': 0,
        'n_eod_exits': 0,
        'median_hold_secs': 0.0,
        'median_sl_dist_usd': 0.0,
        'median_tp_dist_usd': 0.0,
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
        'n_sl_exits': n_sl,
        'n_tp_exits': n_tp,
        'n_inv_exits': n_inv,
        'n_eod_exits': n_eod,
        'median_hold_secs': float(np.median(holds)),
        'median_sl_dist_usd': float(np.median(sl_dists)),
        'median_tp_dist_usd': float(np.median(tp_dists)),
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': profit_factor if np.isfinite(profit_factor) else 999.0,
        'win_rate': win_rate,
        'max_win': float(pnls.max()) if n_t > 0 else 0.0,
        'max_loss': float(pnls.min()) if n_t > 0 else 0.0,
        'error': err,
    }


# ── Data loading — full corpus (corrected day-enumeration pattern) ──────
data_dir = ROOT / 'data'
parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'

t0 = time.time()
# time column is timestamp[ms, UTC] in the 1y parquet
t_table = pq.read_table(str(parquet_path), columns=['time'])
times_ms = t_table.column('time').to_numpy()  # datetime64[ms]
times_ns = times_ms.astype('datetime64[ns]').astype(np.int64)
all_days = np.unique(times_ns // NS_PER_DAY)
day_dows = np.array([
    pd.Timestamp(int(d) * NS_PER_DAY, unit='ns', tz='UTC').dayofweek
    for d in all_days
])
mon_fri_days = all_days[day_dows < 5]
print(f'Day enumeration: {time.time() - t0:.1f}s, '
      f'{len(mon_fri_days):,} Mon-Fri days', flush=True)

# Year split
year_of = lambda d: pd.Timestamp(int(d) * NS_PER_DAY, unit='ns', tz='UTC').year
days_train = np.array(sorted([d for d in mon_fri_days
                              if year_of(d) in (2024, 2025)]))
days_test = np.array(sorted([d for d in mon_fri_days
                             if year_of(d) == 2026]))
print(f'Train: {len(days_train)} days (2024+2025), '
      f'Test: {len(days_test)} days (2026)', flush=True)

dataset = pds.dataset(str(parquet_path), format='parquet')


def _preload(days: np.ndarray) -> dict[str, pd.DataFrame]:
    out = {}
    for day_id in days:
        viz_start = pd.Timestamp(int(day_id) * NS_PER_DAY, unit='ns', tz='UTC')
        viz_end = viz_start + pd.Timedelta(days=1)
        table = dataset.to_table(
            columns=['time', 'open', 'high', 'low', 'close'],
            filter=(pds.field('time') >= viz_start) & (pds.field('time') < viz_end),
        )
        if len(table) < MIN_BARS_PER_DAY:
            continue
        df_day = table.to_pandas().sort_values('time').reset_index(drop=True)
        out[str(viz_start.date())] = df_day
    return out


t_pre = time.time()
print('Pre-loading full corpus...', flush=True)
day_data_train = _preload(days_train)
day_data_test = _preload(days_test)
print(f'Pre-load: {time.time() - t_pre:.1f}s, '
      f'train={len(day_data_train)} test={len(day_data_test)}', flush=True)


# ── Run + summarize ─────────────────────────────────────────────────────
total_jobs = len(CONFIGURATIONS) * (len(day_data_train) + len(day_data_test))
print(f'\nRunning {total_jobs} backtests ({len(CONFIGURATIONS)} configs × '
      f'{len(day_data_train)+len(day_data_test)} days)...', flush=True)

t_start = time.time()
all_runs = []
job_idx = 0
day_data_all = {**day_data_train, **day_data_test}
for sl_mult in CONFIGURATIONS.values():
    cfg_name = _cfg_name(sl_mult)
    print(f'\n=== {cfg_name} ===', flush=True)
    for day_label, df_day in day_data_all.items():
        job_idx += 1
        if job_idx % 100 == 0:
            elapsed = time.time() - t_start
            eta = elapsed * (total_jobs - job_idx) / job_idx
            print(f'  [{job_idx}/{total_jobs}] {cfg_name} {day_label} '
                  f'elapsed={elapsed:.1f}s eta={eta:.1f}s', flush=True)
        row = _run_one(sl_mult, day_label, df_day)
        # Tag train/test + year + month
        day_dt = pd.Timestamp(row['day'])
        row['split'] = 'train' if day_dt.year in (2024, 2025) else 'test'
        row['year'] = day_dt.year
        row['month'] = day_dt.month
        all_runs.append(row)

elapsed_total = time.time() - t_start
print(f'\nDone: {total_jobs} backtests in {elapsed_total:.1f}s '
      f'({total_jobs/elapsed_total:.2f} bt/s)', flush=True)

df = pd.DataFrame(all_runs)
nb_dir = ROOT / 'notebooks'
nb_dir.mkdir(exist_ok=True)
csv_per_day = nb_dir / 'sniper_sl_full_per_day.csv'
df.to_csv(csv_per_day, index=False)
print(f'Wrote {csv_per_day} ({len(df)} rows)', flush=True)


# ── Walk-forward aggregate ───────────────────────────────────────────────
print('\n=== SNIPER SL FULL-CORPUS WALK-FORWARD ===')
print(f'Train: {len(day_data_train)} days (2024+2025) | '
      f'Test: {len(day_data_test)} days (2026) | '
      f'Total: {len(day_data_train)+len(day_data_test)} days')
print('=' * 110)
hdr = (
    f'{"config":<22} {"split":<6} {"trades":>7} {"EV/trade":>10} '
    f'{"PnL/day":>10} {"PnL/yr":>10} {"pos_d":>6} {"neg_d":>6}'
)
print(hdr)
print('-' * 110)

summary_rows = []
for sl_mult in CONFIGURATIONS.values():
    cfg_name = _cfg_name(sl_mult)
    for split in ('train', 'test'):
        g = df[(df.config == cfg_name) & (df.split == split)]
        if len(g) == 0:
            continue
        n_tr = int(g['n_trades'].sum())
        pnl = float(g['pnl_total'].sum())
        ev = pnl / n_tr if n_tr else 0.0
        n_days = len(g)
        pnl_day = pnl / n_days
        # annualize (250 trading days/yr)
        pnl_yr = pnl_day * 250
        pos = int((g.pnl_total > 0).sum())
        neg = int((g.pnl_total < 0).sum())
        zero = int((g.pnl_total == 0).sum())
        print(
            f'{cfg_name:<22} {split:<6} {n_tr:>7} ${ev:>+8.4f} '
            f'${pnl_day:>+8.4f} ${pnl_yr:>+8.0f} '
            f'{pos:>6} {neg:>6}'
        )
        summary_rows.append({
            'config': cfg_name,
            'sl_zone_mult': sl_mult,
            'split': split,
            'n_days': n_days,
            'trades': n_tr,
            'pnl_total': pnl,
            'ev_per_trade': ev,
            'pnl_per_day': pnl_day,
            'pnl_per_year_ann': pnl_yr,
            'pos_days': pos,
            'neg_days': neg,
            'zero_days': zero,
            'sniper_submitted': int(g['n_sniper_submitted'].sum()),
            'sniper_triggered': int(g['n_sniper_triggered'].sum()),
            'sniper_cancelled': int(g['n_sniper_cancelled'].sum()),
            'sniper_expired': int(g['n_sniper_expired'].sum()),
        })

summary_df = pd.DataFrame(summary_rows)
csv_summary = nb_dir / 'sniper_sl_full_summary.csv'
summary_df.to_csv(csv_summary, index=False)
print(f'\nWrote {csv_summary}', flush=True)


# ── Headline delta vs baseline (SNIPER_22_SL_1x) ─────────────────────────
print('\n=== Delta vs SNIPER_22_SL_1x baseline (v7 canonical) ===')
base = summary_df[(summary_df['config'] == _cfg_name(1.0))]
if not base.empty:
    base_train_pnl_day = float(
        base[base['split'] == 'train']['pnl_per_day'].iloc[0])
    base_test_pnl_day = float(
        base[base['split'] == 'test']['pnl_per_day'].iloc[0])
    base_train_ev = float(
        base[base['split'] == 'train']['ev_per_trade'].iloc[0])
    base_test_ev = float(
        base[base['split'] == 'test']['ev_per_trade'].iloc[0])
    print(f'Baseline (SL=1x) train PnL/day=${base_train_pnl_day:+.4f}, '
          f'test PnL/day=${base_test_pnl_day:+.4f}')
    print(f'                  train EV/trade=${base_train_ev:+.4f}, '
          f'test EV/trade=${base_test_ev:+.4f}')
    print()
    for _, r in summary_df.iterrows():
        if r['config'] == _cfg_name(1.0):
            continue
        split = r['split']
        if split == 'train':
            d_pnl = r['pnl_per_day'] - base_train_pnl_day
            d_ev = r['ev_per_trade'] - base_train_ev
        else:
            d_pnl = r['pnl_per_day'] - base_test_pnl_day
            d_ev = r['ev_per_trade'] - base_test_ev
        pct = (r['pnl_per_day'] /
               (base_train_pnl_day if split == 'train' else base_test_pnl_day)
               * 100)
        print(f'  {r["config"]:<22} {split:<6}  '
              f'delta_PnL/day={d_pnl:+.4f}  delta_EV/trade={d_ev:+.4f}  '
              f'pct_of_base={pct:.1f}%')

print(f'\nTotal elapsed: {elapsed_total:.1f}s')