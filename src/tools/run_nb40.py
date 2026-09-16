#!/usr/bin/env python
"""nb40 - measure EV/trade per candlestick tier (A/B/C/D).

Runs the full ICT backtest (run_ict_backtest) on each sampled day,
then re-labels every closed trade by the candlestick A/B/C/D tier of
its source FVG zone. Reports EV/trade per tier.

Key question the user asked:
  "Are A/B/C/D tier FVGs actually profitable? Or are they
   all just losing on average, and we need to know how to
   filter out wrong inversions?"

Setup: standard ICT-only backtest config (matches build_nb36.py):
  - signal_source="fvg", additional_sources=["ifvg"]
  - num_layers=3, inverse_breadth=True
  - invalidation_sl_usd=0.05 (soft-stop on)
  - fvg_require_retest_to_invert=True (default)
  - ms_min_conviction=0.0 (no conviction gate -- we want to
    measure what the existing rules do)
"""
# %%
from __future__ import annotations
import os
import sys
import time
from pathlib import Path

os.environ.setdefault('MPLBACKEND', 'Agg')
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pds
import pyarrow.parquet as pq

NS_PER_DAY = 86_400_000_000_000

# Knobs
N_DAYS = 60  # <-- adjust to taste (we wanted 200; kept 60 here for fast iteration).
                 # When re-running for the full sample, bump to 200.
RNG_SEED = 20260915
MIN_BARS_PER_DAY = 30_000

# Tier classifier knobs (match nb38)
TIER_A_DISPLACEMENT_RATIO = 2.0
TIER_AB_DISPLACEMENT_RATIO = 1.5
TIER_A_MIN_DEPTH = 0.50
TIER_AB_MIN_DEPTH = 0.25
D_PIERCED_AND_INVERTED_MAX_AGE_BARS = 600

# Repo root
def _find_root():
    here = Path('.').resolve()
    for p in [here, *here.parents]:
        if (p / 'src' / 'core' / 'ict_signals.py').is_file():
            return p
    raise RuntimeError('no repo root')

ROOT = _find_root()
sys.path.insert(0, str(ROOT))
from src.core.ict_signals import (
    detect_fvg, generate_ict_pending_signals, compute_simple_atr, IctSeries,
)
from src.core.market_structure import detect_market_structure
from src.core.ict_strategy import TrendStrategyParams
from src.backtest.ict_backtest import run_ict_backtest


def _classify_zone(z, body_size, c1_body, c3_body, struct_dir):
    if (z.pierced_bar >= 0
            and z.inverted_bar >= 0
            and (z.inverted_bar - z.pierced_bar) <= D_PIERCED_AND_INVERTED_MAX_AGE_BARS):
        return 'D'
    bigger_outer = max(c1_body, c3_body)
    disp_ratio = (body_size / bigger_outer) if bigger_outer > 0 else 0.0
    depth_ok_a = z.mitigated_depth_pct >= TIER_A_MIN_DEPTH
    depth_ok_b = z.mitigated_depth_pct >= TIER_AB_MIN_DEPTH
    disp_ok_a = disp_ratio >= TIER_A_DISPLACEMENT_RATIO
    disp_ok_b = disp_ratio >= TIER_AB_DISPLACEMENT_RATIO
    struct_ok = struct_dir == z.direction
    if disp_ok_a and depth_ok_a and struct_ok:
        return 'A'
    if disp_ok_b and depth_ok_b:
        return 'B'
    return 'C'


def _classify_all(zones, body_arr, trend_per_1s):
    """Return dict[zone_id] -> tier."""
    tier_by_id = {}
    for z in zones:
        if z.trigger_bar < 2 or z.trigger_bar >= len(body_arr) - 1:
            continue
        c1_body = float(body_arr[z.trigger_bar - 2])
        c3_body = float(body_arr[z.trigger_bar])
        c2_body = float(body_arr[z.trigger_bar - 1])
        struct_dir_at = int(trend_per_1s[z.trigger_bar - 1])
        tier_by_id[id(z)] = _classify_zone(
            z, c2_body, c1_body, c3_body, struct_dir_at
        )
    return tier_by_id


