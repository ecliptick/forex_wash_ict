"""ATR-anchored SL/TP sweep on top of v6 OPTIMAL.

The user's hypothesis: sub-second SL exits dominate because the
SL is anchored to the zone edge (~$0.05-$0.30) which is SMALLER
than the typical 1s bar range (~$0.13) — the SL is in the noise.

Fix: anchor SL/TP to recent ATR (sl_atr_mult × ATR, tp_atr_mult × ATR).
This bypasses the zone-edge anchor entirely and gives the trade
room to survive normal fluctuation.

Driver:
    python src/tools/run_atr_anchor_sweep.py 60

Output:
    notebooks/atr_anchor_sweep_summary.csv
    notebooks/atr_anchor_sweep_per_day.csv
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
RNG_SEED = 20260917
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
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

from src.core.ict_strategy import TrendStrategyParams
from src.backtest.ict_backtest import run_ict_backtest


def _v6_optimal(**overrides) -> TrendStrategyParams:
    base = dict(
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
        fvg_inv_trade_enabled=True,
        gate_on_gmma_bias=False,
        layer_lifetime_secs=7200,
        bos_choch_ignore_invert_when_aligned=True,
        bos_choch_memory_n_events=5,
        fvg_min_lifetime_secs=3,
        # ATR-anchor DEFAULT OFF (preserves v6 OPTIMAL behaviour)
        atr_anchor_sl_tp=False,
    )
    base.update(overrides)
    return TrendStrategyParams(**base)


# ── Data loading ────────────────────────────────────────────────────────
data_dir = ROOT / 'data'
if not (data_dir / 'XAUUSD_S1_1y.parquet').exists():
    for ancestor in [ROOT.parent, *ROOT.parent.parents]:
        sibling = ancestor / 'data'
        if (sibling / 'XAUUSD_S1_1y.parquet').exists():
            data_dir = sibling
            break
parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'

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
print(f'{len(unique_days):,} Mon-Fri days in corpus')

rng = np.random.default_rng(RNG_SEED)
n_sample = min(N_DAYS, len(unique_days))
sample_days = rng.choice(unique_days, size=n_sample, replace=False)
sample_days.sort()
print(f'Sampling {n_sample} days with seed {RNG_SEED}')

dataset = pds.dataset(str(parquet_path), format='parquet')
print('Pre-loading sample days...')
t_pre = time.time()
day_data = {}
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
print(f'Pre-load: {time.time() - t_pre:.1f}s, {len(day_data)} days')


# ── Config grid ─────────────────────────────────────────────────────────
# (label, kwargs) — each row overrides the v6 OPTIMAL with the ATR-anchor
# combination being tested.
CONFIGS = [
    # Reference (v6 OPTIMAL, ATR-anchor OFF)
    ('v6_optimal',                         {'atr_anchor_sl_tp': False}),

    # ATR-anchor: scaling the SL by N × ATR (where N is the multiplier)
    # Typical ATR(1200) on XAUUSD 1s ≈ $0.17
    #
    # Goal: SL ≥ 1 typical 1s bar range ($0.13), TP ≥ $0.20
    # sl_atr_mult × 0.17 ≥ 0.13 → mult ≥ 0.77 (sl of ~$0.13)
    # For "stop being in the noise": sl_atr_mult ≈ 2.5 → SL ≈ $0.42
    # Sensible R:R ≈ 1:2.25 → tp_atr_mult = sl_atr_mult × 2.25
    ('atr_anchor_m0p5_m1p1',                {'atr_anchor_sl_tp': True, 'sl_atr_mult': 0.5,  'tp_atr_mult': 1.1}),  # SL=$0.085, TP=$0.19 (still tight)
    ('atr_anchor_m1p0_m2p0',                {'atr_anchor_sl_tp': True, 'sl_atr_mult': 1.0,  'tp_atr_mult': 2.0}),  # SL=$0.17, TP=$0.34 (1:2)
    ('atr_anchor_m1p5_m3p0',                {'atr_anchor_sl_tp': True, 'sl_atr_mult': 1.5,  'tp_atr_mult': 3.0}),  # SL=$0.255, TP=$0.51 (1:2)
    ('atr_anchor_m2p0_m4p0',                {'atr_anchor_sl_tp': True, 'sl_atr_mult': 2.0,  'tp_atr_mult': 4.0}),  # SL=$0.34, TP=$0.68 (1:2)
    ('atr_anchor_m2p5_m5p5',                {'atr_anchor_sl_tp': True, 'sl_atr_mult': 2.5,  'tp_atr_mult': 5.5}),  # SL=$0.42, TP=$0.94 (1:2.25 — match v6 R:R)
    ('atr_anchor_m3p0_m6p0',                {'atr_anchor_sl_tp': True, 'sl_atr_mult': 3.0,  'tp_atr_mult': 6.0}),  # SL=$0.51, TP=$1.02 (1:2)
    ('atr_anchor_m3p5_m7p0',                {'atr_anchor_sl_tp': True, 'sl_atr_mult': 3.5,  'tp_atr_mult': 7.0}),  # SL=$0.595, TP=$1.19 (1:2)
    ('atr_anchor_m4p0_m8p0',                {'atr_anchor_sl_tp': True, 'sl_atr_mult': 4.0,  'tp_atr_mult': 8.0}),  # SL=$0.68, TP=$1.36 (1:2)
    ('atr_anchor_m2p5_m3p0',                {'atr_anchor_sl_tp': True, 'sl_atr_mult': 2.5,  'tp_atr_mult': 3.0}),  # SL=$0.42, TP=$0.51 (1:1.2 — tight TP)
    ('atr_anchor_m2p5_m10p0',               {'atr_anchor_sl_tp': True, 'sl_atr_mult': 2.5,  'tp_atr_mult': 10.0}),  # SL=$0.42, TP=$1.70 (1:4 — wide TP)
]


# ── Run sweep ──────────────────────────────────────────────────────────
def _run_one(label: str, kwargs: dict, day_data, dataset) -> tuple[pd.DataFrame, dict]:
    p = _v6_optimal(**kwargs)
    all_trades = []
    days_run = []
    for day_label, df_day in day_data.items():
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                res = run_ict_backtest(df_day, p, strategy_label=label)
        except Exception as e:
            print(f'  ERROR on {day_label}: {e}')
            continue
        for tr in res.trades:
            all_trades.append({
                'config': label,
                'day': day_label,
                'direction': tr.direction,
                'triggered_by': tr.entry_triggered_by,
                'pnl_usd': tr.pnl_usd,
                'exit_reason': tr.exit_reason,
                'hold_secs': tr.hold_secs,
                'is_inv': (tr.entry_triggered_by == 'inv'),
                'stop_usd': tr.stop_usd,
                'target_usd': tr.target_usd,
            })
        days_run.append(day_label)

    df = pd.DataFrame(all_trades)
    summary = {
        'config': label,
        'days': len(days_run),
        'trades': len(df),
        'ev_per_trade': df['pnl_usd'].mean() if len(df) else 0.0,
        'pnl_total': df['pnl_usd'].sum() if len(df) else 0.0,
        'pnl_per_day': (df['pnl_usd'].sum() / len(days_run)) if days_run else 0.0,
        'win_rate': (df['pnl_usd'] > 0).mean() if len(df) else 0.0,
        'trades_per_day': len(df) / len(days_run) if days_run else 0.0,
        'n_sl': (df['exit_reason'] == 'sl').sum(),
        'n_tp': (df['exit_reason'] == 'tp').sum(),
        'n_inv': (df['exit_reason'] == 'inv').sum(),
        'n_subsec_sl': ((df['exit_reason'] == 'sl') & (df['hold_secs'] <= 1)).sum(),
        'pct_subsec_sl': ((df['exit_reason'] == 'sl') & (df['hold_secs'] <= 1)).sum() / len(df) * 100 if len(df) else 0.0,
        'avg_hold_secs': df['hold_secs'].mean() if len(df) else 0.0,
        'avg_win': df.loc[df['pnl_usd'] > 0, 'pnl_usd'].mean() if (df['pnl_usd'] > 0).any() else 0.0,
        'avg_loss': df.loc[df['pnl_usd'] <= 0, 'pnl_usd'].mean() if (df['pnl_usd'] <= 0).any() else 0.0,
        'kwargs': kwargs,
    }
    return df, summary


print(f'\nRunning {len(CONFIGS)} configs on {len(day_data)} days each')
t0 = time.time()
all_dfs = []
all_summaries = []
for label, kwargs in CONFIGS:
    t1 = time.time()
    print(f'  [{len(all_summaries)+1:2d}/{len(CONFIGS)}] {label} ...', end=' ', flush=True)
    df, summary = _run_one(label, kwargs, day_data, dataset)
    all_dfs.append(df)
    all_summaries.append(summary)
    print(f'trades={summary["trades"]:>5d}, '
          f'EV/trade=${summary["ev_per_trade"]:+.4f}, '
          f'PnL/day=${summary["pnl_per_day"]:+.2f}, '
          f'subsec_SL={summary["pct_subsec_sl"]:.1f}% '
          f'({time.time()-t1:.1f}s)')
elapsed = time.time() - t0
print(f'\nTotal sweep: {elapsed:.1f}s ({elapsed/(len(CONFIGS)*len(day_data)):.3f}s/day/config)')


# ── Save + report ──────────────────────────────────────────────────────
sum_df = pd.DataFrame(all_summaries).sort_values('ev_per_trade', ascending=False)
per_day = pd.concat(all_dfs, ignore_index=True)

summary_csv = ROOT / 'notebooks' / 'atr_anchor_sweep_summary.csv'
perday_csv = ROOT / 'notebooks' / 'atr_anchor_sweep_per_day.csv'
sum_df.to_csv(summary_csv, index=False)
per_day.to_csv(perday_csv, index=False)

print('\n' + '=' * 90)
print('ATR-ANCHOR SWEEP — sorted by EV/trade')
print('=' * 90)
print(f'{"Config":<28} {"#":>5} {"EV/trade":>10} {"PnL/day":>10} {"tr/day":>7} {"WR":>6} {"%subsec":>8} {"SL":>4} {"TP":>4} {"INV":>4}')
print('-' * 90)
for s in sum_df.to_dict('records'):
    print(f'{s["config"]:<28} {s["trades"]:>5d} '
          f'{s["ev_per_trade"]:>+.4f} {s["pnl_per_day"]:>+.2f} '
          f'{s["trades_per_day"]:>7.1f} {s["win_rate"]*100:>5.1f}% '
          f'{s["pct_subsec_sl"]:>7.1f}% '
          f'{s["n_sl"]:>4d} {s["n_tp"]:>4d} {s["n_inv"]:>4d}')

print(f'\nSummary CSV: {summary_csv}')
print(f'Per-day CSV: {perday_csv}')

# Headline comparison vs v6 OPTIMAL baseline
ref = next(s for s in all_summaries if s['config'] == 'v6_optimal')
best = sum_df.iloc[0].to_dict()
print('\n' + '=' * 90)
print('HEADLINE')
print('=' * 90)
print(f'Baseline (v6 OPTIMAL):   EV/trade ${ref["ev_per_trade"]:+.4f}  PnL/day ${ref["pnl_per_day"]:+.2f}  subsec_sl={ref["pct_subsec_sl"]:.1f}%')
print(f'Best    ({best["config"]}): EV/trade ${best["ev_per_trade"]:+.4f}  PnL/day ${best["pnl_per_day"]:+.2f}  subsec_sl={best["pct_subsec_sl"]:.1f}%')
print(f'Improvement:     EV/trade ${best["ev_per_trade"]-ref["ev_per_trade"]:+.4f}  PnL/day ${best["pnl_per_day"]-ref["pnl_per_day"]:+.2f}')
positives = sum_df[sum_df['ev_per_trade'] > 0]
print(f'Configs with POSITIVE EV: {len(positives)}/{len(sum_df)}')
if len(positives) > 0:
    print('  ' + ', '.join(positives['config'].tolist()))
