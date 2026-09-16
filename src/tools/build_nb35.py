"""Build nb35_ict_ifvg_accuracy_orb.ipynb -- visual validation for:

1. A 4-tier iFVG / FVG accuracy classification (A / B / C / D) that
   uses ONLY candlestick reads (no delayed indicators) to estimate
   whether a zone is a "true displacement FVG" or just a
   liquidity-sweep probe:

   - A (strong displacement):  middle candle ≥ 2× larger of c1/c3 AND
     mitigation depth ≥ 50% AND aligned with recent structure break
     in the same direction.
   - B (normal FVG):           displacement ratio ≥ 1.5 AND
     mitigation depth ≥ 25%.
   - C (marginal):              falls below one of the A/B gates.
   - D (probable SL-hunt):     zone was pierced AND inverted within
     `D_pierced_and_inverted_max_age_bars` -- the canonical "probe +
     retest" pattern that the existing `fvg_require_retest_to_invert`
     knob already tracks.

2. ORB (Opening Range Breakout) overlay for Asia (00:00 UTC),
   London (07:00 UTC), and NY (13:30 UTC) sessions at 30 min, with
   a wicks-vs-bodies comparison:
   - "Wicks" definition: bar high > or_high OR bar low < or_low.
   - "Bodies" definition: bar close > or_high OR bar close < or_low.
   - Both flavours plotted so the user can see how aggressive vs
     conservative the breakout definition is.

3. "Trade-within-ORB" rule (box theory): until a session's breakout
   is CONFIRMED (body close, not just wick), the tradeable region is
   the ORB box itself. Once confirmed, the tradeable region opens up
   to the whole chart. We draw a shaded background for the ORB box
   and a marker for the breakout bar.
"""
import json
from pathlib import Path

NOTEBOOK_PATH = (Path(__file__).resolve().parent.parent.parent
                 / "notebooks" / "nb35_ict_ifvg_accuracy_orb.ipynb")

cells = []


def md(text):
    cells.append({"cell_type": "markdown", "metadata": {},
                  "source": text.splitlines(keepends=True)})


def code(text):
    cells.append({
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": text.splitlines(keepends=True),
    })


md("""# nb35 -- iFVG accuracy classification + 30-min ORB overlay

The current FVG/iFVG detector (nb34) works on the gap-detection math
but **cannot tell a true displacement FVG from a liquidity-sweep
probe**. A probe that pierces + retests is recorded as `inverted=True`
(an iFVG), but on 1s XAUUSD that pattern is also the textbook
SL-hunt signature -- price closes a few ticks past the zone, comes
back briefly, then continues one-way through it.

This notebook layers **four candlestick-only confirmation methods** on
top of the existing detector to estimate, per zone, how likely it is
to be a "true" FVG vs. an SL-hunt probe. All four use the OHLC bars
themselves; **no delayed indicators** (no ATR-smoothed averages, no
delayed crossover-style signals).

It also adds a **30-min Opening Range Breakout (ORB)** overlay for
the Asia / London / NY sessions, drawn two ways (wicks vs bodies)
so the user can eyeball the breakout quality, plus a "trade within
ORB until confirmed" box-theory gate.

## Knobs

All knobs are at the top of cell 4. Edit and re-run to refresh the
chart.
""")

md("""## Imports + locate repo""")

code("""import os, sys
os.environ.setdefault('MPLBACKEND', 'Agg')
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
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

from src.core.ict_signals import (
    detect_fvg, detect_orb, fvg_retest_signals, compute_simple_atr,
)
from src.core.market_structure import detect_market_structure

print('Imports OK')
print('Project root:', ROOT)
""")

md("""## Load data + compute everything we need""")

