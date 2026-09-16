# ---
# jupyter:
#   jupytext:
#     formats: py:percent
#     text_re_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
# ---
# %%
# nb38 -- FVG candle-quality classifier on N random UTC days
#
# **Note (2026-09-15)**: the original notebook used the labels
# A/B/C/D ("tier"). The renamed system in v2 calls them
# **HUNT / STRONG_ALIGNED / TRUE_DISPLACEMENT / MARGINAL**. This
# notebook still uses the legacy A/B/C/D labels internally because
# it's a standalone study script. The mapping is:
#
# | Old | New |
# |---|---|
# | D | HUNT |
# | A | STRONG_ALIGNED |
# | B | TRUE_DISPLACEMENT |
# | C | MARGINAL |
#
# Asks: do A / B / C / D quality classifications actually predict
# zone inversion rate, or is the notebook-35 sample (n=186 zones
# on 1 day) statistical noise?
#
# Knobs at the top of cell 4:
#   N_DAYS         — how many UTC days to sample. 200 is the
#                    default (covers 2025 + parts of 2024 / 2026
#                    on the 1y parquet). Increase for tighter
#                    CIs; decrease for faster iteration.
#   RNG_SEED       — reproducible day selection.
#
# Output:
#   * Per-day tier counts (A/B/C/D/X)
#   * Pooled inversion / mitigation / untouched outcome counts
#     per tier (across all sampled days)
#   * Per-day inversion-rate distribution per tier (mean, std,
#     25/50/75 percentiles) -- shows whether the inversion-rate
#     is structurally stable or just the average of a few
#     outliers.
#   * Forward-looking probability of inversion by tier given
#     only the FIRST 60 seconds of post-trigger price action
#     (a proxy for "would the strategy have known in time to
#     skip the trade?").

# %%
## Imports + locate repo

# %%
import os, sys
os.environ.setdefault('MPLBACKEND', 'Agg')

# Calendar-day constant. Used in the next cell for day-id
# arithmetic, defined once here so both imports land before usage.
NS_PER_DAY = 86_400_000_000_000

import numpy as np
import pandas as pd
import pyarrow as pa
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from pathlib import Path


def _find_root() -> Path:
    here = Path('.').resolve()
    candidates = [p for p in [here, *here.parents]
                  if (p / 'src' / 'core' / 'ict_signals.py').is_file()]
    if not candidates:
        raise RuntimeError('Could not find ICT repo root (no src/core/ict_signals.py)')
    def _has_fork_sig(p: Path) -> bool:
        f = p / 'src' / 'core' / 'ict_strategy.py'
        try:
            return 'played_out_min_extension_usd' in f.read_text(encoding='utf-8')
        except OSError:
            return False
    with_sig = [p for p in candidates if _has_fork_sig(p)]
    return with_sig[0] if with_sig else candidates[0]


ROOT = _find_root()
sys.path.insert(0, str(ROOT))

from src.core.ict_signals import detect_fvg
from src.core.market_structure import detect_market_structure

print('Imports OK')
print('Project root:', ROOT)

# %%
## Knobs (edit and re-run)

# %%
N_DAYS = 200              # how many UTC days to sample. 200 is the
                          # default (covers 2025 + parts of 2024 / 2026
                          # on the 1y parquet). Increase for tighter
                          # CIs; decrease for faster iteration.
RNG_SEED = 20260915       # reproducible day selection
DRY_RUN = False           # True → process only the first 5 sampled
                          # days (sanity check before full run)
MIN_BARS_PER_DAY = 30_000 # skip days with very few 1s bars (data gaps,
                          # not real holidays -- which have ~25-45k bars)

# Classifier knobs -- match nb35 exactly for an apples-to-apples
# comparison against the original per-day result.
TIER_A_DISPLACEMENT_RATIO = 2.0
TIER_AB_DISPLACEMENT_RATIO = 1.5
TIER_A_MIN_DEPTH = 0.50
TIER_AB_MIN_DEPTH = 0.25
D_PIERCED_AND_INVERTED_MAX_AGE_BARS = 600   # 10 min on 1s data

# Outcome horizons (seconds of follow-through after the trigger bar).
HORIZONS = {
    '5min':  300,
    '15min': 900,
    '60min': 3600,
}

