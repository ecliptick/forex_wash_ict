"""Exit-mode diagnostic for v6 OPTIMAL.

Runs the v6 optimal config and reports the exit-reason distribution
(SL / TP / INV / EOD / CANCEL) broken down by signal source and
trade age (in bars). This tells us WHERE the alpha has to come from.

Output:
  - stdout summary table
  - notebooks/exit_mode_diagnostic.csv
"""
from __future__ import annotations
import os
import sys
import time
import io
import contextlib
from collections import defaultdict
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

from src.core.ict_strategy import TrendStrategyParams
from src.backtest.ict_backtest import run_ict_backtest


def _v6_optimal() -> TrendStrategyParams:
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
        fvg_inv_trade_enabled=True,
        gate_on_gmma_bias=False,
        layer_lifetime_secs=7200,
        bos_choch_ignore_invert_when_aligned=True,
        bos_choch_memory_n_events=5,
        fvg_min_lifetime_secs=3,
    )


# ── Data ────────────────────────────────────────────────────────────────
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


# ── Run + collect per-trade stats ───────────────────────────────────────
p = _v6_optimal()

all_trades = []
print(f'\nRunning v6 OPTIMAL on {len(day_data)} days...', flush=True)
t_start = time.time()
for day_label, df_day in day_data.items():
    buf = io.StringIO()
    err = ""
    try:
        with contextlib.redirect_stdout(buf):
            res = run_ict_backtest(df_day, p, strategy_label='v6_optimal_diag')
    except Exception as e:
        err = str(e)
        res = None
    if err:
        print(f'  ERROR on {day_label}: {err}', flush=True)
        continue
    for tr in res.trades:
        all_trades.append({
            'day': day_label,
            'direction': tr.direction,
            'triggered_by': tr.entry_triggered_by,
            'rank_tier': getattr(tr, 'rank_tier', ''),
            'entry_price': tr.entry_price,
            'exit_price': tr.exit_price,
            'pnl_usd': tr.pnl_usd,
            'lots': tr.lots,
            'exit_reason': tr.exit_reason,
            'hold_secs': tr.hold_secs,
            'is_inv': (tr.entry_triggered_by == 'inv'),
        })
elapsed = time.time() - t_start
print(f'Backtests done in {elapsed:.1f}s ({elapsed/len(day_data):.3f}s per day)',
      flush=True)

df = pd.DataFrame(all_trades)
print(f'\nTotal trades: {len(df):,}')
print(f'Total PnL: ${df["pnl_usd"].sum():.2f}')
print(f'EV/trade: ${df["pnl_usd"].mean():+.4f}')
print(f'Wins: {(df["pnl_usd"] > 0).sum():,} ({(df["pnl_usd"] > 0).mean():.1%})')

# ── Exit-reason distribution ────────────────────────────────────────────
print('\n' + '=' * 80)
print('EXIT-REASON DISTRIBUTION (v6 OPTIMAL)')
print('=' * 80)
exit_grp = df.groupby('exit_reason').agg(
    n=('pnl_usd', 'count'),
    avg_pnl=('pnl_usd', 'mean'),
    total_pnl=('pnl_usd', 'sum'),
    pct=('pnl_usd', lambda x: 100 * len(x) / len(df)),
).sort_values('n', ascending=False)
print(exit_grp.to_string())

# ── Exit-reason × source ────────────────────────────────────────────────
print('\n' + '=' * 80)
print('EXIT-REASON × SOURCE')
print('=' * 80)
src_pivot = df.pivot_table(
    index='exit_reason',
    columns='triggered_by',
    values='pnl_usd',
    aggfunc='count',
    fill_value=0,
)
print(src_pivot.to_string())

# ── EV by exit-reason ──────────────────────────────────────────────────
print('\n' + '=' * 80)
print('EV BY EXIT-REASON')
print('=' * 80)
ev_by_exit = df.groupby('exit_reason').agg(
    n=('pnl_usd', 'count'),
    avg_pnl=('pnl_usd', 'mean'),
    total_pnl=('pnl_usd', 'sum'),
).sort_values('avg_pnl', ascending=False)
print(ev_by_exit.to_string())

# ── Bars-held distribution by exit-reason ───────────────────────────────
print('\n' + '=' * 80)
print('HOLD-SECS DISTRIBUTION BY EXIT-REASON')
print('=' * 80)
if 'hold_secs' in df.columns and df['hold_secs'].sum() > 0:
    bh = df.groupby('exit_reason')['hold_secs'].describe()
    print(bh.to_string())