code("""import pyarrow.parquet as pq
import pyarrow.dataset as ds

data_dir = ROOT / 'data'
if not (data_dir / 'XAUUSD_S1_1y.parquet').exists():
    for ancestor in [ROOT.parent, *ROOT.parent.parents]:
        sibling = ancestor / 'data'
        if (sibling / 'XAUUSD_S1_1y.parquet').exists():
            data_dir = sibling
            break

VIZ_DATE = '2025-01-08'
viz_start = pd.Timestamp(VIZ_DATE, tz='UTC')
viz_end = viz_start + pd.Timedelta(days=2)
parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'

dataset = ds.dataset(str(parquet_path), format='parquet')
table = dataset.to_table(
    columns=['time', 'open', 'high', 'low', 'close', 'tickv'],
    filter=(ds.field('time') >= viz_start) & (ds.field('time') < viz_end),
)
df = table.to_pandas()
df = df.rename(columns={'tickv': 'volume'})
df['time'] = pd.to_datetime(df['time'], utc=True)
df = df.sort_values('time').reset_index(drop=True)
print(f'Data dir: {data_dir}')
print(f'Viz window: {len(df):,} bars  ({df.time.iloc[0]} -> {df.time.iloc[-1]})')

# Pre-compute everything we need once so the chart cell stays tidy.
opens = df['open'].to_numpy()
highs = df['high'].to_numpy()
lows = df['low'].to_numpy()
closes = df['close'].to_numpy()
# Convert tz-aware UTC timestamps to int64 nanoseconds. pandas
# 2.x's .astype('int64') on a datetime64 column returns MICROSECONDS,
# not nanoseconds (silent unit change), so we go through numpy's
# datetime64[ns] view to be explicit.
times_ns = np.array(df['time'].values, dtype='datetime64[ns]').view('int64')
""")

md("""## Tier classification -- A / B / C / D, candlestick-only""")