# Look-ahead-free early signal: with only the first
# EARLY_WINDOW_SECS of price action after the trigger, can the
# detector predict inversion? This is the "would the strategy have
# known in time to skip?" check.
EARLY_WINDOW_SECS = 60

# %%
## Enumerate all UTC days in the parquet (random sample later)

# %%
import pyarrow.dataset as ds

data_dir = ROOT / 'data'
if not (data_dir / 'XAUUSD_S1_1y.parquet').exists():
    for ancestor in [ROOT.parent, *ROOT.parent.parents]:
        sibling = ancestor / 'data'
        if (sibling / 'XAUUSD_S1_1y.parquet').exists():
            data_dir = sibling
            break

parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'

# Cheap day-enumeration pass via Parquet (no row scan beyond the
# column-narrow read). pyarrow's TimestampArray to_numpy returns
# datetime64[ms] (because the parquet schema is timestamp[ms]).
# We convert to UTC-aware then to int64 ns for ns arithmetic.

from pyarrow import parquet as pq
import time as _time_mod
_t0 = _time_mod.time()
t_table = pq.read_table(str(parquet_path), columns=['time'])
# Cast ms-tzUTC -> int64 (ms), then multiply to get ns. We need a
# Schema for cast(), not a DataType.
t_table_ms = t_table.cast(pa.schema([pa.field('time', pa.int64())]))
ms_arr = t_table_ms.column('time').to_numpy(zero_copy_only=False)
t_np = (ms_arr.astype(np.int64) * 1_000_000)
print(f'time column read in {_time_mod.time() - _t0:.1f}s, rows={len(t_np):,}')
print(f'Range: {pd.Timestamp(int(t_np[0]), unit="ns", tz="UTC")} -> '
      f'{pd.Timestamp(int(t_np[-1]), unit="ns", tz="UTC")}')
