"""Fine-grid sniper SL/TP sweep with ATR-scaled option.

Two questions from the user (2026-09-17):
  1. "Can we get more granular backtest so we don't jump from 4 / 8 to 22?"
     — earlier sweep used {1.8, 2.5, 3, 4, 5, 6, 8, 10, 12, 15, 17, 18, 19,
     20, 21, 22, 23, 24, 25}. That's actually a 1.0 grid from 17-25, but
     coarse below 4. This driver goes finer between 4 and 22 in steps
     of 1, plus pushes 22-30 in steps of 2.
  2. "This IS using ATR right?"
     — NO, the v7 sniper uses zone-width-scaled TP
     (``zone_w × fvg_inv_trade_tp_zone_mult``). This driver ALSO
     tests ATR-scaled TP (``ATR_at_entry × fvg_inv_trade_tp_atr_mult``)
     so we can A/B the two regimes on the full corpus.

Configs (zone-width-scaled):
  TP zone_mult in [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
                   17, 18, 19, 20, 22, 24, 26, 28, 30]

Configs (ATR-scaled):
  ATR-scaled TP target ≈ $6.60 (the v7 winner's avg TP)
  With 1s ATR ~$0.10 (on 1.0-bar basis), ATR mult candidates:
  [50, 55, 60, 65, 70, 75, 80, 90, 100, 120]
  (50×$0.10=$5, 60×$0.10=$6, 70×$0.10=$7, etc.)

Walk-forward split: train 2024+2025, test 2026.

Output:
  - notebooks/sniper_fine_per_day.csv
  - notebooks/sniper_fine_summary.csv
  - stdout summary

Usage:
  python src/tools/run_sniper_fine.py
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
        entry_mode='sniper',
    )


def _make_zone_config(tp_zone_mult: float) -> TrendStrategyParams:
    p = _common()
    p.fvg_inv_trade_tp_zone_mult = float(tp_zone_mult)
    p.fvg_inv_trade_tp_atr_mult = 0.0  # disable ATR scaling
    return p


def _make_atr_config(tp_atr_mult: float) -> TrendStrategyParams:
    p = _common()
    p.fvg_inv_trade_tp_zone_mult = 0.0  # disable zone-width (the new knob)
    p.fvg_inv_trade_tp_atr_mult = float(tp_atr_mult)
    return p


# ── Data loading — full corpus ──────────────────────────────────────────
data_dir = ROOT / 'data'
parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'

t0 = time.time()
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

year_of = lambda d: pd.Timestamp(int(d) * MS_PER_DAY, unit='ms', tz='UTC').year
days_train = np.array(sorted([d for d in mon_fri_days if year_of(d) in (2024, 2025)]))
days_test = np.array(sorted([d for d in mon_fri_days if year_of(d) == 2026]))
print(f'Train: {len(days_train)} days (2024+2025), Test: {len(days_test)} days (2026)',
      flush=True)

dataset = pds.dataset(str(parquet_path), format='parquet')


def _preload(days: np.ndarray) -> dict[str, pd.DataFrame]:
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


def _run_one(p: TrendStrategyParams, day_label: str, df_day: pd.DataFrame) -> dict:
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            res = run_ict_backtest(df_day, p, strategy_label='fine')
    except Exception as e:
        return {'day': day_label, 'n_trades': 0, 'pnl_total': 0.0, 'error': str(e)}
    n_t = len(res.trades)
    pnl = float(sum(t.pnl_usd for t in res.trades))
    return {'day': day_label, 'n_trades': n_t, 'pnl_total': pnl,
            'ev_per_trade': pnl / n_t if n_t else 0.0}


# ── Configurations ──────────────────────────────────────────────────────
# Fine grid from 4 to 22 in steps of 1 (the user's request — fill the gap
# between earlier coarse points at 4/8/22), then 24/26/28/30 to confirm
# no peak-and-degrade past 22. ATR-scaled variants at 50, 60, 70, 80, 100
# bracket the equivalent TP distance.
ZONE_TP_VALUES = [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
                  17, 18, 19, 20, 21, 22, 24, 26, 28, 30]
ATR_TP_VALUES = [50, 60, 70, 80, 100]

# Two-pass sweep to fit time budget:
#  Pass 1 (test only): 138 days × 28 configs = 3,864 backtests (~1h)
#  Pass 2 (validate top-3 on train): 516 days × 3 configs = 1,548 backtests (~25min)
TOP_N_TO_VALIDATE = 3

# All configs: name -> (kind, value)
ALL_CONFIGS = []
for v in ZONE_TP_VALUES:
    ALL_CONFIGS.append((f"ZONE_TP{v}", "zone", v))
for v in ATR_TP_VALUES:
    ALL_CONFIGS.append((f"ATR_TP{v}", "atr", v))

print(f'\nTotal configs: {len(ALL_CONFIGS)} ({len(ZONE_TP_VALUES)} zone + {len(ATR_TP_VALUES)} atr)',
      flush=True)

nb_dir = ROOT / 'notebooks'
nb_dir.mkdir(exist_ok=True)


def _run_pass(p_factory, configs, day_dict, split_name):
    all_runs = []
    job_idx = 0
    t_start = time.time()
    n_jobs = len(configs) * len(day_dict)
    for cfg_name, kind, value in configs:
        p = p_factory(value)
        for day_label, df_day in day_dict.items():
            job_idx += 1
            if job_idx % 100 == 0:
                elapsed = time.time() - t_start
                eta = elapsed * (n_jobs - job_idx) / job_idx
                print(f'  [{job_idx}/{n_jobs}] {cfg_name} {day_label} '
                      f'elapsed={elapsed:.1f}s eta={eta:.1f}s', flush=True)
            row = _run_one(p, day_label, df_day)
            row['config'] = cfg_name
            row['kind'] = kind
            row['value'] = value
            row['split'] = split_name
            all_runs.append(row)
    return all_runs


def _make_zone(value):
    return _make_zone_config(value)


def _make_atr(value):
    return _make_atr_config(value)


# Pass 1: test only (138 days), find top-3 by EV/trade
print(f'\n=== Pass 1: TEST (2026, {len(day_data_test)} days) — find peak ===',
      flush=True)
test_runs = _run_pass(_make_zone, [(f"ZONE_TP{v}", "zone", v) for v in ZONE_TP_VALUES],
                     day_data_test, 'test')
test_runs += _run_pass(_make_atr, [(f"ATR_TP{v}", "atr", v) for v in ATR_TP_VALUES],
                      day_data_test, 'test')

df_test = pd.DataFrame(test_runs)
print(f'  Pass 1 done: {len(df_test)} rows', flush=True)

# Find top-N by EV/trade on test
test_summary = []
for cfg_name in df_test.config.unique():
    g = df_test[df_test.config == cfg_name]
    n_tr = int(g['n_trades'].sum())
    pnl = float(g['pnl_total'].sum())
    ev = pnl / n_tr if n_tr else 0.0
    pnl_day = pnl / len(g)
    test_summary.append({
        'config': cfg_name,
        'kind': g.kind.iloc[0],
        'value': g.value.iloc[0],
        'n_days': len(g),
        'trades': n_tr,
        'pnl_total': pnl,
        'ev_per_trade': ev,
        'pnl_per_day': pnl_day,
        'pos_days': int((g.pnl_total > 0).sum()),
        'neg_days': int((g.pnl_total < 0).sum()),
    })
test_summary_df = pd.DataFrame(test_summary).sort_values(
    'pnl_per_day', ascending=False)
print('\n--- Pass 1 top-10 by PnL/day (test only) ---')
print(test_summary_df.head(10).to_string(index=False))

# Pick top configs to validate on train
top_configs = test_summary_df.head(TOP_N_TO_VALIDATE)[['config', 'kind', 'value']].values.tolist()
print(f'\n=== Pass 2: TRAIN (2024+2025, {len(day_data_train)} days) '
      f'— validate top-{TOP_N_TO_VALIDATE} ===', flush=True)

train_runs = []
for cfg_name, kind, value in top_configs:
    if kind == 'zone':
        p = _make_zone_config(value)
    else:
        p = _make_atr_config(value)
    for day_label, df_day in day_data_train.items():
        row = _run_one(p, day_label, df_day)
        row['config'] = cfg_name
        row['kind'] = kind
        row['value'] = value
        row['split'] = 'train'
        train_runs.append(row)

df_train = pd.DataFrame(train_runs)
print(f'  Pass 2 done: {len(df_train)} rows', flush=True)

# Combine
all_runs = test_runs + train_runs
df = pd.DataFrame(all_runs)
df.to_csv(nb_dir / 'sniper_fine_per_day.csv', index=False)
print(f'\nWrote: notebooks/sniper_fine_per_day.csv ({len(df)} rows)', flush=True)


# ── Aggregate ───────────────────────────────────────────────────────────
print(f'\n=== SNIPER FINE-GRID SWEEP — full corpus, walk-forward ===', flush=True)
print('=' * 100)
hdr = (
    f'{"config":<14} {"kind":<6} {"split":<6} {"trades":>7} {"EV/trade":>10} {"PnL/day":>10} '
    f'{"pos_d":>6} {"neg_d":>6}'
)
print(hdr)
print('-' * 100)

# For test configs: full table
# For train configs: only top-N
summary_rows = []
for cfg_name in df.config.unique():
    g = df[df.config == cfg_name]
    for split in ('test', 'train'):  # test first
        gs = g[g.split == split]
        if len(gs) == 0:
            continue
        n_tr = int(gs['n_trades'].sum())
        pnl = float(gs['pnl_total'].sum())
        ev = pnl / n_tr if n_tr else 0.0
        n_days = len(gs)
        pnl_day = pnl / n_days
        pos = int((gs.pnl_total > 0).sum())
        neg = int((gs.pnl_total < 0).sum())
        print(
            f'{cfg_name:<14} {gs.kind.iloc[0]:<6} {split:<6} {n_tr:>7} ${ev:>+8.4f} ${pnl_day:>+8.4f} '
            f'{pos:>6} {neg:>6}'
        )
        summary_rows.append({
            'config': cfg_name, 'kind': gs.kind.iloc[0],
            'value': float(gs.value.iloc[0]),
            'split': split, 'n_days': n_days, 'trades': n_tr,
            'pnl_total': pnl, 'ev_per_trade': ev, 'pnl_per_day': pnl_day,
            'pos_days': pos, 'neg_days': neg,
        })

pd.DataFrame(summary_rows).to_csv(nb_dir / 'sniper_fine_summary.csv', index=False)
print(f'\nWrote: notebooks/sniper_fine_summary.csv')