code("""# All inputs to the classifier come from the OHLC bars themselves
# (or the detector's existing lifecycle events). No delayed indicators.
TIER_A_DISPLACEMENT_RATIO = 2.0  # middle candle >= 2x larger of c1/c3
TIER_AB_DISPLACEMENT_RATIO = 1.5  # lower bar for B
TIER_A_MIN_DEPTH = 0.50           # mitigation depth >= 50%
TIER_AB_MIN_DEPTH = 0.25          # B tier allows 25%

# D-tier ("probable SL-hunt"): pierce + inversion within this many bars
# of the trigger. The existing detector already tracks pierced_bar and
# inverted_bar; we just check both fire within the window.
D_PIERCED_AND_INVERTED_MAX_AGE_BARS = 600  # 10 min on 1s data


def classify_zone(z, body_size, c1_body, c3_body, structure_direction_at_trigger):
    '''Return one of 'A', 'B', 'C', 'D' (or 'X' for unclassified).

    Rules (candlestick-only, no delayed indicators):
      D  pierced_bar >= 0 AND inverted_bar >= 0 AND the gap between
         them <= D_PIERCED_AND_INVERTED_MAX_AGE_BARS. Classic SL-hunt
         probe + brief retest signature; the existing
         ``fvg_require_retest_to_invert`` knob already gives us the
         data.
      A  displacement_ratio >= TIER_A_DISPLACEMENT_RATIO AND
         mitigated_depth_pct >= TIER_A_MIN_DEPTH AND the recent
         structure break (BoS/CHoCH) is in the trade direction.
      B  displacement_ratio >= TIER_AB_DISPLACEMENT_RATIO AND
         mitigated_depth_pct >= TIER_AB_MIN_DEPTH. (Structure
         alignment not required -- neutral / counter-trend FVGs are
         still tradable, just not A.)
      C  everything else (passes one of the two gates, fails the
         other, OR is unmitigated).
      X  unclassified (should not happen with the detector output).
    '''
    # D first -- once a zone is inverted we want to label it D so the
    # user can see the SL-hunt candidates on the chart, not as a
    # "strong displacement" that happens to have flipped.
    if (z.pierced_bar >= 0
            and z.inverted_bar >= 0
            and (z.inverted_bar - z.pierced_bar) <= D_PIERCED_AND_INVERTED_MAX_AGE_BARS):
        return 'D'

    # Displacement ratio: how big is the middle candle vs the larger of
    # the two outer candles? body = abs(close - open).
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


# Detect FVGs at the 1m cadence (matches nb34).
zones = detect_fvg(
    opens, highs, lows, closes,
    resample_to_n_secs=60,
    fvg_min_zone_usd=0.30,
    fvg_displacement_ratio=0.0,  # we want all zones for the classifier
    fvg_body_definition='body',
    require_retest_to_invert=True,
    invalidation_min_pierce_usd=0.05,
    invalidation_min_consecutive_bars=2,
)
print(f'FVG zones: {len(zones)}')

# Per-bar structure direction (BoS/CHoCH trend state) so we can ask
# "at zone trigger_bar N, what was the structure trend?". We compute
# structure on the 1m bars and map the 1m bar index back to 1s.
df_1m = (df.set_index('time')[['open', 'high', 'low', 'close']]
         .resample('1min')
         .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
         .dropna())
if df_1m.index.tz is not None:
    df_1m.index = df_1m.index.tz_localize(None)

structure = detect_market_structure(
    df_1m['high'].to_numpy(), df_1m['low'].to_numpy(),
    df_1m['close'].to_numpy(),
    pivot_len=9, liquidity_len=30,
)

# Walk the structure breaks to get a per-bar trend timeline.
# ``state.trend`` is a single int (+1 / -1) at every detector index.
# We rebuild a per-1s-bar trend array by walking the breaks.
trend_per_1s = np.zeros(len(df), dtype=np.int8)
last = 0
# Convert 1m bar index of each break to 1s bar index.
breaks_in_1s = []
for be in structure.breaks:
    t = df_1m.index[be.bar]
    # Convert tz-naive 1m index to match df['time'] tz-awareness.
    if df['time'].dt.tz is not None:
        # df has tz-aware UTC; localize the naive 1m index to UTC.
        t = t.tz_localize('UTC') if t.tzinfo is None else t.tz_convert('UTC')
    else:
        if t.tzinfo is not None:
            t = t.tz_localize(None)
    idx_1s = df['time'].searchsorted(t, side='right') - 1
    if idx_1s < 0:
        idx_1s = 0
    if idx_1s >= len(df):
        idx_1s = len(df) - 1
    is_bull = be.kind.name in ('BOS_BULL', 'CHOCH_BULL')
    breaks_in_1s.append((idx_1s, 1 if is_bull else -1))
breaks_in_1s.sort()
cursor = 0
for brk_bar, d in breaks_in_1s:
    trend_per_1s[cursor:brk_bar + 1] = last
    last = d
    cursor = brk_bar + 1
trend_per_1s[cursor:] = last
print(f'Structure breaks mapped: {len(breaks_in_1s)}')

# Compute body sizes for the three FVG candles (c1, c2, c3) so we
# can score displacement ratio. The detector's trigger_bar points at
# the closing candle (c3) of the 3-bar pattern; c1 is at trigger_bar-2.
body = np.abs(closes - opens)
tier_counts = {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'X': 0}
tier_per_zone = []
for z in zones:
    if z.trigger_bar < 2 or z.trigger_bar >= len(df) - 1:
        tier_per_zone.append('X')
        tier_counts['X'] += 1
        continue
    c1_body = float(body[z.trigger_bar - 2])
    c3_body = float(body[z.trigger_bar])
    c2_body = float(body[z.trigger_bar - 1])
    struct_dir_at = int(trend_per_1s[z.trigger_bar - 1])
    tier = classify_zone(z, c2_body, c1_body, c3_body, struct_dir_at)
    tier_per_zone.append(tier)
    tier_counts[tier] += 1

print('Tier breakdown:', tier_counts)
""")

md("""## ORB detection -- Asia / London / NY, 30 min, wicks vs bodies""")

