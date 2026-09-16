#!/usr/bin/env python
"""Standalone runner for nb38 — avoids the jupyter nbconvert harness
that's getting killed at ~35s. Mirrors the .py notebook logic.
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault('MPLBACKEND', 'Agg')

import numpy as np
import pandas as pd
import pyarrow as pa
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import pyarrow.dataset as pds
import pyarrow.parquet as pq

NS_PER_DAY = 86_400_000_000_000

# ── Knobs ──────────────────────────────────────────────────────────────────
N_DAYS = 200
RNG_SEED = 20260915
DRY_RUN = False
MIN_BARS_PER_DAY = 30_000

TIER_A_DISPLACEMENT_RATIO = 2.0
TIER_AB_DISPLACEMENT_RATIO = 1.5
TIER_A_MIN_DEPTH = 0.50
TIER_AB_MIN_DEPTH = 0.25
D_PIERCED_AND_INVERTED_MAX_AGE_BARS = 600

HORIZONS = {'5min': 300, '15min': 900, '60min': 3600}
EARLY_WINDOW_SECS = 60

# ── Repo root ──────────────────────────────────────────────────────────────
def _find_root():
    here = Path('.').resolve()
    for p in [here, *here.parents]:
        if (p / 'src' / 'core' / 'ict_signals.py').is_file():
            return p
    raise RuntimeError('no repo root')

ROOT = _find_root()
sys.path.insert(0, str(ROOT))
from src.core.ict_signals import detect_fvg
from src.core.market_structure import detect_market_structure

# ── Data path ──────────────────────────────────────────────────────────────
data_dir = ROOT / 'data'
if not (data_dir / 'XAUUSD_S1_1y.parquet').exists():
    for ancestor in [ROOT.parent, *ROOT.parent.parents]:
        sibling = ancestor / 'data'
        if (sibling / 'XAUUSD_S1_1y.parquet').exists():
            data_dir = sibling
            break
parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'

# ── Day enumeration (weekday-only) ────────────────────────────────────────
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
print(f'Day enumeration: {time.time() - t0:.1f}s')
print(f'  Mon-Fri days: {len(unique_days):,}')

rng = np.random.default_rng(RNG_SEED)
n_sample = min(N_DAYS, len(unique_days))
sample_days = rng.choice(unique_days, size=n_sample, replace=False)
sample_days.sort()
if DRY_RUN:
    sample_days = sample_days[:5]
print(f'Sampled {len(sample_days)} days')

# ── Classifier + outcome helpers ──────────────────────────────────────────
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


def _zone_outcome(z, horizons):
    out = {
        'inverted': bool(z.inverted),
        'mitigated': bool(z.mitigated_bar >= 0),
        'untouched': bool(z.mitigated_bar < 0 and not z.inverted),
        'retest_gap_bars': (
            int(z.inverted_bar - z.pierced_bar)
            if (z.pierced_bar >= 0 and z.inverted_bar >= 0) else -1
        ),
        'mitigated_depth': float(z.mitigated_depth_pct),
    }
    for h_name, h_secs in horizons.items():
        h_bars = h_secs
        out[f'pierced_within_{h_name}'] = bool(
            z.pierced_bar >= 0
            and (z.pierced_bar - z.trigger_bar) <= h_bars
        )
        out[f'inverted_within_{h_name}'] = bool(
            z.inverted_bar >= 0
            and (z.inverted_bar - z.trigger_bar) <= h_bars
        )
        out[f'mitigated_within_{h_name}'] = bool(
            z.mitigated_bar >= 0
            and (z.mitigated_bar - z.trigger_bar) <= h_bars
        )
    return out


# ── Per-day loop ───────────────────────────────────────────────────────────
rows_per_day = []
day_summary = []
skipped_days = 0
t_start = time.time()
dataset = pds.dataset(str(parquet_path), format='parquet')

for di, day_id in enumerate(sample_days):
    viz_start = pd.Timestamp(int(day_id) * NS_PER_DAY, unit='ns', tz='UTC')
    viz_end = viz_start + pd.Timedelta(days=1)
    table = dataset.to_table(
        columns=['time', 'open', 'high', 'low', 'close'],
        filter=(pds.field('time') >= viz_start) & (pds.field('time') < viz_end),
    )
    n_bars = len(table)
    if n_bars < MIN_BARS_PER_DAY:
        skipped_days += 1
        continue
    df_day = table.to_pandas().sort_values('time').reset_index(drop=True)

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
    tier_counts_d = {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'X': 0}

    for z in zones:
        if z.trigger_bar < 2 or z.trigger_bar >= n_bars - 1:
            continue
        c1_body = float(body_arr[z.trigger_bar - 2])
        c3_body = float(body_arr[z.trigger_bar])
        c2_body = float(body_arr[z.trigger_bar - 1])
        struct_dir_at = int(trend_per_1s[z.trigger_bar - 1])
        tier = _classify_zone(z, c2_body, c1_body, c3_body, struct_dir_at)
        tier_counts_d[tier] += 1
        outcome = _zone_outcome(z, HORIZONS)
        rows_per_day.append({
            'day_id': int(day_id),
            'day_date': str(pd.Timestamp(int(day_id) * NS_PER_DAY,
                                          unit='ns', tz='UTC').date()),
            'tier': tier,
            'direction': int(z.direction),
            'zone_usd': float(z.zone_high - z.zone_low),
            'mitigated_depth': float(z.mitigated_depth_pct),
            'trigger_bar': int(z.trigger_bar),
            **outcome,
        })

    day_summary.append({
        'day_id': int(day_id),
        'day_date': str(pd.Timestamp(int(day_id) * NS_PER_DAY,
                                       unit='ns', tz='UTC').date()),
        'n_bars': int(n_bars),
        'n_zones': int(len(zones)),
        **tier_counts_d,
    })

    if (di + 1) % 25 == 0 or (di + 1) == len(sample_days):
        elapsed = time.time() - t_start
        last_day = day_summary[-1]['day_date']
        last_zones = day_summary[-1]['n_zones']
        last_tiers = {k: day_summary[-1][k] for k in ['A', 'B', 'C', 'D']}
        print(f'  [{di+1:>3d}/{len(sample_days)}] day={last_day} '
              f'zones={last_zones} tiers={last_tiers} '
              f'elapsed={elapsed:.1f}s', flush=True)

print(f'\nSkipped days: {skipped_days}')
print(f'Sampled zones: {len(rows_per_day):,}')

zones_df = pd.DataFrame(rows_per_day)
days_df = pd.DataFrame(day_summary)

# ── Aggregations ──────────────────────────────────────────────────────────
print('\n=== Pooled tier counts ===')
print(zones_df['tier'].value_counts().reindex(['A', 'B', 'C', 'D', 'X'], fill_value=0))

print('\n=== Outcome rates by tier (% of tier, pooled) ===')
outcomes = (zones_df.groupby('tier')[['inverted', 'mitigated', 'untouched']]
            .mean() * 100)
outcomes['n_zones'] = zones_df.groupby('tier').size()
outcomes = outcomes.reindex(['A', 'B', 'C', 'D', 'X']).fillna(0)
print(outcomes.round(1))

print('\n=== Per-day inversion rate by tier ===')
per_day_inv = (zones_df.groupby(['day_date', 'tier'])['inverted']
               .mean().unstack(fill_value=np.nan)
               .reindex(columns=['A', 'B', 'C', 'D']))
print(f'Days covered (A): {(per_day_inv["A"].notna()).sum()}')
print(f'Days covered (B): {(per_day_inv["B"].notna()).sum()}')
print(f'Days covered (C): {(per_day_inv["C"].notna()).sum()}')
print(f'Days covered (D): {(per_day_inv["D"].notna()).sum()}')
print('  stat   |    A    |    B    |    C    |    D')
for stat_name, fn in [('mean', np.nanmean), ('std', np.nanstd),
                      ('p25', lambda x: np.nanpercentile(x, 25)),
                      ('p50', lambda x: np.nanpercentile(x, 50)),
                      ('p75', lambda x: np.nanpercentile(x, 75))]:
    row = []
    for tier in ['A', 'B', 'C', 'D']:
        col = per_day_inv[tier].dropna()
        row.append(fn(col) if len(col) else np.nan)
    print(f'  {stat_name:5s} | ' + ' | '.join(
        [f'{v*100:>6.1f}%' if not np.isnan(v) else '   nan ' for v in row]
    ))

print('\n=== Early-warning rates (% of tier zones) ===')
header = ['Tier', 'n'] + sum(
    [[f'pierc<{h}', f'inv<{h}'] for h in HORIZONS.keys()], []
)
print('  ' + ' | '.join(f'{c:<10s}' for c in header))
for tier in ['A', 'B', 'C', 'D']:
    sub = zones_df[zones_df['tier'] == tier]
    n = len(sub)
    if n == 0:
        continue
    row = [tier, str(n)]
    for h_name in HORIZONS.keys():
        pc = sub[f'pierced_within_{h_name}'].mean() * 100
        inv = sub[f'inverted_within_{h_name}'].mean() * 100
        row.append(f'{pc:.0f}%')
        row.append(f'{inv:.0f}%')
    print('  ' + ' | '.join(f'{c:<10s}' for c in row))

# ── Chart ─────────────────────────────────────────────────────────────────
tier_colors = {
    'A': '#ffe066', 'B': '#a8e6cf', 'C': '#d3d3d3', 'D': '#ff8a80',
}
fig, axes = plt.subplots(1, 2, figsize=(16, 6))
ax = axes[0]
counts_for_bar = (zones_df['tier'].value_counts()
                  .reindex(['A', 'B', 'C', 'D', 'X']).fillna(0))
bars = ax.bar(['A', 'B', 'C', 'D', 'X'], counts_for_bar.values,
              color=[tier_colors[t] for t in ['A', 'B', 'C', 'D']] + ['#ffffff'],
              edgecolor='black', linewidth=0.8)
for bar, n in zip(bars, counts_for_bar.values):
    ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() * 1.01,
            f'{int(n):,}', ha='center', va='bottom', fontsize=10)
ax.set_ylabel('Total zones across sampled days')
ax.set_title('(1) Pooled tier counts')
ax.grid(True, alpha=0.3, axis='y')

ax = axes[1]
data_for_box = [
    per_day_inv[t].dropna().values * 100 for t in ['A', 'B', 'C', 'D']
]
bp = ax.boxplot(
    data_for_box,
    tick_labels=['A', 'B', 'C', 'D'],
    patch_artist=True, showmeans=True, meanline=True,
)
for patch, tier in zip(bp['boxes'], ['A', 'B', 'C', 'D']):
    patch.set_facecolor(tier_colors[tier])
    patch.set_alpha(0.7)
ax.set_ylabel('Per-day inversion rate (%)')
ax.set_title('(2) Per-day inversion rate by tier')
ax.grid(True, alpha=0.3, axis='y')
ax.set_ylim(-5, 105)

fig.suptitle(
    f'nb38 -- FVG tier classifier on {len(sample_days)} sampled UTC days '
    f'({len(zones_df):,} zones)',
    fontsize=12,
)
fig.tight_layout()
out_png = ROOT / 'notebooks' / 'nb38_tier_study.png'
plt.savefig(out_png, dpi=140)
plt.close()
print(f'\nChart saved: {out_png}')

# Save the raw data for downstream analysis
out_csv = ROOT / 'notebooks' / 'nb38_tier_study.csv'
zones_df.to_csv(out_csv, index=False)
print(f'CSV saved: {out_csv}')

print(f'\nTotal runtime: {time.time() - t_start:.1f}s')