# Build params: standard ICT config matching build_nb36.
def _make_params():
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
        ms_min_conviction=0.0,        # NO conviction gate (default)
        ms_boost_conviction=1.5,      # widen TP on aligned entries
        use_atr_scaling=True,
        atr_len=1200,
        sl_atr_mult=0.25, tp_atr_mult=0.55,
        sl_usd=0.80, tp_usd=1.80, lots=0.01,
        fvg_require_retest_to_invert=True,
        fvg_invalidation_min_pierce_usd=0.05,
        fvg_invalidation_min_consecutive_bars=2,
        fvg_supersede_on_new=True,
        # Renko / sweep / structure-invalidation OFF -- we want
        # to measure the existing strategy's behaviour cleanly.
        renko_drive_invalidation=False,
        fvg_sweep_enabled=False,
        fvg_invalidate_on_structure=False,
    )


# Data
data_dir = ROOT / 'data'
if not (data_dir / 'XAUUSD_S1_1y.parquet').exists():
    for ancestor in [ROOT.parent, *ROOT.parent.parents]:
        sibling = ancestor / 'data'
        if (sibling / 'XAUUSD_S1_1y.parquet').exists():
            data_dir = sibling
            break
parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'

# Enumerate Mon-Fri days
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
print(f'Day enumeration: {time.time() - t0:.1f}s, {len(unique_days):,} Mon-Fri days')

rng = np.random.default_rng(RNG_SEED)
n_sample = min(N_DAYS, len(unique_days))
sample_days = rng.choice(unique_days, size=n_sample, replace=False)
sample_days.sort()
print(f'Sampled {n_sample} days')

# %% [markdown]
# ### Per-day backtest loop
#
# For each day:
#   1. Read 1s OHLC for the day window
#   2. Run detect_fvg + classify A/B/C/D
#   3. Run the FULL run_ict_backtest on the day
#   4. For each closed trade, look up the source FVG's tier
#      via the trade's ``fvg_zone`` reference
#   5. Append to per-trade tier dataframe
#
# %%
# ── Per-day backtest loop ─────────────────────────────────────────────────
# For each day:
#   1. Read 1s OHLC for the day window
#   2. Run detect_fvg + classify A/B/C/D
#   3. Run the FULL run_ict_backtest on the day
#   4. For each closed trade, look up the source FVG's tier
#      via the trade's ``fvg_zone`` reference
#   5. Append to per-trade tier dataframe
all_trades = []
day_summary = []
skipped_days = 0
t_start = time.time()
dataset = pds.dataset(str(parquet_path), format='parquet')

# Helper: read a single day + compute zones + classify
def _compute_zones_for_day(viz_start, viz_end):
    table = dataset.to_table(
        columns=['time', 'open', 'high', 'low', 'close', 'tickv'],
        filter=(pds.field('time') >= viz_start) & (pds.field('time') < viz_end),
    )
    n_bars = len(table)
    if n_bars < MIN_BARS_PER_DAY:
        return None
    df_day = table.to_pandas().sort_values('time').reset_index(drop=True)
    df_day = df_day.rename(columns={'tickv': 'volume'})
    opens = df_day['open'].to_numpy()
    highs = df_day['high'].to_numpy()
    lows = df_day['low'].to_numpy()
    closes = df_day['close'].to_numpy()

    df_day['time_naive'] = df_day['time'].dt.tz_convert(None)
    df_1m = (df_day.set_index('time_naive')[['open', 'high', 'low', 'close']]
             .resample('1min')
             .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
             .dropna())
    structure = detect_market_structure(
        df_1m['high'].to_numpy(), df_1m['low'].to_numpy(),
        df_1m['close'].to_numpy(),
        pivot_len=9, liquidity_len=30,
    )
    trend_per_1s = np.zeros(n_bars, dtype=np.int8)
    breaks_in_1s = []
    for be in structure.breaks:
        t = df_1m.index[be.bar]
        ts = (pd.Timestamp(t, tz='UTC')
              if t.tzinfo is None
              else pd.Timestamp(t).tz_convert('UTC'))
        idx_1s = df_day['time'].searchsorted(ts, side='right') - 1
        if idx_1s < 0:
            idx_1s = 0
        if idx_1s >= n_bars:
            idx_1s = n_bars - 1
        is_bull = be.kind.name in ('BOS_BULL', 'CHOCH_BULL')
        breaks_in_1s.append((idx_1s, 1 if is_bull else -1))
    breaks_in_1s.sort()
    cursor = 0
    last_trend = 0
    for brk_bar, d in breaks_in_1s:
        trend_per_1s[cursor:brk_bar + 1] = last_trend
        last_trend = d
        cursor = brk_bar + 1
    trend_per_1s[cursor:] = last_trend

    zones = detect_fvg(
        opens, highs, lows, closes,
        resample_to_n_secs=60,
        fvg_min_zone_usd=0.30,
        fvg_displacement_ratio=0.0,
        fvg_body_definition='body',
        require_retest_to_invert=True,
        invalidation_min_pierce_usd=0.05,
        invalidation_min_consecutive_bars=2,
    )
    body_arr = np.abs(closes - opens)
    tier_by_id = _classify_all(zones, body_arr, trend_per_1s)
    # Tier lookup by (trigger_bar, direction, zl, zh) -- the trade's
    # ``fvg_zone`` is a SEPARATE FvgZone instance (built inside the
    # bar-loop's own detect_fvg call) so ``id()`` doesn't match.
    # Use content-based keying instead.
    tier_by_key = {}
    for z in zones:
        tid = tier_by_id.get(id(z))
        if tid is None:
            continue
        key = (int(z.trigger_bar), int(z.direction),
               round(float(z.zone_low), 4), round(float(z.zone_high), 4))
        tier_by_key[key] = tid
    return df_day, zones, tier_by_key


