"""Full-corpus sniper-in mode A/B test — v7+ validation (added 2026-09-17).

The 58-day sample may be overfit (user pushback, 2026-09-17). This
driver runs the v7 winner and a few key variants across the FULL
1-year corpus (~660 Mon-Fri UTC days) to confirm robustness.

Configs tested (kept minimal to fit time budget):
  1. IMMEDIATE  — entry_mode='immediate' (v6 OPTIMAL baseline, no INV_TRADE)
  2. SNIPER_22  — entry_mode='sniper', fvg_inv_trade_tp_zone_mult=22.0
  3. SNIPER_8   — entry_mode='sniper', fvg_inv_trade_tp_zone_mult=8.0  (sanity check: monotonic?)
  4. SNIPER_4   — entry_mode='sniper', fvg_inv_trade_tp_zone_mult=4.0

Walk-forward split:
  - Train: 2024 + 2025 Mon-Fri days (~521 days)
  - Test:  2026 Mon-Fri days (~139 days)
  Reports per-config EV/trade on each half independently.

Output:
  - notebooks/sniper_full_per_day.csv   per-day breakdown (all 660 days)
  - notebooks/sniper_full_summary.csv   per-config totals
  - stdout summary table

Usage:
  python src/tools/run_sniper_full.py
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

from src.core.ict_strategy import TrendStrategyParams
from src.backtest.ict_backtest import run_ict_backtest


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
        fvg_min_lifetime_secs=3,
    )


# (name, dict of overrides)
CONFIGURATIONS = {
    "IMMEDIATE":  {"entry_mode": "immediate"},
    "SNIPER_4":   {"entry_mode": "sniper", "fvg_inv_trade_tp_zone_mult": 4.0},
    "SNIPER_8":   {"entry_mode": "sniper", "fvg_inv_trade_tp_zone_mult": 8.0},
    "SNIPER_22":  {"entry_mode": "sniper", "fvg_inv_trade_tp_zone_mult": 22.0},
}


def _make_config(name: str) -> TrendStrategyParams:
    p = _common()
    for k, v in CONFIGURATIONS[name].items():
        setattr(p, k, v)
    return p


# ── Data loading — full corpus ──────────────────────────────────────────
data_dir = ROOT / 'data'
parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'

t0 = time.time()
# time column is millisecond precision
t_table_ms = pq.read_table(str(parquet_path), columns=['time']).cast(
    pa.schema([pa.field('time', pa.int64())])
)
ms = t_table_ms.column('time').to_numpy(zero_copy_only=False).astype(np.int64)
days_ms = ms // MS_PER_DAY
all_days = np.unique(days_ms)
day_dows = np.array([
    pd.Timestamp(int(d) * MS_PER_DAY, unit='ms', tz='UTC').dayofweek
    for d in all_days
])
mon_fri_days = all_days[day_dows < 5]
print(f'Day enumeration: {time.time() - t0:.1f}s, {len(mon_fri_days):,} Mon-Fri days',
      flush=True)

# Year split
year_of = lambda d: pd.Timestamp(int(d) * MS_PER_DAY, unit='ms', tz='UTC').year
days_train = np.array(sorted([d for d in mon_fri_days if year_of(d) in (2024, 2025)]))
days_test = np.array(sorted([d for d in mon_fri_days if year_of(d) == 2026]))
print(f'Train: {len(days_train)} days (2024+2025), Test: {len(days_test)} days (2026)',
      flush=True)

dataset = pds.dataset(str(parquet_path), format='parquet')


def _preload(days: np.ndarray) -> dict[str, pd.DataFrame]:
    """Pre-load all days into memory (this is ~660 days × ~80k bars = 53M bars)."""
    out = {}
    for day_id in days:
        viz_start = pd.Timestamp(int(day_id) * MS_PER_DAY, unit='ms', tz='UTC')
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
print(f'Pre-load: {time.time() - t_pre:.1f}s, train={len(day_data_train)} test={len(day_data_test)}',
      flush=True)


# ── Run + summarize ─────────────────────────────────────────────────────
def _run_one(cfg_name: str, day_label: str, df_day: pd.DataFrame) -> dict:
    p = _make_config(cfg_name)
    buf = io.StringIO()
    err = ""
    try:
        with contextlib.redirect_stdout(buf):
            res = run_ict_backtest(df_day, p, strategy_label=f'full_{cfg_name}')
    except Exception as e:
        err = str(e)
        res = None
    if res is None:
        return {
            'config': cfg_name, 'day': day_label,
            'n_trades': 0, 'pnl_total': 0.0, 'ev_per_trade': 0.0,
            'n_sniper_submitted': 0, 'n_sniper_triggered': 0,
            'n_sniper_cancelled': 0, 'n_sniper_expired': 0,
            'error': err,
        }
    n_t = len(res.trades)
    pnl = float(sum(t.pnl_usd for t in res.trades))
    return {
        'config': cfg_name, 'day': day_label,
        'n_trades': n_t, 'pnl_total': pnl,
        'ev_per_trade': pnl / n_t if n_t else 0.0,
        'n_sniper_submitted': res.n_sniper_submitted,
        'n_sniper_triggered': res.n_sniper_triggered,
        'n_sniper_cancelled': res.n_sniper_cancelled,
        'n_sniper_expired': res.n_sniper_expired,
        'error': err,
    }


configs = list(CONFIGURATIONS.keys())
nb_dir = ROOT / 'notebooks'
nb_dir.mkdir(exist_ok=True)
total_jobs = len(configs) * (len(day_data_train) + len(day_data_test))
print(f'\nRunning {total_jobs} backtests ({len(configs)} configs × '
      f'{len(day_data_train)+len(day_data_test)} days)...', flush=True)

t_start = time.time()
all_runs = []
job_idx = 0
for cfg_name in configs:
    print(f'\n=== {cfg_name} ===', flush=True)
    for day_label, df_day in {**day_data_train, **day_data_test}.items():
        job_idx += 1
        if job_idx % 100 == 0:
            elapsed = time.time() - t_start
            eta = elapsed * (total_jobs - job_idx) / job_idx
            print(f'  [{job_idx}/{total_jobs}] {cfg_name} {day_label} '
                  f'elapsed={elapsed:.1f}s eta={eta:.1f}s', flush=True)
        row = _run_one(cfg_name, day_label, df_day)
        # Tag train/test
        day_year = pd.Timestamp(row['day']).year
        row['split'] = 'train' if day_year in (2024, 2025) else 'test'
        all_runs.append(row)

df = pd.DataFrame(all_runs)
df.to_csv(nb_dir / 'sniper_full_per_day.csv', index=False)
print(f'\nWrote: notebooks/sniper_full_per_day.csv ({len(df)} rows)', flush=True)


# ── Aggregate ───────────────────────────────────────────────────────────
print(f'\n=== SNIPER-IN MODE FULL-CORPUS A/B — '
      f'{len(day_data_train)+len(day_data_test)} Mon-Fri UTC days ===', flush=True)
print('=' * 110)
hdr = (
    f'{"config":<11} {"split":<6} {"trades":>7} {"EV/trade":>10} {"PnL/day":>10} '
    f'{"pos_d":>6} {"neg_d":>6} {"zero_d":>7}'
)
print(hdr)
print('-' * 110)
for cfg_name in configs:
    for split in ('train', 'test'):
        g = df[(df.config == cfg_name) & (df.split == split)]
        if len(g) == 0:
            continue
        n_tr = int(g['n_trades'].sum())
        pnl = float(g['pnl_total'].sum())
        ev = pnl / n_tr if n_tr else 0.0
        n_days = len(g)
        pnl_day = pnl / n_days
        pos = int((g.pnl_total > 0).sum())
        neg = int((g.pnl_total < 0).sum())
        zero = int((g.pnl_total == 0).sum())
        print(
            f'{cfg_name:<11} {split:<6} {n_tr:>7} ${ev:>+8.4f} ${pnl_day:>+8.4f} '
            f'{pos:>6} {neg:>6} {zero:>7}'
        )

# ── Combined summary CSV ───────────────────────────────────────────────
summary_rows = []
for cfg_name in configs:
    for split in ('train', 'test'):
        g = df[(df.config == cfg_name) & (df.split == split)]
        n_tr = int(g['n_trades'].sum())
        pnl = float(g['pnl_total'].sum())
        summary_rows.append({
            'config': cfg_name,
            'split': split,
            'n_days': len(g),
            'trades': n_tr,
            'pnl_total': pnl,
            'ev_per_trade': pnl / n_tr if n_tr else 0.0,
            'pnl_per_day': pnl / len(g) if len(g) else 0.0,
            'pos_days': int((g.pnl_total > 0).sum()),
            'neg_days': int((g.pnl_total < 0).sum()),
            'zero_days': int((g.pnl_total == 0).sum()),
            'sniper_submitted': int(g['n_sniper_submitted'].sum()),
            'sniper_triggered': int(g['n_sniper_triggered'].sum()),
            'sniper_cancelled': int(g['n_sniper_cancelled'].sum()),
            'sniper_expired': int(g['n_sniper_expired'].sum()),
        })
pd.DataFrame(summary_rows).to_csv(nb_dir / 'sniper_full_summary.csv', index=False)
print(f'\nWrote: notebooks/sniper_full_summary.csv')
print(f'\nTotal elapsed: {time.time() - t_start:.1f}s')