# ── SL hunt analysis: how many SL exits happen within N bars ───────────
print('\n' + '=' * 80)
print('SL-HUNT TIMING: % of SL exits by hold-secs bucket')
print('=' * 80)
if 'hold_secs' in df.columns:
    sl_df = df[df['exit_reason'] == 'sl']
    if len(sl_df) > 0:
        buckets = [0, 1, 2, 3, 5, 10, 30, 60, 300, 1800, 999999]
        labels = ['0-1', '1-2', '2-3', '3-5', '5-10', '10-30', '30-60', '60-300',
                  '300-1800', '1800+']
        sl_df = sl_df.copy()
        sl_df['bucket'] = pd.cut(sl_df['hold_secs'], bins=buckets,
                                  labels=labels, right=True, include_lowest=True)
        bucket_grp = sl_df.groupby('bucket', observed=False).agg(
            n=('pnl_usd', 'count'),
            pct_of_sl=('pnl_usd', lambda x: 100 * len(x) / len(sl_df)),
            avg_pnl=('pnl_usd', 'mean'),
        )
        print(bucket_grp.to_string())

# ── INV trade breakdown ────────────────────────────────────────────────
print('\n' + '=' * 80)
print('INV-TRADE BREAKDOWN')
print('=' * 80)
inv_df = df[df['triggered_by'] == 'inv']
non_inv_df = df[df['triggered_by'] != 'inv']
if len(inv_df) > 0:
    print(f'INV trades: {len(inv_df):,}')
    print(f'INV EV/trade: ${inv_df["pnl_usd"].mean():+.4f}')
    print(f'INV PnL total: ${inv_df["pnl_usd"].sum():.2f}')
    print(f'INV win rate: {(inv_df["pnl_usd"] > 0).mean():.1%}')
    print(f'INV exit reasons:')
    print(inv_df['exit_reason'].value_counts().to_string())
    print(f'\nNon-INV trades: {len(non_inv_df):,}')
    print(f'Non-INV EV/trade: ${non_inv_df["pnl_usd"].mean():+.4f}')
    print(f'Non-INV PnL total: ${non_inv_df["pnl_usd"].sum():.2f}')
    print(f'Non-INV win rate: {(non_inv_df["pnl_usd"] > 0).mean():.1%}')

# ── Where to find alpha: per-tier PnL ──────────────────────────────────
print('\n' + '=' * 80)
print('PER-RANK-TIER PnL')
print('=' * 80)
tier_grp = df.groupby('rank_tier', dropna=False).agg(
    n=('pnl_usd', 'count'),
    avg_pnl=('pnl_usd', 'mean'),
    total_pnl=('pnl_usd', 'sum'),
).sort_values('avg_pnl', ascending=False)
print(tier_grp.to_string())

# ── Save per-trade CSV for further analysis ─────────────────────────────
csv_path = ROOT / 'notebooks' / 'exit_mode_diagnostic.csv'
df.to_csv(csv_path, index=False)
print(f'\nPer-trade CSV: {csv_path}')

# ── Headline insight ───────────────────────────────────────────────────
print('\n' + '=' * 80)
print('HEADLINE INSIGHTS')
print('=' * 80)
total_n = len(df)
sl_pct = (df['exit_reason'] == 'sl').sum() / total_n * 100
tp_pct = (df['exit_reason'] == 'tp').sum() / total_n * 100
inv_pct = (df['exit_reason'] == 'inv').sum() / total_n * 100
print(f'SL exits: {sl_pct:.1f}% of trades — the dominant loss mode')
print(f'TP exits: {tp_pct:.1f}% of trades')
print(f'INV exits: {inv_pct:.1f}% of trades')

# How much would EV improve if we just cut SL-1 trades?
sl1 = df[df['hold_secs'] <= 1] if 'hold_secs' in df.columns else df.iloc[:0]
if len(sl1) > 0:
    sl1_sl = sl1[sl1['exit_reason'] == 'sl']
    if len(sl1_sl) > 0:
        sl1_loss = sl1_sl['pnl_usd'].sum()
        sl1_n = len(sl1_sl)
        new_pnl = df['pnl_usd'].sum() - sl1_loss
        new_n = len(df) - sl1_n
        new_ev = new_pnl / new_n if new_n else 0
        print(f'\nIf we cut the {sl1_n} trades that exit via SL within 1 sec:')
        print(f'  PnL: ${df["pnl_usd"].sum():.2f} → ${new_pnl:.2f} '
              f'(+${-sl1_loss:.2f})')
        print(f'  EV/trade: ${df["pnl_usd"].mean():+.4f} → ${new_ev:+.4f} '
              f'(+${new_ev - df["pnl_usd"].mean():+.4f})')

# Cut the worst N%
def _cut_bottom_pct(df, pct):
    """What if we cut the worst X% of trades by pnl_usd?"""
    sorted_pnl = df['pnl_usd'].sort_values()
    n_cut = int(len(sorted_pnl) * pct)
    cut_pnl = sorted_pnl.iloc[:n_cut].sum()
    remaining_pnl = df['pnl_usd'].sum() - cut_pnl
    remaining_n = len(df) - n_cut
    return remaining_pnl, remaining_n, remaining_pnl / remaining_n if remaining_n else 0


for pct in [0.10, 0.20, 0.30, 0.50]:
    rem_pnl, rem_n, rem_ev = _cut_bottom_pct(df, pct)
    print(f'  Cut bottom {pct:.0%}: PnL ${rem_pnl:+.2f}, '
          f'EV/trade ${rem_ev:+.4f} (n={rem_n})')