code("""SESSIONS = [
    ('Asia',   0),    # 00:00 UTC
    ('London', 7),    # 07:00 UTC
    ('NY',     13),   # 13:00 UTC (NY open at 13:30 falls inside this 30-min range)
]
OR_DURATION_MINS = 30

# or_sessions[name] = list of dicts {start_ns, end_ns, or_high, or_low,
# broke_high_bar (wicks), broke_high_body_bar, broke_low_bar (wicks),
# broke_low_body_bar, confirmed_break_dir (+1/-1/0)}
or_sessions = {name: [] for name, _ in SESSIONS}

NS_PER_MIN = 60_000_000_000
NS_PER_DAY = 86_400_000_000_000
NS_PER_HOUR = 3_600_000_000_000

# We work on the tz-naive 1s ns array.
times_naive_ns = times_ns  # already int64 ns
# Convert UTC->naive: subtract UTC offset (0 for UTC) -- already 0.
# Just confirm tz-awareness isn't sneaking in via df['time']:
# times_naive_ns above is int64 ns since epoch; UTC == naive at the
# wall-clock level for our purposes (no DST shifts in the parquet).

# Days present in the data:
days = (times_naive_ns // NS_PER_DAY).astype(np.int64)
unique_days = np.unique(days)

for name, start_hour in SESSIONS:
    for d in unique_days:
        day_start_ns = int(d) * NS_PER_DAY
        or_start_ns = day_start_ns + start_hour * NS_PER_HOUR
        or_end_ns = or_start_ns + OR_DURATION_MINS * NS_PER_MIN
        day_end_ns = day_start_ns + NS_PER_DAY

        # OR window bars
        or_mask = (times_naive_ns >= or_start_ns) & (times_naive_ns < or_end_ns)
        if not or_mask.any():
            continue
        or_idx = np.where(or_mask)[0]
        or_high = float(highs[or_idx].max())
        or_low = float(lows[or_idx].min())

        # Walk post-OR bars; track FIRST wick-based and FIRST body-based
        # breakouts independently.
        broke_high_bar_wick = -1
        broke_low_bar_wick = -1
        broke_high_bar_body = -1
        broke_low_bar_body = -1
        first_break_bar = -1
        first_break_dir = 0  # +1 bull, -1 bear, 0 unconfirmed

        post_mask = (times_naive_ns >= or_end_ns) & (times_naive_ns < day_end_ns)
        post_idx = np.where(post_mask)[0]
        for j in post_idx:
            h, l, c = highs[j], lows[j], closes[j]
            # Wicks: high > or_high or low < or_low.
            if broke_high_bar_wick < 0 and h > or_high:
                broke_high_bar_wick = j
            if broke_low_bar_wick < 0 and l < or_low:
                broke_low_bar_wick = j
            # Bodies: close > or_high or close < or_low.
            if broke_high_bar_body < 0 and c > or_high:
                broke_high_bar_body = j
            if broke_low_bar_body < 0 and c < or_low:
                broke_low_bar_body = j

        # Confirmation: whichever body-break fires first is the
        # "confirmed breakout" bar. Until a body break fires, the
        # tradeable region is the ORB box itself.
        candidates = []
        if broke_high_bar_body >= 0:
            candidates.append((broke_high_bar_body, +1))
        if broke_low_bar_body >= 0:
            candidates.append((broke_low_bar_body, -1))
        if candidates:
            candidates.sort()
            first_break_bar, first_break_dir = candidates[0]

        or_sessions[name].append({
            'day_start_ns': day_start_ns,
            'start_ns': or_start_ns,
            'end_ns': or_end_ns,
            'or_high': or_high,
            'or_low': or_low,
            'broke_high_bar_wick': broke_high_bar_wick,
            'broke_low_bar_wick': broke_low_bar_wick,
            'broke_high_bar_body': broke_high_bar_body,
            'broke_low_bar_body': broke_low_bar_body,
            'confirmed_break_bar': first_break_bar,
            'confirmed_break_dir': first_break_dir,
        })

# Quick summary
for name, _ in SESSIONS:
    sessions = or_sessions[name]
    n = len(sessions)
    n_confirmed = sum(1 for s in sessions if s['confirmed_break_bar'] >= 0)
    n_ambiguous = n - n_confirmed
    print(f'{name}: {n} sessions, {n_confirmed} confirmed body-breaks, {n_ambiguous} no body-break by EOD')
""")