for di, day_id in enumerate(sample_days):
    viz_start = pd.Timestamp(int(day_id) * NS_PER_DAY, unit='ns', tz='UTC')
    viz_end = viz_start + pd.Timedelta(days=1)
    out = _compute_zones_for_day(viz_start, viz_end)
    if out is None:
        skipped_days += 1
        continue
    df_day, zones, tier_by_key = out

    # Run the full ICT backtest
    p = _make_params()
    try:
        res = run_ict_backtest(df_day, p, strategy_label='nb40')
    except Exception as e:
        print(f'  [{di+1}/{n_sample}] day={viz_start.date()} BACKTEST ERROR: {e}')
        skipped_days += 1
        continue

    # Tag each closed trade with the candlestick tier of its source FVG
    n_tier_tagged = 0
    for t in res.trades:
        # Trade's source FVG zone is in t.fvg_zone. Look up the
        # tier via content-based key (the trade's fvg_zone is a
        # SEPARATE FvgZone instance built inside the bar-loop's
        # own detect_fvg call -- id() won't match).
        if t.fvg_zone is not None:
            z = t.fvg_zone
            key = (int(z.trigger_bar), int(z.direction),
                   round(float(z.zone_low), 4),
                   round(float(z.zone_high), 4))
            tier = tier_by_key.get(key, 'X')
        elif t.entry_triggered_by == 'ifvg':
            tier = 'I'   # iFVG (separate from A/B/C/D)
        else:
            tier = 'X'
        # entry_triggered_by tells us fvg vs ifvg vs orb etc.
        all_trades.append({
            'day_date': str(viz_start.date()),
            'tier': tier,
            'src': t.entry_triggered_by,
            'direction': int(t.direction),
            'entry_price': float(t.entry_price),
            'exit_price': float(t.exit_price),
            'pnl_usd': float(t.pnl_usd),
            'exit_reason': t.exit_reason,
            'lots': float(t.lots),
            'hold_secs': float(t.hold_secs),
            'layer_idx': int(t.layer_idx),
        })
        n_tier_tagged += 1
    day_summary.append({
        'day_date': str(viz_start.date()),
        'n_zones': len(zones),
        'n_trades': len(res.trades),
        'n_tagged': n_tier_tagged,
        'tier_a': sum(1 for t in tier_by_key.values() if t == 'A'),
        'tier_b': sum(1 for t in tier_by_key.values() if t == 'B'),
        'tier_c': sum(1 for t in tier_by_key.values() if t == 'C'),
        'tier_d': sum(1 for t in tier_by_key.values() if t == 'D'),
    })

    if (di + 1) % 10 == 0 or (di + 1) == n_sample:
        elapsed = time.time() - t_start
        last = day_summary[-1]
        print(f'  [{di+1:>3d}/{n_sample}] day={last["day_date"]} '
              f'zones={last["n_zones"]} trades={last["n_trades"]} '
              f'tiers={last["tier_a"]}/{last["tier_b"]}/'
              f'{last["tier_c"]}/{last["tier_d"]} '
              f'elapsed={elapsed:.1f}s', flush=True)

# %% [markdown]
# ### Analysis: EV/trade by tier
#
# %%
print(f'\nSkipped days: {skipped_days}')
print(f'Total trades: {len(all_trades):,}')

trades_df = pd.DataFrame(all_trades)
days_df = pd.DataFrame(day_summary)

# ── Analysis ──────────────────────────────────────────────────────────────
# Note: tier 'I' = iFVG (separate from FVG ABCD). tier 'X' = orb
# / wyckoff / sweep (no source FVG). tier '' also appears for
# non-FVG sources.
print('\n=== Trade counts by tier ===')
print(trades_df['tier'].value_counts())

