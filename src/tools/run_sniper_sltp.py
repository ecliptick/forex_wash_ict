"""Sniper SL/TP multiplier sweep — v6+ Innovation #1 (added 2026-09-17).

The first sniper A/B (run_sniper_in.py) showed:
  SNIPER    alone : +$0.12 EV/trade, +$0.94/day
  SNIPER_INV stack: +$0.10 EV/trade, +$1.64/day

Both clear the positive-EV bar. This sweep varies the SL/TP
multipliers (the sniper uses fvg_inv_trade_sl_zone_mult / _tp_zone_mult)
and the min_zone filter to find the sniper optimum.

Configs tested:
  1. SNIPER_BASE       — SL=1.0, TP=1.8 (the v6 INV_TRADE default)
  2. SNIPER_TP2_5      — SL=1.0, TP=2.5 (looser TP, 1:2.5 R:R)
  3. SNIPER_TP3_0      — SL=1.0, TP=3.0 (very loose TP, 1:3 R:R)
  4. SNIPER_TP4_0      — SL=1.0, TP=4.0 (max loose TP, 1:4 R:R)
  5. SNIPER_SL07_TP2_0 — SL=0.7, TP=2.0 (tighter SL, 1:2.86 R:R)
  6. SNIPER_SL07_TP2_5 — SL=0.7, TP=2.5 (tighter SL, 1:3.57 R:R)
  7. SNIPER_MINZONE_50 — min zone $0.50 (higher quality zones only)
  8. SNIPER_MINZONE_80 — min zone $0.80 (very high quality zones only)

Usage:
  python src/tools/run_sniper_sltp.py [N_DAYS]
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
N_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 58
RNG_SEED = 20260917
MIN_BARS_PER_DAY = 30_000


def _find_root():
    here = Path('.').resolve()
    for p in [here, *here.parents]:
        if (p / 'src' / 'core' / 'ict_signals.py').is_file():
            return p


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
        entry_mode='sniper',  # ALL configs use sniper
    )


# (name, dict of overrides) — fine grid around the peak
CONFIGURATIONS = {
    "TP13":  {"fvg_inv_trade_tp_zone_mult": 13.0},
    "TP14":  {"fvg_inv_trade_tp_zone_mult": 14.0},
    "TP15":  {"fvg_inv_trade_tp_zone_mult": 15.0},
    "TP16":  {"fvg_inv_trade_tp_zone_mult": 16.0},
    "TP17":  {"fvg_inv_trade_tp_zone_mult": 17.0},
    "TP18":  {"fvg_inv_trade_tp_zone_mult": 18.0},
    "TP19":  {"fvg_inv_trade_tp_zone_mult": 19.0},
    "TP20":  {"fvg_inv_trade_tp_zone_mult": 20.0},
    "TP21":  {"fvg_inv_trade_tp_zone_mult": 21.0},
    "TP22":  {"fvg_inv_trade_tp_zone_mult": 22.0},
    "TP23":  {"fvg_inv_trade_tp_zone_mult": 23.0},
    "TP24":  {"fvg_inv_trade_tp_zone_mult": 24.0},
    "TP25":  {"fvg_inv_trade_tp_zone_mult": 25.0},
}


def _make_config(name: str) -> TrendStrategyParams:
    p = _common()
    for k, v in CONFIGURATIONS[name].items():
        setattr(p, k, v)
    return p


# Load days (same pattern)
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
rng = np.random.default_rng(RNG_SEED)
n_sample = min(N_DAYS, len(unique_days))
sample_days = rng.choice(unique_days, size=n_sample, replace=False)
sample_days.sort()
dataset = pds.dataset(str(parquet_path), format='parquet')
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
print(f'Loaded {len(day_data)} days in {time.time() - t0:.1f}s', flush=True)


def _run_one(cfg_name: str, day_label: str, df_day: pd.DataFrame) -> dict:
    p = _make_config(cfg_name)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            res = run_ict_backtest(df_day, p, strategy_label=f'sltp_{cfg_name}')
    except Exception as e:
        return {'config': cfg_name, 'day': day_label,
                'n_trades': 0, 'pnl_total': 0.0, 'error': str(e)}
    n_t = len(res.trades)
    pnl = float(sum(t.pnl_usd for t in res.trades))
    return {
        'config': cfg_name, 'day': day_label,
        'n_trades': n_t, 'pnl_total': pnl,
        'ev_per_trade': pnl / n_t if n_t else 0.0,
    }


configs = list(CONFIGURATIONS.keys())
nb_dir = ROOT / 'notebooks'
total_jobs = len(configs) * len(day_data)
print(f'\nRunning {total_jobs} backtests ({len(configs)} configs x {len(day_data)} days)', flush=True)
t_start = time.time()
all_runs = []
job_idx = 0
for cfg_name in configs:
    for day_label, df_day in day_data.items():
        job_idx += 1
        if job_idx % 50 == 0:
            elapsed = time.time() - t_start
            eta = elapsed * (total_jobs - job_idx) / job_idx
            print(f'  [{job_idx}/{total_jobs}] elapsed={elapsed:.1f}s eta={eta:.1f}s', flush=True)
        all_runs.append(_run_one(cfg_name, day_label, df_day))

df = pd.DataFrame(all_runs)
df.to_csv(nb_dir / 'sniper_sltp_per_day.csv', index=False)

print(f'\n=== SNIPER SL/TP SWEEP — {len(day_data)} days, seed {RNG_SEED} ===')
print('=' * 90)
hdr = f'{"config":<14} {"trades":>7} {"EV/trade":>10} {"PnL/day":>10} {"pos_days":>9} {"neg_days":>9}'
print(hdr)
print('-' * 90)
for cfg_name, g in df.groupby('config'):
    n_tr = int(g['n_trades'].sum())
    pnl = float(g['pnl_total'].sum())
    ev = pnl / n_tr if n_tr else 0.0
    pnl_day = pnl / len(day_data)
    pos = (g.pnl_total > 0).sum()
    neg = (g.pnl_total < 0).sum()
    print(f'{cfg_name:<14} {n_tr:>7} ${ev:>+8.4f} ${pnl_day:>+8.4f} {pos:>9} {neg:>9}')

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
    })
pd.DataFrame(summary_rows).to_csv(nb_dir / 'sniper_sltp_summary.csv', index=False)
print(f'\nWrote: notebooks/sniper_sltp_summary.csv + per_day.csv')
print(f'Elapsed: {time.time() - t_start:.1f}s')