md("""## Mid-window stats: how do tiers correlate with outcomes?""")

code("""# Walk each zone's mid-window: from trigger_bar to min(inverted_bar,
# expired_bar, played_out_bar, mitigated_bar + window). The "outcome"
# for an unmitigated zone is "untouched" (which is good -- the gap
# held); for a mitigated zone we record the depth and time-to-touch.
# For a D-tier (SL-hunt) zone, the time from pierce to retest is the
# critical measurement -- short = textbook hunt, long = real iFVG.

HORIZON_BARS = 1800  # 30 min on 1s data -- how far to look for follow-through

def zone_outcome(z, horizon=HORIZON_BARS):
    end = min(z.trigger_bar + horizon, len(df) - 1)
    if z.inverted:
        retest_gap = (z.inverted_bar - z.pierced_bar) if z.pierced_bar >= 0 else None
        return 'inverted', retest_gap
    if z.played_out_bar >= 0 and z.played_out_bar <= end:
        return 'played_out', z.played_out_bar - z.trigger_bar
    if z.mitigated_bar >= 0 and z.mitigated_bar <= end:
        return f'mitigated', z.mitigated_bar - z.trigger_bar
    return 'untouched', end - z.trigger_bar


rows = []
for z, tier in zip(zones, tier_per_zone):
    outcome, meta = zone_outcome(z)
    rows.append({
        'tier': tier,
        'direction': z.direction,
        'zone_usd': z.zone_high - z.zone_low,
        'depth_pct': z.mitigated_depth_pct,
        'outcome': outcome,
        'meta': meta,
        'trigger_bar': z.trigger_bar,
    })
outcomes_df = pd.DataFrame(rows)
print('Outcome by tier (counts):')
print(outcomes_df.groupby(['tier', 'outcome']).size().unstack(fill_value=0))
print()
print('D-tier retest-gap distribution (bars): pierce -> inverted_bar')
d = outcomes_df[(outcomes_df.tier == 'D') & (outcomes_df.outcome == 'inverted')]
if len(d) > 0:
    print(d['meta'].describe())
""")

md("""## Chart""")