print('\n=== EV/trade by tier (PnL from baseline ICT backtest) ===')
print(f'{"tier":>4s} | {"n":>6s} | {"TP%":>5s} | {"SL%":>5s} | '
      f'{"inv%":>5s} | {"avg_pnl":>9s} | {"sum_pnl":>10s} | '
      f'{"avg_hold_s":>10s}')
print('-' * 80)
for tier in ['A', 'B', 'C', 'D', 'I', 'X']:
    sub = trades_df[trades_df['tier'] == tier]
    if len(sub) == 0:
        continue
    n = len(sub)
    tp = (sub['exit_reason'] == 'tp').mean() * 100
    sl = (sub['exit_reason'] == 'sl').mean() * 100
    inv = (sub['exit_reason'] == 'inv').mean() * 100
    avg_pnl = sub['pnl_usd'].mean()
    sum_pnl = sub['pnl_usd'].sum()
    avg_hold = sub['hold_secs'].mean()
    print(f'{tier:>4s} | {n:>6d} | {tp:>4.1f}% | {sl:>4.1f}% | '
          f'{inv:>4.1f}% | ${avg_pnl:>+7.3f} | ${sum_pnl:>+9.1f} | '
          f'{avg_hold:>9.1f}s')

# Daily breakdown -- which tiers contribute to daily PnL?
print('\n=== Daily PnL per tier ===')
print('  stat |    A    |    B    |    C    |    D    |    I    |    X')
g = trades_df.groupby(['day_date', 'tier'])['pnl_usd'].sum().unstack(fill_value=0)
for stat, fn in [('mean', np.mean), ('std', np.std),
                  ('p25', lambda x: np.percentile(x, 25)),
                  ('p50', lambda x: np.percentile(x, 50)),
                  ('p75', lambda x: np.percentile(x, 75))]:
    row = []
    for tier in ['A', 'B', 'C', 'D', 'I', 'X']:
        col = g[tier] if tier in g.columns else pd.Series([0.0])
        row.append(fn(col) if len(col) else np.nan)
    print(f'  {stat:4s} |' + ' | '.join(
        f'{v:>+7.2f}' if not np.isnan(v) else '   nan  ' for v in row
    ))

# Headline: aggregate EV/trade per tier, all sampled days
print('\n=== Final EV/trade ===')

# %% [markdown]
# ### Counterfactual: drop tiers
#
# %%
total_pnl = trades_df.groupby('tier')['pnl_usd'].sum()
total_n = trades_df.groupby('tier').size()
for tier in ['A', 'B', 'C', 'D', 'I', 'X']:
    if tier not in total_pnl.index:
        continue
    n = total_n[tier]
    pnl = total_pnl[tier]
    ev = pnl / n if n > 0 else 0.0
    print(f'  {tier}: n={n:>5d}  total_pnl=${pnl:>+8.2f}  '
          f'EV/trade=${ev:>+7.4f}')

# ── Counterfactual: drop-C tier filter (the user's hypothesis) ───────────
print('\n=== Counterfactual: drop C-tier trades ===')
print('  Baseline: all tiers included')
baseline_pnl = trades_df['pnl_usd'].sum()
baseline_n = len(trades_df)
print(f'    n={baseline_n} total_pnl=${baseline_pnl:>+7.2f} '
      f'EV/trade=${baseline_pnl/baseline_n:>+7.4f}')

for drop_tier in [['C'], ['C', 'D'], ['B', 'C', 'D'], ['D']]:
    sub = trades_df[~trades_df['tier'].isin(drop_tier)]
    n = len(sub)
    pnl = sub['pnl_usd'].sum()
    ev = pnl / n if n > 0 else 0.0
    delta_pnl = pnl - baseline_pnl
    print(f'  Drop {drop_tier}: n={n:>5d} ({100*n/baseline_n:>5.1f}% of baseline)  '
          f'total_pnl=${pnl:>+7.2f} ({delta_pnl:>+7.2f} delta)  '
          f'EV/trade=${ev:>+7.4f}')

# Save raw trades for downstream analysis
out_csv = ROOT / 'notebooks' / 'nb40_trades_by_tier.csv'
trades_df.to_csv(out_csv, index=False)
out_days = ROOT / 'notebooks' / 'nb40_day_summary.csv'
days_df.to_csv(out_days, index=False)
print(f'\nCSV saved: {out_csv} / {out_days}')
print(f'Total runtime: {time.time() - t_start:.1f}s')