unique_days_all = np.unique(t_np // NS_PER_DAY)
print(f'Distinct UTC day-buckets in parquet: {len(unique_days_all):,}')

# Filter to Mon–Fri only (XAUUSD doesn't trade Sat; Sun is
# only the evening-open, very thin). Holidays have many fewer
# bars than a normal session but are still Mon–Fri days, so
# they remain in the pool — the MIN_BARS_PER_DAY guard below
# silently skips them. WHY: a day that's "missing" because
# the market was closed should NOT count as a structural
# sample — it would inflate "untouched" rates for zones that
# had no chance to fill.
day_dows = np.array([
    pd.Timestamp(int(d) * NS_PER_DAY, unit='ns', tz='UTC').dayofweek
    for d in unique_days_all
])
unique_days = unique_days_all[day_dows < 5]   # drop Sat (5) and Sun (6)
print(f'Mon-Fri day-buckets: {len(unique_days):,}')

# %%
## Sample N_DAYS random UTC days (reproducible via RNG_SEED)

# %%
rng = np.random.default_rng(RNG_SEED)
# Day-stratified sampling: every day equally likely. We over-sample
# from the candidate list (replacement allowed? no -- without
# replacement is safer). If we want N > len(unique_days), we cap.
n_sample = min(N_DAYS, len(unique_days))
sample_days = rng.choice(unique_days, size=n_sample, replace=False)
sample_days.sort()
print(f'Sampled {n_sample} UTC days')
if DRY_RUN:
    sample_days = sample_days[:5]
    print(f'  DRY_RUN → using only first 5 days')
print(f'  first: {pd.Timestamp(int(sample_days[0]) * NS_PER_DAY, unit="ns", tz="UTC")}')
print(f'  last:  {pd.Timestamp(int(sample_days[-1]) * NS_PER_DAY, unit="ns", tz="UTC")}')

# %%
## Per-day detector + classifier + outcome harvest

# %%
def _classify_zone(
    z, body_size, c1_body, c3_body, structure_direction_at_trigger,
):
    '''Mirror of nb35's classify_zone. Pure function, no side effects.'''
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
    struct_ok = structure_direction_at_trigger == z.direction
    if disp_ok_a and depth_ok_a and struct_ok:
        return 'A'
    if disp_ok_b and depth_ok_b:
        return 'B'
    return 'C'


def _zone_outcome_horizon(z, n_bars, body_c2, body_c1, body_c3,
                          early_high, early_low, horizons):
    '''Compute outcomes at multiple horizons + early-signal proxy.

    Returns dict with:
      inverted:           bool   -- z.inverted (lifetime pierce + retest)
      mitigated:          bool   -- z was at least partially mitigated
      untouched:          bool   -- no fill yet by lifetime end
      pierced_within_<H>:  bool   -- forward-looking: first pierce
                                    within H seconds of trigger
      inverted_within_<H>:bool   -- forward-looking: pierce + retest
                                    within H seconds
      mitigated_within_<H>: bool -- forward-looking: partial fill
                                    within H seconds
      retest_gap_bars:    int    -- pierce_bar -> inverted_bar (or -1)
      mitigated_depth:    float  -- z.mitigated_depth_pct (lifetime)
    '''
    out = {
        'inverted': bool(z.inverted),
        'mitigated': bool(z.mitigated_bar >= 0),
        'untouched': bool(
            z.mitigated_bar < 0 and not z.inverted
        ),
        'retest_gap_bars': (
            int(z.inverted_bar - z.pierced_bar)
            if (z.pierced_bar >= 0 and z.inverted_bar >= 0) else -1
        ),
        'mitigated_depth': float(z.mitigated_depth_pct),
    }
    # Forward-looking "would the strategy have known within H seconds?"
    # Using z.pierced_bar / inverted_bar / mitigated_bar directly,
    # they're already NO-LOOK-AHEAD (recorded when the event
    # happened in real time), so any report at "pierced within H"
    # is a valid early-warning simulation.
    for h_name, h_secs in horizons.items():
        h_bars = h_secs  # 1s data: 1 bar per second
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


# %%
# Per-day loop. We process one UTC day at a time so we don't blow
# memory on the full 1y parquet in RAM. For each day we:
#   1. Read 1s OHLC for the day window
#   2. Compute structure on a 1m resample (matches nb35)
#   3. Run detect_fvg at 1m cadence with the same knobs as nb35
#   4. Classify each zone into A/B/C/D
#   5. Compute outcome metrics + per-horizon early-signal proxy
#   6. Append into a single per-zone dataframe
#
# Output rows are kept short (no zone objects in the dataframe).

rows_per_day = []
day_summary = []
skipped_days = 0

t_start = pd.Timestamp.now(tz='UTC')

# We re-use the dataset + filter pattern from nb35 for cheap day reads.
import pyarrow.dataset as pds
dataset = pds.dataset(str(parquet_path), format='parquet')

for di, day_id in enumerate(sample_days):
    day_start_ns = int(day_id) * NS_PER_DAY
    viz_start = pd.Timestamp(day_start_ns, unit='ns', tz='UTC')
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

    # 1m resample for structure (matches nb35)
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
    # Map 1m structure breaks to 1s trend timeline.
    trend_per_1s = np.zeros(n_bars, dtype=np.int8)
    breaks_in_1s = []
    for be in structure.breaks:
        t = df_1m.index[be.bar]
        idx_1s = df_day['time'].searchsorted(
            pd.Timestamp(t, tz='UTC') if t.tzinfo is None
            else pd.Timestamp(t).tz_convert('UTC'),
            side='right',
        ) - 1
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

    # FVG detector (matches nb35 exactly)
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
    n_zones = len(zones)
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

        # Early signal: the high/low of the first EARLY_WINDOW_SECS
        # bars after the trigger. Used in the early-warning calc.
        eh_end = min(z.trigger_bar + EARLY_WINDOW_SECS, n_bars)
        early_high = float(highs[z.trigger_bar + 1:eh_end].max()) \
            if eh_end > z.trigger_bar + 1 else float(closes[z.trigger_bar])
        early_low = float(lows[z.trigger_bar + 1:eh_end].min()) \
            if eh_end > z.trigger_bar + 1 else float(closes[z.trigger_bar])

        outcome = _zone_outcome_horizon(
            z, n_bars, c2_body, c1_body, c3_body,
            early_high, early_low, HORIZONS,
        )

        row = {
            'day_id': int(day_id),
            'day_date': str(pd.Timestamp(int(day_id) * NS_PER_DAY,
                                          unit='ns', tz='UTC').date()),
            'tier': tier,
            'direction': int(z.direction),
            'zone_usd': float(z.zone_high - z.zone_low),
            'mitigated_depth': float(z.mitigated_depth_pct),
            'trigger_bar': int(z.trigger_bar),
        }
        row.update(outcome)
        rows_per_day.append(row)

    day_summary.append({
        'day_id': int(day_id),
        'day_date': str(pd.Timestamp(int(day_id) * NS_PER_DAY,
                                       unit='ns', tz='UTC').date()),
        'n_bars': int(n_bars),
        'n_zones': int(n_zones),
        **tier_counts_d,
    })

    if (di + 1) % 20 == 0 or (di + 1) == n_sample:
        elapsed = (pd.Timestamp.now(tz='UTC') - t_start).total_seconds()
        print(
            f'  [{di+1:>3d}/{n_sample}] day={day_summary[-1]["day_date"]} '
            f'zones={n_zones} tiers={tier_counts_d} '
            f'elapsed={elapsed:.1f}s'
        )

print(f'\nSkipped days (data gap): {skipped_days}')
print(f'Sampled zones: {len(rows_per_day):,}')

# %%
## Build aggregated dataframes

# %%
zones_df = pd.DataFrame(rows_per_day)
days_df = pd.DataFrame(day_summary)
print(f'zones_df: {zones_df.shape}')
print(zones_df.head())

# %%
## Tier counts across all sampled days

# %%
tier_counts = zones_df['tier'].value_counts().reindex(['A', 'B', 'C', 'D', 'X'], fill_value=0)
print('Pooled tier counts across all sampled days:')
print(tier_counts)
print(f'\nTotal zones: {len(zones_df):,}')
print(f'Distinct days with ≥ 1 zone: {zones_df["day_date"].nunique()}')

# %%
## Inversion / mitigation / untouched outcomes by tier (pooled)

# %%
outcomes_by_tier = (
    zones_df.groupby('tier')[['inverted', 'mitigated', 'untouched']]
    .mean() * 100
)
outcomes_by_tier['n_zones'] = (
    zones_df.groupby('tier').size()
)
outcomes_by_tier = outcomes_by_tier.reindex(['A', 'B', 'C', 'D', 'X']).fillna(0)
print('Outcome rates by tier (% of tier, pooled across sampled days):')
print(outcomes_by_tier.round(1))

# %%
## Per-day inversion rate distribution by tier

# %%
# Group by day, compute per-day inversion rate per tier, then take
# mean / std / percentiles across days. Lets us see whether the
# tier-level inversion rate is day-stable or driven by a few
# outlier days.

per_day_inv = (
    zones_df.groupby(['day_date', 'tier'])['inverted']
    .mean()
    .unstack(fill_value=np.nan)
    .reindex(columns=['A', 'B', 'C', 'D'])
)
print('Per-day inversion rate by tier (% of tier’s zones):')
print('  stat   |    A    |    B    |    C    |    D')
for stat_name, fn in [
    ('mean', np.nanmean),
    ('std',  np.nanstd),
    ('p25',  lambda x: np.nanpercentile(x, 25)),
    ('p50',  lambda x: np.nanpercentile(x, 50)),
    ('p75',  lambda x: np.nanpercentile(x, 75)),
]:
    row = []
    for tier in ['A', 'B', 'C', 'D']:
        col = per_day_inv[tier].dropna()
        row.append(fn(col) if len(col) else np.nan)
    print(f'  {stat_name:5s} | ' + ' | '.join(
        [f'{v*100:>6.1f}%' if not np.isnan(v) else '   nan ' for v in row]
    ))
print()
print(f'Days with ≥ 1 zone in tier A: '
      f'{(per_day_inv["A"].notna()).sum()} / {len(per_day_inv)}')
print(f'Days with ≥ 1 zone in tier B: '
      f'{(per_day_inv["B"].notna()).sum()} / {len(per_day_inv)}')

# %%
## Forward-looking: would the strategy have known within H seconds?

# %%
# For each tier, what fraction of inverted zones (or inverted-within-
# H zones) had a pierce within H seconds of the trigger? This is the
# "early-warning" view: skip the trade if we see the early sign.

print('Early-warning rates (% of tier zones, computed NO-LOOK-AHEAD from pierce_bar/inverted_bar/mitigated_bar):')
print()
header = ['Tier', 'n']
h_keys = list(HORIZONS.keys())
header += sum([[f'pierc<{h}', f'inv<{h}'] for h in h_keys], [])
print('  ' + ' | '.join(f'{c:<10s}' for c in header))
for tier in ['A', 'B', 'C', 'D']:
    sub = zones_df[zones_df['tier'] == tier]
    n = len(sub)
    if n == 0:
        continue
    row = [tier, str(n)]
    for h_name in h_keys:
        pc = sub[f'pierced_within_{h_name}'].mean() * 100
        inv = sub[f'inverted_within_{h_name}'].mean() * 100
        row.append(f'{pc:.0f}%')
        row.append(f'{inv:.0f}%')
    print('  ' + ' | '.join(f'{c:<10s}' for c in row))

# %%
## Charts: (1) tier-mix across days, (2) per-day inversion-rate boxplot

# %%
fig, axes = plt.subplots(1, 2, figsize=(16, 6))

# (1) Tier counts (pooled)
ax = axes[0]
tier_colors = {
    'A': '#ffe066', 'B': '#a8e6cf', 'C': '#d3d3d3', 'D': '#ff8a80',
}
counts_for_bar = tier_counts.reindex(['A', 'B', 'C', 'D', 'X']).fillna(0)
bars = ax.bar(
    ['A', 'B', 'C', 'D', 'X'],
    counts_for_bar.values,
    color=[tier_colors[t] for t in ['A', 'B', 'C', 'D']] + ['#ffffff'],
    edgecolor='black', linewidth=0.8,
)
for bar, n in zip(bars, counts_for_bar.values):
    ax.text(
        bar.get_x() + bar.get_width() / 2, bar.get_height() * 1.01,
        f'{int(n):,}', ha='center', va='bottom', fontsize=10,
    )
ax.set_ylabel('Total zones across sampled days')
ax.set_title('(1) Pooled tier counts')
ax.grid(True, alpha=0.3, axis='y')

# (2) Per-day inversion-rate distribution per tier (boxplot)
ax = axes[1]
data_for_box = [
    per_day_inv[t].dropna().values * 100 for t in ['A', 'B', 'C', 'D']
]
bp = ax.boxplot(
    data_for_box,
    tick_labels=['A', 'B', 'C', 'D'],
    patch_artist=True,
    showmeans=True,
    meanline=True,
)
for patch, tier in zip(bp['boxes'], ['A', 'B', 'C', 'D']):
    patch.set_facecolor(tier_colors[tier])
    patch.set_alpha(0.7)
ax.set_ylabel('Per-day inversion rate (%)')
ax.set_title('(2) Per-day inversion rate by tier')
ax.grid(True, alpha=0.3, axis='y')
ax.set_ylim(-5, 105)

fig.suptitle(
    f'nb38 -- FVG tier classifier on {len(per_day_inv)} sampled UTC days '
    f'({len(zones_df):,} zones)',
    fontsize=12,
)
fig.tight_layout()
out = ROOT / 'notebooks' / 'nb38_tier_study.png'
plt.savefig(out, dpi=140)
plt.close()
print(f'Chart saved: {out}')

# %%
## Notes & next steps

# %%
print("""
Findings to look for in the output above:

1. **Tier-mix stability across days** (output from cells
   \"Tier counts\" and \"Per-day inversion rate by tier\"):
   is the A:B:C:D ratio roughly stable per day, or does it
   swing wildly between days (in which case the tier
   classifier is more about market regime than zone quality)?

2. **Inversion rate by tier** (output from \"Outcome rates by
   tier\"): does A invert less than B/C/D AND does D invert
   more? If the inversion rate is similar across tiers, the
   classifier isn't doing useful work.

3. **Early-warning by horizon** (output from \"Early-warning
   rates\"): what fraction of the inverted-within-15min zones
   had a pierce within 5min? If a meaningful fraction of the
   *good zones* (that didn't invert within 15min) would have
   shown an early sign, that's a filter the existing detector
   can exploit.

4. **Per-day variance** (boxplot): if the inversion rate
   distribution per tier overlaps (e.g. A is sometimes higher
   than C on a particular day), the tier classifier is
   unreliable as a static filter -- it would need to be
   self-calibrated per-day.
""")