code("""fig, ax = plt.subplots(figsize=(30, 15))

# Candles (1m).
x_dates = mdates.date2num(df_1m.index.to_pydatetime())
ohl = df_1m[['open', 'high', 'low', 'close']].to_numpy()
o1, h1, l1, c1 = ohl[:, 0], ohl[:, 1], ohl[:, 2], ohl[:, 3]
half_w = 0.80 / 2 / (24 * 60)
up_color = '#26a69a'
down_color = '#ef5350'
for x, lo, hi in zip(x_dates, l1, h1):
    ax.vlines(x, lo, hi, color='black', linewidth=0.6, zorder=2)
for x, oo, cc in zip(x_dates, o1, c1):
    bottom = min(oo, cc)
    height = max(abs(oo - cc), 0.001)
    color = up_color if cc >= oo else down_color
    ax.add_patch(mpatches.Rectangle((x - half_w, bottom), 2 * half_w, height,
                                    facecolor=color, edgecolor='black',
                                    linewidth=0.4, zorder=3))

# ORB overlay (per session) -- draw the ORB box and (if not yet
# body-confirmed) shade the tradeable region in a session colour.
SESSION_COLORS = {
    'Asia':   '#bb86fc',  # lavender
    'London': '#03dac6',  # teal
    'NY':     '#ff7597',  # pink
}


def _ns_to_x(ns):
    '''Convert UTC ns to matplotlib date number using our 1-second dataframe.'''
    t = pd.Timestamp(int(ns), unit='ns', tz='UTC')
    return mdates.date2num(t.tz_localize(None))


# Walk each session; draw box + (optional) "trade within ORB" shaded
# background. After a confirmed body-break, the shaded band stops and
# we draw a labelled marker at the break bar.
session_overlay_y_lo = df_1m['low'].min() - 0.30
session_overlay_y_hi = df_1m['high'].max() + 0.30
y_range = session_overlay_y_hi - session_overlay_y_lo
for name, _ in SESSIONS:
    color = SESSION_COLORS[name]
    for s in or_sessions[name]:
        # Draw the ORB box itself (thin rectangle).
        box_xl = _ns_to_x(s['start_ns'])
        box_xr = _ns_to_x(s['end_ns'])
        ax.add_patch(mpatches.Rectangle(
            (box_xl, s['or_low']), box_xr - box_xl,
            s['or_high'] - s['or_low'],
            facecolor=color, edgecolor='black',
            alpha=0.18, linewidth=1.0, zorder=2,
        ))
        # Horizontal lines on the ORB high/low for the rest of the day
        # (until a confirmed body-break).
        confirm_x = _ns_to_x(times_naive_ns[s['confirmed_break_bar']]) \
            if s['confirmed_break_bar'] >= 0 else _ns_to_x(
                s['start_ns'] + NS_PER_DAY)
        # Clip to chart x-range
        chart_xl = mdates.date2num(df_1m.index[0])
        chart_xr = mdates.date2num(df_1m.index[-1])
        line_xl = max(box_xr, chart_xl)
        line_xr = min(confirm_x, chart_xr)
        if line_xr > line_xl:
            ax.hlines(s['or_high'], line_xl, line_xr,
                      colors=color, linewidth=1.2, linestyles='--', alpha=0.85,
                      zorder=3)
            ax.hlines(s['or_low'], line_xl, line_xr,
                      colors=color, linewidth=1.2, linestyles='--', alpha=0.85,
                      zorder=3)
        # Markers: wick-based breakouts as small grey dots; body-based
        # breakouts as large coloured stars.
        for bar_idx, kind in [
            (s['broke_high_bar_wick'], 'wick_high'),
            (s['broke_low_bar_wick'],  'wick_low'),
        ]:
            if bar_idx < 0:
                continue
            bx = _ns_to_x(times_naive_ns[bar_idx])
            by = (highs[bar_idx] if kind == 'wick_high' else lows[bar_idx])
            ax.scatter([bx], [by], marker='.', s=40, color='gray',
                       edgecolors='black', linewidths=0.4, zorder=4)
        for bar_idx, kind in [
            (s['broke_high_bar_body'], 'body_high'),
            (s['broke_low_bar_body'],  'body_low'),
        ]:
            if bar_idx < 0:
                continue
            bx = _ns_to_x(times_naive_ns[bar_idx])
            by = (closes[bar_idx] if kind == 'body_high' else closes[bar_idx])
            marker = '^' if kind == 'body_high' else 'v'
            ax.scatter([bx], [by], marker=marker, s=180,
                       facecolors=color, edgecolors='black',
                       linewidths=0.8, zorder=5)

# FVG zones, color-coded by tier. We OVERLAY (not replace) the existing
# tier-A zones with a thicker gold border + darker fill so the user
# can see the "high-confidence" set at a glance.
TIER_COLORS = {
    'A': ('#ffe066', '#b8860b'),  # gold body, dark gold edge
    'B': ('#a8e6cf', '#2d6a4f'),  # green body, dark green edge
    'C': ('#d3d3d3', '#696969'),  # gray body, dark gray edge
    'D': ('#ff8a80', '#b71c1c'),  # red body, dark red edge
    'X': ('#ffffff', '#000000'),
}
FVG_FWD_BARS = 3600


def _bar_to_x(bar_idx):
    t = df['time'].iloc[bar_idx]
    return mdates.date2num(t.tz_localize(None) if t.tz is not None else t)


n_tier = {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'X': 0}
for z, tier in zip(zones, tier_per_zone):
    if z.trigger_bar >= len(df):
        continue
    runtime_caps = [z.trigger_bar + FVG_FWD_BARS]
    if z.inverted_bar >= 0 and z.inverted_bar < len(df):
        runtime_caps.append(z.inverted_bar)
    po_bar = getattr(z, 'played_out_bar', -1)
    if po_bar >= 0 and po_bar < len(df):
        runtime_caps.append(po_bar)
    if z.superseded_bar >= 0 and z.superseded_bar < len(df):
        runtime_caps.append(z.superseded_bar)
    right_bar = min(runtime_caps)
    xl = _bar_to_x(z.trigger_bar)
    xr = _bar_to_x(min(len(df) - 1, right_bar))
    if xr <= xl:
        continue
    face, edge = TIER_COLORS.get(tier, TIER_COLORS['X'])
    is_d = (tier == 'D')
    alpha = 0.35 if is_d else 0.20
    hatch = 'xx' if is_d else None
    ax.add_patch(mpatches.Rectangle(
        (xl, z.zone_low), xr - xl, z.zone_high - z.zone_low,
        facecolor=face, edgecolor=edge, alpha=alpha,
        hatch=hatch, linewidth=0.6, zorder=2.5,
    ))
    n_tier[tier] += 1

# Overlay trade-within-ORB regions: for each session, the tradeable
# region between ORB end and the confirmed body-break is the ORB box
# itself. After confirmation, we don't restrict the tradeable region.
# We add small "iFVG only within ORB" markers -- a thin vertical band
# in the session colour -- for unconfirmed sessions.
for name, _ in SESSIONS:
    color = SESSION_COLORS[name]
    for s in or_sessions[name]:
        if s['confirmed_break_bar'] >= 0:
            continue  # breakout confirmed; ORB-box restriction is lifted
        # Session day hasn't had a body-break by EOD -- the ORB
        # restriction stands. We already drew the box above; no extra
        # shading needed.

# Stats line on the chart.
_na = n_tier['A']; _nb = n_tier['B']; _nc = n_tier['C']; _nd = n_tier['D']
stats_text = (
    f'Tiers: A={_na} B={_nb} C={_nc} D={_nd} (D = SL-hunt candidate)'
)
ax.set_title('nb35 -- iFVG accuracy tiers + 30-min ORB  '
             + f'({VIZ_DATE}) | ' + stats_text,
             fontsize=12)

ax.set_ylabel('Price (USD)')
ax.set_xlabel('Time (UTC)')
ax.grid(True, alpha=0.3)
ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'{v:.2f}'))
ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=12))
ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M\\n%b %d'))
ax.set_xlim(df_1m.index[0], df_1m.index[-1])
ax.set_ylim(session_overlay_y_lo, session_overlay_y_hi)

# Legend.
legend_handles = [
    # Tier swatches
    mpatches.Patch(facecolor='#ffe066', edgecolor='#b8860b', alpha=0.45,
                   label='Tier A -- true FVG, strong displacement + structure-aligned'),
    mpatches.Patch(facecolor='#a8e6cf', edgecolor='#2d6a4f', alpha=0.30,
                   label='Tier B -- true FVG, normal'),
    mpatches.Patch(facecolor='#d3d3d3', edgecolor='#696969', alpha=0.30,
                   label='Tier C -- marginal (one gate failed)'),
    mpatches.Patch(facecolor='#ff8a80', edgecolor='#b71c1c', alpha=0.45,
                   hatch='xx',
                   label='Tier D -- probable SL-hunt (pierced+retested fast)'),
    # ORB
    mpatches.Patch(facecolor='#bb86fc', edgecolor='black', alpha=0.18,
                   label='Asia ORB (00:00 UTC, 30 min)'),
    mpatches.Patch(facecolor='#03dac6', edgecolor='black', alpha=0.18,
                   label='London ORB (07:00 UTC, 30 min)'),
    mpatches.Patch(facecolor='#ff7597', edgecolor='black', alpha=0.18,
                   label='NY ORB (13:00 UTC, 30 min)'),
    plt.Line2D([0], [0], color='gray', marker='.', linestyle='', markersize=8,
               markeredgecolor='black', markeredgewidth=0.4,
               label='Wick breakout (high/low past ORB)'),
    plt.Line2D([0], [0], color='#bb86fc', marker='^', linestyle='', markersize=10,
               markeredgecolor='black', label='Body breakout UP (close > ORB high)'),
    plt.Line2D([0], [0], color='#bb86fc', marker='v', linestyle='', markersize=10,
               markeredgecolor='black', label='Body breakout DOWN (close < ORB low)'),
]
ax.legend(handles=legend_handles, loc='upper left', ncol=2, fontsize=8,
          framealpha=0.93)
fig.subplots_adjust(top=0.93, bottom=0.07, left=0.05, right=0.97)
out = ROOT / 'notebooks' / 'nb35_chart.png'
plt.savefig(out, dpi=160)
plt.close()
print(f'Chart saved: {out}')
print(f'Zones drawn per tier: {n_tier}')
""")

md("""## Inspect: which D-tier zones are the SL-hunt candidates?""")

code("""print('Tier-D (probable SL-hunt) zones:')
print('  trigger_time          dir  pierce_bar  inv_bar  retest_gap  zone_usd  outcome')
for z, tier in zip(zones, tier_per_zone):
    if tier != 'D':
        continue
    t = df['time'].iloc[z.trigger_bar]
    gap = (z.inverted_bar - z.pierced_bar) if (z.pierced_bar >= 0 and z.inverted_bar >= 0) else -1
    zone_usd = z.zone_high - z.zone_low
    print(f'  {t}  {z.direction:+d}    '
          f'{z.pierced_bar:>10d}  {z.inverted_bar:>7d}  {gap:>10d}  '
          f'{zone_usd:.2f}  {"inverted" if z.inverted else "live"}')
""")

md("""## Notes & next steps

What this notebook gives you:

1. **A tier classification per FVG zone** using only the OHLC bars
   themselves -- no lagged indicators. The four tiers are
   intentionally conservative: D is the "did the detector just call
   an SL-hunt an iFVG?" bucket, A is the "strong displacement,
   structure-aligned" bucket.

2. **A visual A/B test of ORB breakout definitions** (wicks vs
   bodies) for three ICT sessions, with the ORB box + post-ORB
   range lines drawn so you can eyeball the tradeable region.

3. **A "trade within ORB until confirmed" baseline**: until a
   session's body-close breakout fires, the ORB box is the only
   tradeable region. After the body-close breakout, the whole chart
   is tradeable.

What's still TODO before this becomes a strategy-level change:

* The tradeable-region filter (`trade_within_orb_until_break`) is
  drawn but NOT enforced in `run_ict_backtest` yet -- that's a
  separate code change in `ict_backtest.py` (gating signal emission
  on whether the current bar falls in an un-broken ORB).
* The tier classifier is a one-shot function in this notebook; if
  you want to use it as a backtest filter, promote it to
  `src/core/ict_signals.py` and add `tier_filter` to
  `TrendStrategyParams`.
* D-tier zones look like SL-hunts but a fast retest can also be a
  real liquidity grab followed by a real reversal -- the
  `D_PIERCED_AND_INVERTED_MAX_AGE_BARS = 600` (=10 min) is a
  starting point; tune against the backtest EV.
""")

nb = {
    "cells": cells,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python",
                       "name": "python3"},
        "language_info": {"name": "python", "version": "3.12"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}

NOTEBOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
NOTEBOOK_PATH.write_text(json.dumps(nb, indent=1))
print(f'Wrote {NOTEBOOK_PATH}  ({len(cells)} cells)')
