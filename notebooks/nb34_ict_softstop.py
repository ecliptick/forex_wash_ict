# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.19.3
# ---

# %% [markdown]
# # nb34 — ICT visual validation
#
# Visual validation of the four new behaviours in this repo:
#
# 1. **3-layer orders spread evenly across the FVG zone** — layer
#    anchors visible as horizontal lines inside the FVG rectangle.
# 2. **Dynamic SL/TP** — TP rule table (0/1/2/3+ structure breaks)
#    scales the take-profit on top of the static SL/TP USD.
# 3. **FVG inversion soft-stop** — trades with `exit_reason='inv'`
#    plotted with a distinct marker.
# 4. **FVG price-rank treatment** — A/B/C tier scaling on SL/TP
#    based on the FVG's percentile within its UTC day.
#
# Additionally, this notebook overlays **market structure** (BoS / CHoCH
# / CHoCH+) on the price chart. Each structure event is rendered as a
# **horizontal bracket in the dead space** above (for bull) or below
# (for bear) the chart, **spanning from the swing bar (where the
# broken level was formed) to the break bar (where the level was
# violated)**. A text label (`BoS↑`, `BoS↓`, `CHoCH↑`, `CHoCH↓`,
# `CHoCH+↑`, `CHoCH+↓`) sits at the LEFT end of the bracket (the
# swing side) — read left-to-right: "event type [swing bar → break
# bar]". No legend entries for structure events; the label is the
# legend. The bracket never overlaps any candle because it lives in
# the dead space outside the visible y-range.
#
# **Mitigation is visible on the candle**: each FVG zone that gets
# filled has (a) a colored vertical band behind the mitigation candle
# in the FVG color, (b) a dashed connector from the zone to the
# mitigation dot, and (c) a large dot at the fill price on the zone
# edge. Trade entries sourced from FVG zones get a similar
# `FVG↑`/`FVG↓`/`iFVG↑`/`iFVG↓` label above the entry triangle +
# dashed connector to the source zone, so you can see "this trade
# fired from this zone" at a glance.
#
# 2026-09-02 changes:
#
# * **Inline render.** The chart cell embeds the matplotlib figure
#   directly via `IPython.display.display(fig)` instead of writing
#   `notebooks/nb34_chart.png`. The PNG fallback only fires if the
#   kernel lacks IPython (running the cell as a plain script).
# * **SL / TP lines removed.** The per-trade `ax.hlines(SL/TP)` loops
#   are gone, along with the 5 corresponding legend entries
#   (1× SL, 4× TP rule). The chart is a zone-lifecycle validator, not
#   a trade-management tool — the entry triangle + exit marker
#   already encode "where the trade fired" and "how it exited".
# * **BoS / CHoCH brackets span swing→break.** Each structure
#   event is now drawn as a horizontal bracket in the dead space
#   above/below the chart, spanning from the swing bar (where the
#   broken level was formed) to the break bar (where the level
#   was violated). Text label sits at the swing-side end. The
#   bracket lives outside the visible y-range so it never
#   overlaps any candle. Legend entries for BoS/CHoCH/CHoCH+
#   were removed because the labels carry the same info inline.
# * **Mitigation visible on candles.** Each FVG that gets filled
#   now has (1) a colored vertical band behind the mitigation
#   candle in the FVG color, (2) a dashed connector from the zone
#   rectangle to the mitigation dot, and (3) a large dot at the
#   fill price. Trade entries from FVG zones get a matching label
#   + dashed connector.
#
# 2026-09-15 changes (LuxAlgo-style viz parity):
#
# * **Polarity-flipped iFVG facecolor.** Following the LuxAlgo IFVG
#   indicator convention, an inverted FVG now draws with its facecolor
#   FLIPPED to the OPPOSITE polarity (a bull FVG that inverted draws
#   RED; a bear FVG that inverted draws GREEN). The edge color and
#   hatch are unchanged. This makes the polarity flip visible at a
#   glance — the rectangle's solid fill becomes the "iFVG color",
#   not the original FVG color. The dual-fill overlay (the original
#   FVG rectangle from `trigger_bar` to `inverted_bar` drawn under the
#   iFVG) is kept so the chart still shows "this used to be a bull
#   FVG" as the underlying color where appropriate.
# * **Explicit `ENTRY↑` / `ENTRY↓` text labels next to each trade
#   triangle.** The previous `FVG↑/iFVG↑` label was small and easy to
#   miss; we now draw a bold `ENTRY↑` (long) or `ENTRY↓` (short) text
#   label in a high-contrast color (darkgreen for FVG longs, darkred
#   for FVG shorts; bright magenta/cyan tones for iFVG entries) so the
#   entry fires are unmistakable on the chart.
# * **iFVG→entry connector.** When a trade's source zone has
#   `inverted=True`, a SECOND dashed connector is drawn from the
#   inversion bar (`inverted_bar` mapped to chart x) horizontally to
#   the entry bar. This visually links "this trade fired because of
#   the iFVG that started at this bar".
# * **Legend updated** with new entries for the polarity-flipped
#   iFVG colors and the new `ENTRY↑/↓` labels.
#
# 2026-09-15 polish pass:
#
# * **BoS / CHoCH brackets pushed further off the price chart.**
#   `OFFSET_MULT` bumped from 1.5× to 6.0× candle wick, line widths
#   softened (BoS hairline, CHoCH medium, CHoCH+ slightly thicker),
#   and the direction triangles at the break-bar end of the bracket
#   are REMOVED. The floating `BoS↑/CHoCH↓` text labels carry the
#   direction info — triangles were visual noise on busy days.
# * **Non-inverted FVGs use green/red.** Bull FVG → lightgreen /
#   darkgreen edge (was cyan / darkblue); bear FVG → lightcoral /
#   darkred edge (was magenta / darkred). Matches the canonical
#   ICT green-up / red-down convention. iFVG entry triangles stay
#   cyan/magenta so they're visually distinct from regular FVG
#   entries.
# * **Supersede disabled at the viz layer.** The strategy default
#   (`TrendStrategyParams.fvg_supersede_on_new=True`) is correct for
#   the backtest, but on a 1-day 1s XAUUSD sample the supersede
#   rule kills ~85%+ of detected zones (most FVGs overlap with a
#   newer zone in price because the market chops), which turned the
#   chart into a wall of `///`-hatched rectangles with very few LIVE
#   zones visible. For VISUAL VALIDATION we now call `detect_fvg`
#   with `supersede_on_new=False` so all detected zones show as live
#   rectangles. The backtest still uses the strategy default; this
#   notebook is a viz only.
# * **Mitigation dots downsized.** `s=320` → `s=90`, plus the
#   mitigation highlight band alpha dropped from 0.18 to 0.10 so
#   the candles don't get darkened. The dot is now visible but not
#   chart-dominant.
# * **Ylim padding bumped** from `candle_wick_max * 2.0` to `* 7.0`
#   to make room for the wider BoS/CHoCH bracket offset.
#
# 2026-09-15 follow-up:
#
# * **Brackets pushed further + canvas made taller.** `OFFSET_MULT`
#   bumped 6.0× → 10.0× candle wick, and the figure figsize
#   increased from `(28, 14)` to `(28, 18)`. The candles were still
#   feeling compressed at 6.0× because the canvas was squat; the
#   taller figure gives both the candles and the bracket more room.
#   Ylim padding bumped to `candle_wick_max * 11.0` to match.
#
# 2026-09-15 follow-up #2:
#
# * **Chart now plots 1m candles natively, not 1s candles.** The
#   FVG detector now runs directly on the 1m OHLC array (with
#   `resample_to_n_secs=0`) instead of the 1s array with an
#   internal 60s resample, so all zone bar indices line up with
#   1m candles. The `1m→1s` mapping loop for structure breaks is
#   gone — `structure.breaks` is indexed into the 1m bars natively
#   because the detector now consumes 1m input. The `FVG_FWD_BARS`
#   cap dropped from 3600 (1h on 1s) to 60 (1h on 1m). The candle
#   body half-width tightened from `0.80/2/24/60` (~24s) to
#   `0.45/2/24/60` (~32s) so 1m candles don't overlap. The 1s
#   `df` is still loaded for the backtest (cell 5), which needs
#   tick-level fill resolution; the chart is a viz-only consumer
#   of `df_1m`.
#
# 2026-09-15 follow-up #3:
#
# * **Viz now uses a random day + random 6h segment**, capped at
#   360 1m bars (was: fixed `2025-01-08 + 2 days`, 2,880 candles
#   crammed into an 18-inch canvas). Two motivations: (a) 6h of
#   1m candles has ~5× more vertical breathing room per candle,
#   so candles look like candles instead of thin lines; (b) random
#   sampling across the parquet's full time range means every
#   re-run tests the viz params against a different price regime.
#   Knobs: `VIZ_SEGMENT_HOURS` (default 6h), `VIZ_SEED` (default
#   `20260915` — bump for a fresh sample). For a deterministic
#   pick, override the random call by setting `VIZ_DATE` + manually
#   editing `viz_start = pd.Timestamp(VIZ_DATE, tz='UTC')`.
# * **FVG cutoff bug FIXED.** The chart cell had 13 references to
#   `len(df)` (the 1s bar count) for bar-index cap checks; the
#   detector now produces 1m bar indices, so zones with trigger
#   bar > `len(df_1m)` were silently drawn as garbage rectangles
#   pinned to the right edge of the chart (the `if xr <= xl:
#   continue` check would let them through when both clamped to
#   the same value). All chart-cell cap checks now use
#   `len(df_1m)` so out-of-range zones are correctly skipped. The
#   knife-chevron widths also bumped from 3s to 30s on the time
#   axis so the supersede-cut marker is visible at 1m scale.
#   `IFVG_FWD_BARS` clamped from 3600 (60h on 1m, way past the
#   chart's right edge) to 60 (60m, matching the FVG cap).
#
# 2026-09-15 follow-up #4:
#
# * **FVG detection back on 1s, mapped to 1m for plotting.** The
#   1m-based detection (follow-up #2) smoothed out micro-
#   structure (1-tick pierces, 1s retests) and merged consecutive
#   FVGs into single wider zones — both bad for visual
#   validation. Detector now runs on `df` (1s) again with
#   `resample_to_n_secs=0`; the chart cell maps every 1s bar
#   index to its containing 1m bar via a new `_sec_to_1m_idx`
#   helper using `np.searchsorted` on `df_1m.index.values` (O(log N)
#   per lookup, vectorizable). Same approach for structure breaks:
#   detected on 1s, mapped to 1m before the bracket loop. The
#   `_bar_to_x` helper clamps 1s indices to `[0, len(df)-1]` then
#   routes through `_sec_to_1m_idx` so all chart-drawing calls
#   stay at 1s granularity.
# * **Ylim tightened to candle range + 2× wick pad** (was: 11×).
#   The wider pad was eating ~70% of the canvas vertical space.
#   Tightening it lets the candles fill the chart vertically.
# * **Figsize bumped from (28, 18) to (28, 48)** so the
#   taller canvas has more vertical room per candle.
# * **`OFFSET_MULT` = 5× candle wick** because the tighter ylim
#   needs proportionally less bracket offset. 5× puts the bracket
#   spine at ~50% of the typical 1m candle range above/below
#   the candle — close enough to associate, far enough to not
#   overlap adjacent brackets on busy bars.
# * **Black-hatch bug FIXED on iFVG original-color overlay.**
#   The overlay used `hatch='//', edgecolor='none'`, which made
#   matplotlib render the hatch lines in BLACK (hatch lines
#   ignore `edgecolor` and use their own default color — a known
#   matplotlib quirk). User reported "black frontslash on
#   iFVGs" — that was it. Fix: drop the `hatch='//'` entirely.
#   The cyan/magenta tint of the original FVG color now reads
#   cleanly under the polarity-flipped iFVG's red/limegreen.
# * **Green→red flip now visible.** When an FVG is inverted, the
#   original FVG portion (before `inverted_bar`) was drawn at
#   alpha=0.05 (mitigated depth ≥50% branch) — effectively
#   invisible. Bumped to alpha=0.28 when the zone is inverted
#   so the cyan→red / magenta→green transition reads at
#   chart-glance level. iFVG rectangle alpha bumped 0.45 → 0.55
#   for the same reason.
# * **Background equal-length highlight bands REMOVED.** The
#   mitigation block previously drew a full-height
#   `mpatches.Rectangle` spanning the ENTIRE visible y-range
#   behind each mitigated candle — 34 of these stacked into
#   nearly-uniform colored wallpaper. Removed; only the
#   dashed connector line + dot remain.
# * **Entry text labels REMOVED.** The bold "ENTRY↑ FVG" /
#   "ENTRY↓ iFVG" text labels (with white-bbox) cluttered the
#   chart without adding information — the entry triangles +
#   dashed connectors already mark entry points unambiguously.
# * **Structure params scaled to 1s.** `pivot_len=540` /
#   `liquidity_len=1800` (×60 vs the 1m defaults) so the
#   detector produces ~3-5 BoS/CHoCH per 6h instead of
#   hundreds of micro-events.
#
# 2026-09-15 follow-up #5:
#
# * **`FVG_FWD_BARS` = 21,600 (6 hours, was 720 = 12 min).**
#   The 12-min cap was clipping long-lived FVGs — the smoke
#   test has 19 of 333 zones whose real end event was 13-200
#   minutes after trigger. With the 12-min cap, all 19 looked
#   identical (12 min wide), hiding real FVG lifetimes. The
#   user reported "each FVG survives 15+ minutes" — those were
#   real, just clipped visually. Now `FVG_FWD_BARS = 6 hours`
#   (the entire segment's wall-clock duration) so zones are
#   drawn to their actual `mitigated_bar` / `inverted_bar`
#   `superseded_bar`. Real FVG lifetimes now show: median
#   37s, p90 12 min, long tail 30-200 min. `IFVG_FWD_BARS`
#   bumped to match.
#
# ## Edit knobs
#
# The `VIZ_*` constants at the top of cell 4 are the only knobs. Edit
# and re-run to refresh the chart.

# %% [markdown]
# ## Imports + 1-week sample

# %%
import os, sys
os.environ.setdefault('MPLBACKEND', 'Agg')
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
import matplotlib.patheffects as pe
from pathlib import Path

def _find_root() -> Path:
    here = Path('.').resolve()
    candidates = [p for p in [here, *here.parents]
                  if (p / 'src' / 'core' / 'ict_signals.py').is_file()]
    if not candidates:
        raise RuntimeError('Could not find ICT repo root (no src/core/ict_signals.py)')
    # Prefer the candidate whose ict_strategy.py contains the
    # fork-only signature 'played_out_min_extension_usd' so that
    # running from a parent dir doesn't shadow the actual repo.
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

from src.core import TrendStrategyParams
from src.core.ict_signals import detect_fvg
from src.core.market_structure import detect_market_structure
from src.backtest import run_ict_backtest

print('Imports OK')
print('Project root:', ROOT)

# %%
# 2026-09-15 follow-up #3: viz now uses a RANDOM DAY + RANDOM 6h
# SEGMENT instead of a fixed 2-day window. Two motivations:
#
#   1. A fixed 2-day window × 1440 1m candles/day = 2880 candles
#      crammed into an 18-inch tall canvas made every candle a
#      thin horizontal line — visually a 1m chart that looked
#      10-second-compressed. Picking a random 6h slice (360 bars)
#      gives the candles ~5× more vertical breathing room.
#   2. Random sampling means every re-run produces a different
#      vignette — useful for stress-testing the viz params
#      (bracket offset, candle width, legend size) across many
#      different price regimes without manually picking dates.
#
# We seed the RNG so the (random day, random segment) choice is
# reproducible across re-runs. To force a different sample, bump
# ``VIZ_SEED`` or set ``VIZ_DATE = 'YYYY-MM-DD'`` + ``VIZ_SEGMENT_HOUR``
# explicitly to override the random pick.
import random as _random
VIZ_SEGMENT_HOURS = 6  # how many hours of 1m bars the chart plots
VIZ_SEGMENT_BARS = VIZ_SEGMENT_HOURS * 60
VIZ_SEED = 20260915   # change me to pick a fresh random sample
# Minimum number of 1s bars a day must have for it to be eligible
# for the random picker. Days with fewer bars (weekends, holidays,
# data-feed outages like 2025-10-18 which is a no-data gap) are
# excluded so we don't waste a re-run on an empty chart.
VIZ_MIN_DAY_BARS = 5000
_random.seed(VIZ_SEED)

# Load a 3-month rolling window so the random-day picker has plenty
# of choice; the actual viz segment is a 6h slice from inside the
# chosen day. We use the pyarrow Dataset API with predicate pushdown
# to avoid materialising the full year of 1s bars.
import pyarrow.parquet as pq
import pyarrow.dataset as ds
data_dir = ROOT / 'data'
if not (data_dir / 'XAUUSD_S1_1y.parquet').exists():
    for ancestor in [ROOT.parent, *ROOT.parent.parents]:
        sibling = ancestor / 'data'
        if (sibling / 'XAUUSD_S1_1y.parquet').exists():
            data_dir = sibling
            break

parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'
# Build a cheap "which days have data" map without scanning every
# row. pyarrow can read the row-group statistics for the time
# column on a sorted parquet and tell us the time range of every
# row-group. For a parquet written in chronological order the
# per-row-group min/max gives us an approximate "first and last
# bar" of each contiguous run; the union of those runs is the
# activity map.
pf = pq.ParquetFile(str(parquet_path))
_parq_t0 = pf.read_row_group(0, columns=['time']).to_pandas()['time'].iloc[0]
_parq_t1 = pf.read_row_group(pf.metadata.num_row_groups - 1, columns=['time']).to_pandas()['time'].iloc[-1]
_parq_t0 = pd.to_datetime(_parq_t0, utc=True)
_parq_t1 = pd.to_datetime(_parq_t1, utc=True)
print(f'Parquet time range: {_parq_t0} -> {_parq_t1}')

# Build eligible-days list (days with >= VIZ_MIN_DAY_BARS 1s bars).
# This is one pyarrow scan per day that passes the cheap fast-path
# (>= 1 row in that day's bucket) and an exact count for the days
# we keep. Faster than scanning 365 days worth of data on every run.
ds_ = ds.dataset(str(parquet_path), format='parquet')
_all_days = pd.date_range(_parq_t0.normalize(), _parq_t1.normalize(), freq='D', tz='UTC')
_random.shuffle(_all_days.to_list())  # so we iterate in random order
eligible_days = []
for _d in _all_days:
    _t0 = _d
    _t1 = _d + pd.Timedelta(days=1)
    _n = ds_.to_table(
        columns=['time'],
        filter=(ds.field('time') >= _t0) & (ds.field('time') < _t1),
    ).num_rows
    if _n >= VIZ_MIN_DAY_BARS:
        eligible_days.append(_d)
    if len(eligible_days) >= 30:
        # 30 eligible days is plenty of variety for one re-run;
        # bail early to keep the picker fast.
        break
print(f'Eligible days (>= {VIZ_MIN_DAY_BARS:,} bars): {len(eligible_days)} '
      f'of {len(_all_days)} total')

if not eligible_days:
    raise RuntimeError('No days in the parquet have enough bars for the viz.')

# Pick a random eligible day, then a random starting hour inside it.
viz_date = _random.choice(eligible_days)
_random_hour = _random.randint(0, max(24 - VIZ_SEGMENT_HOURS, 0))
viz_start = viz_date + pd.Timedelta(hours=_random_hour)
viz_end = viz_start + pd.Timedelta(hours=VIZ_SEGMENT_HOURS)
VIZ_DATE = viz_date.strftime('%Y-%m-%d')
VIZ_SEGMENT_START = viz_start.strftime('%H:%M UTC')
print(f'Pick: {VIZ_DATE} starting {VIZ_SEGMENT_START} '
      f'(window: {viz_start} -> {viz_end})')

table = ds_.to_table(
    columns=['time', 'open', 'high', 'low', 'close', 'tickv'],
    filter=(ds.field('time') >= viz_start) & (ds.field('time') < viz_end),
)
df = table.to_pandas()
df = df.rename(columns={'tickv': 'volume'})
df['time'] = pd.to_datetime(df['time'], utc=True)
df = df.sort_values('time').reset_index(drop=True)
print(f'Data dir: {data_dir}')
print(f'Viz sample: {len(df):,} 1s bars  ({df.time.iloc[0]} -> {df.time.iloc[-1]})')

# %% [markdown]
# ## Run the backtest + show summary

# %%
p = TrendStrategyParams(
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
    ms_boost_conviction=1.5,
    use_atr_scaling=True,
    atr_len=1200,
    sl_atr_mult=0.25, tp_atr_mult=0.55,
    sl_usd=0.80, tp_usd=1.80, lots=0.01,
)
res = run_ict_backtest(df, p, strategy_label='viz_ict')
s = res.summary()
for k, v in s.items():
    print(f'  {k}: {v}')
print(f'  n_signals_emitted: {res.n_signals_emitted}')
print(f'  n_signals_consumed: {res.n_signals_consumed}')
print(f'  n_fills: {res.n_fills}')
print(f'  n_soft_stops: {res.n_soft_stops}')
print(f'  n_inversions_detected: {res.n_inversions_detected}')

# %% [markdown]
# ## Build 1m candles + detect FVG / structure

# %%
# 2026-09-15 follow-up #4: viz now does FVG detection on the 1s
# bars (NOT on the 1m bars) so the zone lifecycle is the genuine
# fine-grained one. The chart's x-axis still plots 1m candles
# (for readability); every zone bar index is mapped to the 1m
# bar containing that 1s timestamp via `_sec_to_1m_idx` defined
# below. Same approach as follow-up #1 — but the detector now
# runs on the FULL 1s granularity again because that's where
# micro-structure (1-tick pierces, 1s retests) actually lives.
# 1m-based detection smoothed out small zones and merged
# consecutive FVGs into a single wider zone, both of which
# are bad for visual validation.
df_1m = (df.set_index('time')[['open', 'high', 'low', 'close']]
         .resample('1min')
         .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
         .dropna())
if df_1m.index.tz is not None:
    df_1m.index = df_1m.index.tz_localize(None)
print(f'1m candles: {len(df_1m):,}')

# FVG detection on the 1s bars (resample_to_n_secs=0 = "no internal
# resample"; the input is the working timeframe). With supersede-on-
# new DISABLED so the chart shows ALL detected zones as live
# rectangles for visual validation (the backtest still uses the
# strategy default).
zones = detect_fvg(
    df['open'].to_numpy(), df['high'].to_numpy(),
    df['low'].to_numpy(), df['close'].to_numpy(),
    resample_to_n_secs=0,    # already on the 1s timeframe
    fvg_min_zone_usd=0.30,
    fvg_displacement_ratio=0.0,
    fvg_body_definition='body',
    # 2026-09-15: supersede-on-new DISABLED at the viz layer.
    #
    # The strategy default (TrendStrategyParams.fvg_supersede_on_new=True)
    # is correct for the backtest — a new FVG that overlaps an
    # older one in price makes the older one a stale level. But on
    # a 1-day 1s XAUUSD sample, the supersede rule kills ~85%+ of
    # detected zones (most FVGs overlap with a newer zone in price
    # because the market chops inside a tight range), which turns
    # the chart into a wall of `///`-hatched rectangles with very
    # few LIVE zones visible.
    #
    # For VISUAL VALIDATION we want to see ALL detected zones as
    # live rectangles so the user can eyeball the lifecycle. The
    # backtest still uses the strategy default — this notebook is
    # a viz only, not a strategy source of truth. If you want to
    # see what the strategy actually does, set this back to True
    # and re-run.
    supersede_on_new=False,
)
print(f'FVG zones: {len(zones)}')
n_inv = sum(1 for z in zones if z.inverted)
print(f'  inverted: {n_inv}')

# 2026-09-17: demonstrate the new ``fvg_min_lifetime_secs`` filter.
# The detector currently returns every zone (filter OFF). Sweep the same
# data through thresholds of 3, 10, and 30 seconds to show how the
# "born-dead" zones drop out. The chart above still draws the unfiltered
# set; this is just a diagnostic.
#
# NOTE: on 1s data, 1 detector bar == 1 wall-clock second, so the
# parameter is a pure bar-count. No ``times_utc_ns`` needed.
for _lt in (0, 3, 10, 30):
    _zones_lt = detect_fvg(
        df['open'].to_numpy(), df['high'].to_numpy(),
        df['low'].to_numpy(), df['close'].to_numpy(),
        resample_to_n_secs=0,
        fvg_min_zone_usd=0.30,
        fvg_displacement_ratio=0.0,
        fvg_body_definition='body',
        supersede_on_new=False,
        fvg_min_lifetime_secs=_lt,
    )
    _dropped = len(zones) - len(_zones_lt)
    print(f'  fvg_min_lifetime_secs={_lt:3d}s → '
          f'{len(_zones_lt)} zones kept, {_dropped} dropped '
          f'({_dropped/max(len(zones),1)*100:.0f}%)')
del _zones_lt

# Map every 1s bar index to its containing 1m bar index. We do
# this once up-front so the chart's x-axis lookups are O(1) per
# bar. The mapping uses the same searchsorted trick the previous
# round used for structure breaks.
_t_1m_ns = df_1m.index.values.astype('datetime64[ns]').astype('int64')  # int64 ns
def _sec_to_1m_idx(sec_idx: int) -> int:
    if sec_idx >= len(df):
        sec_idx = len(df) - 1
    if sec_idx < 0:
        sec_idx = 0
    # Convert the 1s bar's timestamp to int64 nanoseconds (UTC).
    # df['time'] is tz-aware (UTC) so .value is the right epoch
    # without needing localization.
    t_ns = df['time'].iloc[sec_idx].value
    # Binary search against the 1m-candle timestamps (also int64 ns).
    idx = int(np.searchsorted(_t_1m_ns, t_ns, side='right')) - 1
    if idx < 0:
        idx = 0
    if idx >= len(df_1m):
        idx = len(df_1m) - 1
    return idx

# Map structure breaks (1s indices) into 1m indices so the
# bracket-drawing loop can index into df_1m for wick lookups.
#
# 2026-09-15 follow-up #4: structure detection runs on the 1s bars
# for granularity, BUT with `pivot_len` / `liquidity_len` scaled UP
# by 60× to match the 1m-bar semantic of the previous round.
# Otherwise the detector on 1s produces hundreds of micro-BoSes
# (every 1-second pivot becomes a BoS), which makes the chart
# unreadable. pivot_len=540 (= 9 min on 1s) / liquidity_len=1800
# (= 30 min on 1s) gives roughly the same "one BoS every few bars"
# density as the 1m bars version.
structure = detect_market_structure(
    df['high'].to_numpy(), df['low'].to_numpy(),
    df['close'].to_numpy(),
    pivot_len=540, liquidity_len=1800,
)
# Same resample — the detector is run on 1s bars here too, so the
# bracket swing/break bars line up with 1s indices and need to be
# mapped to 1m for the chart. This is the trade-off: 1s detection
# gives accurate zones but the chart now needs an extra mapping
# step.
breaks_1m = []
for be in structure.breaks:
    b_1m = _sec_to_1m_idx(int(be.bar))
    s_1m = _sec_to_1m_idx(int(getattr(be, 'broken_swing_bar', be.bar) or be.bar))
    breaks_1m.append(type(be)(
        kind=be.kind, bar=b_1m,
        broken_swing_bar=s_1m,
        broken_swing_price=getattr(be, 'broken_swing_price', 0.0),
    ))
structure.breaks = breaks_1m
chochp_bars = set()  # placeholder if we don't have the exact predicate
print(f'Structure breaks: BoS={sum(1 for b in structure.breaks if b.kind.name in ("BOS_BULL", "BOS_BEAR"))}, '
      f'CHoCH={sum(1 for b in structure.breaks if b.kind.name in ("CHOCH_BULL", "CHOCH_BEAR"))}')

# %% [markdown]
# ## Chart

# %%
# Build the per-trade DataFrame from ``res.trades`` directly (NOT
# ``res.trades_df()``) because the dataclass ``to_dict()`` drops
# ``fvg_zone`` for JSON-serialisability. The chart needs the live
# ``FvgZone`` reference to draw the entry→zone connector + iFVG label.
# 2026-09-15 fix: trades_df() previously broke the fvg_zone lookup
# silently (the ``t.get('fvg_zone')`` check returned None and the
# label was never drawn). Now we read the trade dataclasses directly.
trade_rows = []
for t in res.trades:
    trade_rows.append({
        'entry_time': pd.to_datetime(t.entry_time, unit='ns', utc=True),
        'exit_time': pd.to_datetime(t.exit_time, unit='ns', utc=True),
        'direction': int(t.direction),
        'entry_price': float(t.entry_price),
        'exit_price': float(t.exit_price),
        'exit_reason': str(t.exit_reason),
        'pnl_usd': float(t.pnl_usd),
        'hold_secs': float(t.hold_secs),
        'layer_idx': int(t.layer_idx),
        'is_ifvg': bool(t.is_ifvg),
        'entry_triggered_by': str(t.entry_triggered_by),
        'fvg_zone': t.fvg_zone,  # keep the live FvgZone reference
    })
trades = pd.DataFrame(trade_rows)

fig, ax = plt.subplots(figsize=(28, 48))

# ── Color palette (2026-09-15 polish pass) ─────────────────────────
# Non-inverted FVGs now use green/red (matches the canonical ICT
# green-up / red-down convention used everywhere else in the repo)
# instead of cyan/magenta. The iFVG entry triangles stay in cyan /
# magenta so they're visually distinct from regular FVG entries.
BULL_FVG_FACE = 'lightgreen'    # was 'cyan'
BEAR_FVG_FACE = 'lightcoral'    # was 'magenta'
BULL_FVG_EDGE = 'darkgreen'     # was 'darkblue'
BEAR_FVG_EDGE = 'darkred'       # unchanged
# iFVG polarity-flipped colors (unchanged from 2026-09-15):
#   bull FVG inverted → red face, darkred edge
#   bear FVG inverted → limegreen face, darkgreen edge
BULL_IFVG_FACE = 'red'
BEAR_IFVG_FACE = 'limegreen'

# Candles
x_dates = mdates.date2num(df_1m.index.to_pydatetime())
ohl = df_1m[['open', 'high', 'low', 'close']].to_numpy()
opens, highs, lows, closes = ohl[:, 0], ohl[:, 1], ohl[:, 2], ohl[:, 3]
half_w = 0.45 / 2 / (24 * 60)  # 1m candle body half-width ≈ 32s (leaves a small gap)
up_color = '#26a69a'
down_color = '#ef5350'
for x, lo, hi in zip(x_dates, lows, highs):
    ax.vlines(x, lo, hi, color='black', linewidth=0.6, zorder=2)
for x, o, c in zip(x_dates, opens, closes):
    bottom = min(o, c)
    height = max(abs(o - c), 0.001)
    color = up_color if c >= o else down_color
    ax.add_patch(mpatches.Rectangle((x - half_w, bottom), 2 * half_w, height,
                                    facecolor=color, edgecolor='black',
                                    linewidth=0.4, zorder=3))

# FVG zones. Each zone gets a rectangle cut short at the EARLIEST of:
#   played_out_bar  (direction ran past the zone edge)
#   superseded_bar  (a newer zone overlapped in price, gated on time)
#   inverted_bar    (polarity flipped — replaced by the iFVG below)
#   trigger_bar + FVG_FWD_BARS (default visibility cap)
# Zones with pierced_bar set but inverted=False (an SL-hunt probe that
# did not retest — added 2026-08-20) get a small dashed tick at the
# pierce bar so the user can see the probe on the chart.
# FVG visibility cap (the latest moment a zone is drawn on the chart).
# 2026-09-15 follow-up #4: was 3600 (= 60 min on 1s wall clock)
# before the previous round clamped it to 720 (= 12 min). The
# 12-min cap was hiding real FVG lifetimes — several zones in the
# smoke test actually live 30-200 minutes (because the zone's
# trigger candle was never retested), and a 12-min cap visually
# clips them all to 12-min rectangles. The user reported "FVGs
# survive 15+ minutes" — those are real.
#
# Restored to a generous cap: 6 hours (= 21,600 1s bars), matching
# the chart's segment duration. A zone with no real end event is
# drawn all the way to the segment end (and the segment already
# only shows 6h of price action, so this is a safe visual cap that
# never overshoots the chart's right edge).
FVG_FWD_BARS = 21_600  # 6 hours on 1s wall clock
def _bar_to_x(bar_idx):
    # 2026-09-15 follow-up #4: bar indices now point into the 1s
    # `df` (FVG detection runs on 1s again). Map the 1s index to
    # its containing 1m bar so the chart's x-axis (which plots 1m
    # candles) shows the zone at the correct candle. Clamp on
    # either side so a stray index doesn't crash.
    if bar_idx >= len(df):
        bar_idx = len(df) - 1
    if bar_idx < 0:
        bar_idx = 0
    t = df_1m.index[_sec_to_1m_idx(bar_idx)]
    return mdates.date2num(t.tz_localize(None) if t.tz is not None else t)

def _bar_1m_to_x(bar_1m_idx):
    # Used for structure breaks, which are already mapped to 1m
    # bar indices by the breaks_1m loop. Direct lookup.
    if bar_1m_idx >= len(df_1m):
        bar_1m_idx = len(df_1m) - 1
    if bar_1m_idx < 0:
        bar_1m_idx = 0
    t = df_1m.index[bar_1m_idx]
    return mdates.date2num(t.tz_localize(None) if t.tz is not None else t)
n_drawn = 0
n_superseded_drawn = 0
n_played_out_drawn = 0
n_mitigated_drawn = 0
for z in zones:
    if z.trigger_bar >= len(df):
        continue
    runtime_caps = [z.trigger_bar + FVG_FWD_BARS]
    if z.inverted and z.inverted_bar >= 0 and z.inverted_bar < len(df):
        runtime_caps.append(z.inverted_bar)
    # Use getattr so the chart stays renderable even if a zone was
    # built from an older detect_fvg that didn't yet track the
    # played-out lifecycle event. (-1 == not played out, treated as
    # 'no cap').
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
    if z.superseded_bar >= 0:
        # Cut short at superseded_bar — the OLDER zone ends at this
        # bar because a NEWER zone formed overlapping its price
        # range. Make the cut visually unmistakable: solid vertical
        # line at the cut bar, plus "knife" chevrons pointing right
        # at the cut so the user sees the cut going RIGHT (newer
        # bar comes after).
        if z.mitigated_bar >= 0:
            opacity = 0.10
            n_mitigated_drawn += 1
        else:
            opacity = 0.22
        face = BULL_FVG_FACE if z.direction == 1 else BEAR_FVG_FACE
        edge = BULL_FVG_EDGE if z.direction == 1 else BEAR_FVG_EDGE
        ax.add_patch(mpatches.Rectangle((xl, z.zone_low), xr - xl, z.zone_high - z.zone_low,
                                        facecolor=face, edgecolor=edge, alpha=opacity,
                                        hatch='///', linewidth=0.4, zorder=2))
        # Solid vertical cut line at superseded_bar — the "knife"
        # that cuts the zone short.
        sup_x = _bar_to_x(z.superseded_bar)
        ax.plot([sup_x, sup_x], [z.zone_low, z.zone_high],
                color=edge, linewidth=1.4, linestyle='-', alpha=0.9, zorder=3)
        # "Knife" chevrons pointing right at the cut.
        cut_y_low = z.zone_low + (z.zone_high - z.zone_low) * 0.25
        cut_y_mid = (z.zone_low + z.zone_high) / 2.0
        cut_y_high = z.zone_high - (z.zone_high - z.zone_low) * 0.25
        # 2026-09-15 follow-up #3: knife chevron widths bumped from
        # 3s to 30s on the time axis so the cut mark is visible on
        # 1m candles (the previous 3s was a hairline at this scale).
        knife_w = 30.0 / (24 * 60 * 60)
        for cy in (cut_y_low, cut_y_mid, cut_y_high):
            ax.plot([sup_x, sup_x + knife_w * 4], [cy, cy],
                    color=edge, linewidth=1.2, alpha=0.85, zorder=3.5)
            ax.plot([sup_x, sup_x + knife_w * 2], [cy, cy - knife_w * 8],
                    color=edge, linewidth=1.2, alpha=0.85, zorder=3.5)
            ax.plot([sup_x, sup_x + knife_w * 2], [cy, cy + knife_w * 8],
                    color=edge, linewidth=1.2, alpha=0.85, zorder=3.5)
        n_superseded_drawn += 1
    elif po_bar >= 0:
        # Wheat + dot hatch; the direction ran past the zone edge.
        face = 'wheat'
        edge = 'saddlebrown'
        ax.add_patch(mpatches.Rectangle((xl, z.zone_low), xr - xl, z.zone_high - z.zone_low,
                                        facecolor=face, edgecolor=edge, alpha=0.10,
                                        hatch='..', linewidth=0.4, zorder=2))
        n_played_out_drawn += 1
    else:
        # Live FVG (with mitigation depth shading)
        # 2026-09-15 follow-up #4: when the zone is inverted,
        # bump the original FVG portion's alpha from 0.05 (very
        # faint) up to 0.28 so the green-to-red flip is visible
        # at chart-glance level. Without this, the original FVG
        # color was effectively invisible under the iFVG's
        # polarity-flipped rectangle.
        if z.mitigated_bar >= 0 and z.mitigated_depth_pct >= 0.5:
            if z.inverted:
                opacity = 0.28
            else:
                opacity = 0.05
            n_mitigated_drawn += 1
        elif z.mitigated_bar >= 0:
            opacity = 0.12
            n_mitigated_drawn += 1
        else:
            opacity = 0.22
        face = BULL_FVG_FACE if z.direction == 1 else BEAR_FVG_FACE
        edge = BULL_FVG_EDGE if z.direction == 1 else BEAR_FVG_EDGE
        ax.add_patch(mpatches.Rectangle((xl, z.zone_low), xr - xl, z.zone_high - z.zone_low,
                                        facecolor=face, edgecolor=edge, alpha=opacity,
                                        linewidth=0.4, zorder=2))
    # SL-hunt probe marker (added 2026-08-20): a dashed vertical tick at
    # ``pierced_bar`` when the zone was probed but never retested
    # (``pierced_bar >= 0`` and ``inverted_bar < 0``). This is the
    # common SL-hunt pattern on 1s XAUUSD — price closes past the zone
    # for a few ticks then keeps going one-way. With
    # ``fvg_require_retest_to_invert=True`` the zone stays alive but the
    # user can see where the probe happened on the chart.
    if (
        z.pierced_bar >= 0
        and z.inverted_bar < 0
        and z.pierced_bar < len(df)
    ):
        probe_x = _bar_to_x(z.pierced_bar)
        ax.plot([probe_x, probe_x], [z.zone_low, z.zone_high],
                color='orange', linewidth=1.0, linestyle='--',
                alpha=0.85, zorder=3)
    n_drawn += 1

# iFVG rectangle: drawn ONLY for inverted zones, starting at
# inverted_bar (NOT at the original trigger_bar). Same vertical
# extent as the original FVG, but hatched + flipped-polarity
# facecolor AND edge color so the polarity flip is visible on the
# time axis at a glance.
#
# 2026-09-15 (LuxAlgo parity): the facecolor is now FLIPPED to the
# OPPOSITE polarity — a bull FVG that inverted draws RED (the color
# it would have had if it had been a bear zone), and a bear FVG that
# inverted draws GREEN. This matches the canonical "inversion"
# semantic: an iFVG is no longer the same direction as the FVG it
# was born from, so its visual color should reflect the NEW
# direction, not the old one.
#
# Original FVG colors (kept for comparison):
#   bull FVG  → facecolor 'cyan',   edgecolor 'darkblue'
#   bear FVG  → facecolor 'magenta', edgecolor 'darkred'
#
# iFVG colors (LuxAlgo parity):
#   bull FVG inverted (now BEAR direction) → facecolor 'red',      edgecolor 'darkred'
#   bear FVG inverted (now BULL direction) → facecolor 'limegreen', edgecolor 'darkgreen'
# 2026-09-15 follow-up #4: bumped to 720 (12 hours on 1s = 60 min
# on 1m after the mapping). Matches the FVG cap and gives iFVGs
# enough room to render their polarity flip on the chart.
#
# Follow-up #5: bumped to 21,600 (6 hours on 1s = 6 hours on 1m
# after the mapping). Long-lived inverted zones (some live 1-3
# hours in the smoke test) were being clipped at the 12-min
# cap. Now they render to the segment end where they actually end.
IFVG_FWD_BARS = 21_600
for z in zones:
    if not (z.inverted and z.inverted_bar >= 0 and z.inverted_bar < len(df)):
        continue
    ifxl = _bar_to_x(z.inverted_bar)
    ifxr = _bar_to_x(min(len(df) - 1, z.inverted_bar + IFVG_FWD_BARS))
    if ifxr <= ifxl:
        continue
    # Polarity-FLIPPED facecolor + edge color (2026-09-15).
    # Bull FVG inverted → iFVG is now bearish → red.
    # Bear FVG inverted → iFVG is now bullish → green.
    is_bull_orig = z.direction > 0
    face = 'red' if is_bull_orig else 'limegreen'
    edge = 'darkred' if is_bull_orig else 'darkgreen'
    ax.add_patch(mpatches.Rectangle((ifxl, z.zone_low), ifxr - ifxl, z.zone_high - z.zone_low,
                                    facecolor=face, edgecolor=edge, alpha=0.55,
                                    hatch='\\', linewidth=0.8, zorder=2))
    inv_x = _bar_to_x(z.inverted_bar)
    ax.plot([inv_x, inv_x], [z.zone_low, z.zone_high],
            color='black', linewidth=1.2, linestyle=':', zorder=3)
    # Inversion diagonal hatching: the second half of the ORIGINAL
    # zone (from inverted_bar to the runtime cap) is overlaid with a
    # diagonal hatch so the polarity flip is visible at a glance.
    # The first half is drawn normally (the FVG was "valid" before
    # the inversion); the second half is visually distinct.
    inv_right_bar = min(
        z.trigger_bar + FVG_FWD_BARS,
        po_bar if po_bar >= 0 and po_bar < len(df) else z.trigger_bar + FVG_FWD_BARS,
        z.superseded_bar if z.superseded_bar >= 0 and z.superseded_bar < len(df) else z.trigger_bar + FVG_FWD_BARS,
    )
    inv_xr = _bar_to_x(min(len(df) - 1, inv_right_bar))
    if inv_xr > inv_x:
        # Original-FVG-color overlay so the user can still see
        # "this iFVG was a bull/bear FVG before inversion" — drawn
        # UNDER the polarity-flipped iFVG rectangle, lower alpha,
        # so the flipped color wins visually but the original is
        # hint-able.
        #
        # 2026-09-15 follow-up #4: NO hatch on the original-color
        # overlay. Matplotlib's `hatch='//'` renders in BLACK when
        # `edgecolor='none'` is set on the parent patch — a known
        # matplotlib quirk where hatch lines ignore `edgecolor`
        # and use their own default color. The result was a sea of
        # black frontslash hatches overlapping the iFVG, which is
        # what the user reported as "black frontslash on iFVGs".
        # Without the hatch, the cyan/magenta tint of the original
        # FVG color reads cleanly under the polarity-flipped
        # rectangle's stronger red/limegreen.
        orig_face = BULL_FVG_FACE if is_bull_orig else BEAR_FVG_FACE
        ax.add_patch(mpatches.Rectangle((inv_x, z.zone_low), inv_xr - inv_x,
                                        z.zone_high - z.zone_low,
                                        facecolor=orig_face, edgecolor='none',
                                        alpha=0.22, linewidth=0.0, zorder=2.3))
    n_drawn += 1

# ── Mitigation markers (the actual fill point + candle highlight) ─
# A mitigation marker has THREE components:
#
# 1. **Vertical highlight band** behind the candle at `mitigated_bar`
#    in the FVG color (light cyan/magenta tint at low alpha). This
#    makes the candle's involvement in the mitigation UNMISTAKABLE —
#    the whole bar gets a colored backdrop.
#
# 2. **Connector line** from the FVG zone rectangle's RIGHT edge
#    (at `mitigated_bar` x-position) down/up to the mitigation dot.
#    Drawn as a dashed line in the zone color. This links the
#    zone to the bar visually.
#
# 3. **Large mitigation dot** at the actual fill price (bar's
#    close if close inside zone, else bar's high/low on the
#    entry side of the zone). Big `s=320` so it pops over
#    candles + FVG + structure markers.
#
# The fill price is anchored INSIDE the zone rectangle's vertical
# range so the dot sits ON the FVG rectangle (not floating off to
# the side on a candle wick). For a bull FVG (price dived DOWN
# into the zone), the dot sits at `max(zone_low, bar_low)` so
# it's at the zone's bottom edge or above. For a bear FVG, the
# dot sits at `min(zone_high, bar_high)`.
y_min, y_max = df_1m['low'].min(), df_1m['high'].max()
y_range = max(y_max - y_min, 0.01)
n_mit_dots = 0
for z in zones:
    if z.mitigated_bar < 0 or z.mitigated_bar >= len(df):
        continue
    if z.superseded_bar >= 0 and z.mitigated_bar > z.superseded_bar:
        continue  # mitigation past the superseded cutoff — skip
    # 2026-09-15 follow-up #4: z.mitigated_bar is a 1s index but
    # df_1m only has 1m candles. Map to 1m bar for price lookups.
    mit_1m = _sec_to_1m_idx(z.mitigated_bar)
    mx = _bar_to_x(z.mitigated_bar)
    bar_low = float(df_1m['low'].iloc[mit_1m])
    bar_high = float(df_1m['high'].iloc[mit_1m])
    bar_close = float(df_1m['close'].iloc[mit_1m])
    is_bull_z = z.direction > 0
    # Anchor the dot AT the zone's edge (so it sits on the rectangle).
    # Bull FVG → retest came in from above → dot at zone_low edge
    #   (or bar_low if bar_low is above the zone, wick-only).
    # Bear FVG → retest came in from below → dot at zone_high edge
    #   (or bar_high if bar_high is below the zone, wick-only).
    if is_bull_z:
        # Bar dipped into the zone. Dot sits at the deepest point
        # the bar reached inside the zone — clamp to zone_low so
        # the dot sits ON the rectangle (not above it).
        my = max(z.zone_low, min(z.zone_high, bar_low))
    else:
        my = min(z.zone_high, max(z.zone_low, bar_high))
    # Zone color (used for the connector + dot).
    zone_face = BULL_FVG_FACE if is_bull_z else BEAR_FVG_FACE
    zone_edge = BULL_FVG_EDGE if is_bull_z else BEAR_FVG_EDGE
    # Connector line from the zone's right edge to the dot.
    # Right edge of zone at zone_top or zone_bottom (whichever is
    # closer to the dot's y). Vertical line down to the dot.
    if is_bull_z:
        connector_top = z.zone_high
    else:
        connector_top = z.zone_low
    ax.plot([mx, mx], [connector_top, my],
            color=zone_edge, linewidth=1.4, alpha=0.7,
            linestyle=(0, (3, 2)),  # dashed: 3-on, 2-off
            solid_capstyle='butt', zorder=3)
    # The mitigation dot — colored by depth:
    # dark green for >=50%, orange for >=25%, gray for <25%.
    if z.mitigated_depth_pct >= 0.5:
        mit_color = 'darkgreen'
    elif z.mitigated_depth_pct >= 0.25:
        mit_color = 'orange'
    else:
        mit_color = 'gray'
    # Downsized dot — visible but not chart-dominant.
    ax.scatter([mx], [my], marker='o', s=90,
               facecolors=mit_color, edgecolors='black',
               linewidths=1.2, zorder=10, alpha=1.0)
    n_mit_dots += 1
print(f'Mitigation markers drawn: {n_mit_dots}')

# ── BoS / CHoCH / CHoCH+ markers (└──────┘ brackets spanning ────
# swing-bar → break-bar, with text label) ─────────────────────────
# Each structure event is marked by a `└──────┘`-style bracket
# in the dead space above the chart (for bull breaks) or below
# (for bear breaks). The bracket visually says "from swing bar
# (left) to break bar (right)", with vertical end caps that DROP
# DOWN from the spine (bull) or RISE UP from the spine (bear) to
# the candle wick at each end — anchoring the bracket to the
# actual candles it describes:
#
#   Bull break (swing-high broken to the upside)
#
#                    ─────────────────────  ← spine in dead space
#                    │                    │   above the chart
#                    │                    │
#                    │                    │ ← end caps DROP DOWN
#                    ┤                    ├   to the candle wicks
#                   ╱╲                   ╱╲
#                  ╱  ╲                 ╱  ╲
#                swing bar          break bar
#                (candle #1234)    (candle #1550)
#
#   Bear break (swing-low broken to the downside)
#
#                  ╲╱                   ╲╱
#                   ╲                    ╱
#                   │                    │  ← candle wicks
#                   │                    │
#                   │                    │ ← end caps RISE UP
#                   │                    │   from the spine
#                    ─────────────────────  ← spine in dead space
#                                            BELOW the chart
#                swing bar          break bar
#                (candle #1234)    (candle #1550)
#
# The bracket is anchored ABOVE the highest high in the visible
# chart (for bull) or BELOW the lowest low (for bear) so it
# never overlaps any candle. The horizontal "spine" sits at
# a fixed offset above y_max / below y_min; the two end caps
# drop/rise back down toward (but not touching) the candle
# wicks at the swing bar and the break bar.
#
# Text label sits AT THE RIGHT END of the bracket (the break side)
# so the eye reads left-to-right: "this swing bar ... this break
# bar → event type". The label is placed slightly above/below
# the bracket spine so it doesn't overlap the bracket line.
#
# The bracket is annotated with a single token ("BoS↑", "BoS↓",
# "CHoCH↑", "CHoCH↓", "CHoCH+↑", "CHoCH+↓") — no legend entries
# for structure events, the label IS the legend.
y_min, y_max = df_1m['low'].min(), df_1m['high'].max()
y_range = max(y_max - y_min, 0.01)
# Bracket anchor parameters.
# The bracket is drawn in `└──────┘` style: a horizontal spine
# sitting in the dead space above the chart (for bull) / below
# (for bear), with two vertical end-caps that DROP DOWN (bull) /
# RISE UP (bear) from the spine toward the candle wick at each
# end. The caps visually anchor the bracket to the candles they
# describe — "this bracket covers from this swing bar's high
# (left cap touches the candle's high) to this break bar's high
# (right cap touches the candle's high)".
#
# Per-candle offset:
#
#   The spine offset is RELATIVE TO THE CANDLE, not to the whole
#   chart's y-range. For each bracket we compute:
#     swing_wick_range = swing_bar.high - swing_bar.low
#     break_wick_range = break_bar.high - break_bar.low
#     candle_ref       = max(swing_wick_range, break_wick_range)
#     spine_offset     = candle_ref * OFFSET_MULT (default 5.0×)
#
#   This means a tight-range candle (e.g. 0.10 USD wick) gets a
#   bracket spine 0.15 USD above it, while a wide-range candle
#   (e.g. 5.00 USD wick) gets a spine 7.50 USD above. The bracket
#   is always proportional to its constituent candles.
#
#   Bull (swing-high broken to the upside)
#
#                    ───────────  ← spine at candle_ref * 1.5
#                    │          │   above the candle wick
#                    │          │
#                    │          │ ← end caps DROP DOWN
#                    ┤          ├   to the candle wicks
#                   ╱╲         ╱╲
#                  ╱  ╲       ╱  ╲
#
#   Bear (swing-low broken to the downside)
#
#                  ╲╱         ╲╱
#                   ╲          ╱
#                   │          │  ← candle wicks
#                   │          │
#                   │          │ ← end caps RISE UP from
#                   │          │   below the candle wicks
#                    ───────────  ← spine at candle_ref * 1.5
#                                    below the candle wick
#
# Floating label:
#
#   The label ("BoS↑" / "CHoCH↓" etc.) is drawn as FLOATING TEXT
#   beside the bracket — no bbox box, no rounded-rect background.
#   The text is colored in the event color and has a subtle white
#   outline (drawn via `path_effects.Stroke`) so it stays legible
#   when it overlaps other annotations like trade-marker labels.
#   Labels are not in the legend (legend has FVG/iFVG/mitigation/
#   trade entries only) — the floating text IS the legend for
#   structure events.
y_min, y_max = df_1m['low'].min(), df_1m['high'].max()
y_range = max(y_max - y_min, 0.01)
# Multiplier for the per-candle spine offset. 2026-09-15 polish:
# bumped from 1.5× to 6.0× so the bracket sits well above/below the
# candles (the previous 1.5× put the bracket too close to the price
# action and made it hard to read on busy days). 2026-09-15
# follow-up: bumped further to 10.0× because the 6.0× setting still
# pushed the bracket close to the candle wicks on wide-range bars,
# and the canvas itself was too compressed to give the bracket
# breathing room. 10× + a taller figsize now keeps the bracket well
# clear of the price action.
# 2026-09-15 follow-up #4: reduced to 5.0× because the ylim is now
# tighter (4× candle_wick_max pad instead of 11×) and the canvas
# is taller (28×24 instead of 28×18). 5× × candle wick puts the
# bracket at ~50% of the typical 1m candle range above/below the
# candle — close enough to associate with its candle, far enough
# to not overlap adjacent candles' brackets on busy bars.
OFFSET_MULT = 5.0
# Tiny gap so the cap doesn't actually touch the candle wick.
cap_gap_usd = 0.05
# 2026-09-15 polish: line widths softened — BoS gets a thin hairline
# (the lowest-priority event), CHoCH a medium line, CHoCH+ slightly
# thicker. The bracket itself is now the visual cue; we no longer
# draw a triangle at the break-bar end.
BOS_LINE_WIDTH = 0.9
CHOCH_LINE_WIDTH = 1.6
CHOCHP_LINE_WIDTH = 1.9
n_bos_drawn = 0
n_choch_drawn = 0
n_chochp_drawn = 0
for be in structure.breaks:
    if be.bar >= len(df_1m):
        continue
    x_break = _bar_1m_to_x(be.bar)
    # Swing bar — the bar where the broken level was formed.
    # Falls back to the break bar if the detector didn't record one
    # (older break events without the field).
    swing_bar_idx = int(getattr(be, 'broken_swing_bar', -1) or -1)
    if swing_bar_idx < 0 or swing_bar_idx >= len(df_1m):
        swing_bar_idx = be.bar
    x_swing = _bar_1m_to_x(swing_bar_idx)
    # For readability, if the swing→break span is tiny (a few bars),
    # make sure the bracket is at least ~5 minutes wide so it stays
    # visible.
    MIN_SPAN_DAYS = 5 / (24 * 60)
    if (x_break - x_swing) < MIN_SPAN_DAYS:
        x_left = x_break - MIN_SPAN_DAYS
    else:
        x_left = x_swing
    x_right = x_break
    # Per-candle wick range (high-low of the bar) at both endpoints.
    # 2026-09-15 follow-up #2: now reading from df_1m because
    # structure.breaks is indexed into the 1m bars.
    swing_wick_range = float(
        df_1m['high'].iloc[swing_bar_idx] - df_1m['low'].iloc[swing_bar_idx]
    )
    break_wick_range = float(
        df_1m['high'].iloc[be.bar] - df_1m['low'].iloc[be.bar]
    )
    # Per-candle spine offset — RELATIVE TO THE CANDLE, not the chart.
    candle_ref = max(swing_wick_range, break_wick_range, 0.05)
    spine_offset_usd = candle_ref * OFFSET_MULT
    # Wick at swing bar (high for bull, low for bear) so the cap
    # meets the candle.
    swing_wick = float(
        df_1m['high'].iloc[swing_bar_idx] if be.kind.name in ('BOS_BULL', 'CHOCH_BULL')
        else df_1m['low'].iloc[swing_bar_idx]
    )
    # Wick at break bar.
    break_wick = float(
        df_1m['high'].iloc[be.bar] if be.kind.name in ('BOS_BULL', 'CHOCH_BULL')
        else df_1m['low'].iloc[be.bar]
    )
    is_choch = be.kind.name in ('CHOCH_BULL', 'CHOCH_BEAR')
    is_bull = be.kind.name in ('BOS_BULL', 'CHOCH_BULL')
    is_chochp = be.bar in chochp_bars
    # Color + line width: BoS hairline, CHoCH medium, CHoCH+ slightly
    # thicker; bull green, bear red. CHoCH+ uses a gold tone (the
    # highest-conviction FVG-confirmed reversal).
    if is_chochp:
        color = '#b8860b'        # dark goldenrod — gold but readable
        line_width = CHOCHP_LINE_WIDTH
    elif is_choch:
        color = '#00cc44' if is_bull else '#ff1a1a'
        line_width = CHOCH_LINE_WIDTH
    else:
        color = 'limegreen' if is_bull else 'tomato'
        line_width = BOS_LINE_WIDTH
    z = 4 if (is_choch or is_chochp) else 3
    # Spine y: above the higher of the two candle wicks (bull) /
    # below the lower of the two candle wicks (bear), offset by
    # `spine_offset_usd`. This is RELATIVE TO THE CANDLES, not the
    # chart's y-range.
    if is_bull:
        spine_y = max(swing_wick, break_wick) + spine_offset_usd
        # Cap y: from spine_y DOWN to wick + tiny gap.
        cap_left_y = swing_wick + cap_gap_usd
        cap_right_y = break_wick + cap_gap_usd
    else:
        spine_y = min(swing_wick, break_wick) - spine_offset_usd
        # Cap y: from spine_y UP to wick - tiny gap.
        cap_left_y = swing_wick - cap_gap_usd
        cap_right_y = break_wick - cap_gap_usd
    # Horizontal spine — from swing bar to break bar in the dead space.
    ax.plot([x_left, x_right], [spine_y, spine_y],
            color=color, linewidth=line_width, alpha=0.95,
            solid_capstyle='butt', zorder=z)
    # Left cap (vertical tick at the swing bar): from the spine
    # DOWN (bull) / UP (bear) to the candle wick. This makes the
    # `└` shape on the left end.
    ax.plot([x_left, x_left], [spine_y, cap_left_y],
            color=color, linewidth=line_width, alpha=0.95,
            solid_capstyle='butt', zorder=z)
    # Right cap (vertical tick at the break bar): from the spine
    # DOWN (bull) / UP (bear) to the candle wick. This makes the
    # `┘` shape on the right end.
    ax.plot([x_right, x_right], [spine_y, cap_right_y],
            color=color, linewidth=line_width, alpha=0.95,
            solid_capstyle='butt', zorder=z)
    # 2026-09-15 polish: triangles REMOVED. The break-bar end of
    # the bracket now ends cleanly at the right cap; the floating
    # text label below carries the direction info (`↑` / `↓`).
    # Triangles previously added visual noise on busy days and
    # overlapped with trade entry triangles.
    # FLOATING TEXT LABEL — at the LEFT side of the spine (the swing
    # side), so the eye reads "event_type [swing→break]"
    # left-to-right. No bbox box — just colored text with a thin
    # white halo so it stays legible when it crosses other
    # annotations. The label is the legend for structure events
    # (no legend entries for BoS / CHoCH / CHoCH+).
    if is_chochp:
        label_text = 'CHoCH+↑' if is_bull else 'CHoCH+↓'
    elif is_choch:
        label_text = 'CHoCH↑' if is_bull else 'CHoCH↓'
    else:
        label_text = 'BoS↑' if is_bull else 'BoS↓'
    label_x = x_left
    label_y = spine_y
    label_fontsize = 8 if (is_choch or is_chochp) else 7
    label_weight = 'bold' if (is_choch or is_chochp) else 'normal'
    ax.text(label_x, label_y, label_text,
            color=color, fontsize=label_fontsize,
            fontweight=label_weight, ha='right', va='center',
            zorder=z + 2,
            path_effects=[pe.withStroke(linewidth=2.0, foreground='white',
                                       alpha=0.85)])
    if is_chochp:
        n_chochp_drawn += 1
    elif is_choch:
        n_choch_drawn += 1
    else:
        n_bos_drawn += 1
print(f'Structure markers drawn: BoS={n_bos_drawn}, CHoCH={n_choch_drawn}, CHoCH+={n_chochp_drawn}')

# (SL / TP lines intentionally NOT drawn in this notebook — the chart
# is a visual validator for FVG / iFVG zone lifecycles, not for trade
# management. The trade markers below (entry triangles + exit Xs) are
# enough to eyeball where trades fired and how they exited.)

# Trades — entry triangles + exit markers + connector from entry to
# the FVG zone rectangle that sourced the entry (so the chart
# visually shows "this trade fired because of THIS zone").
#
# 2026-09-15 changes:
#   1. `ENTRY↑` / `ENTRY↓` text labels drawn next to each triangle
#      (bold, high-contrast, color-coded by source type: FVG vs iFVG).
#   2. iFVG-sourced entries get an extra dashed connector from the
#      inversion bar (`fvg_zone.inverted_bar` mapped to chart x)
#      horizontally to the entry bar — visually links "this trade
#      fired because of the iFVG that started at this bar".
#   3. Trade iteration uses ``res.trades`` directly (dataclass list)
#      instead of ``trades_df()`` so the live ``fvg_zone`` reference
#      is preserved (to_dict drops it for JSON-serialisability).
for _, t in trades.iterrows():
    direction = int(t['direction'])
    ep = float(t['entry_price'])
    entry_t = pd.Timestamp(t['entry_time']).tz_localize(None)
    exit_t = pd.Timestamp(t['exit_time']).tz_localize(None)
    triggered_by = str(t.get('entry_triggered_by', ''))
    fvg_zone_obj = t.get('fvg_zone', None)
    is_inv = bool(t.get('is_ifvg', False))
    is_bull_z = (fvg_zone_obj is not None and int(getattr(fvg_zone_obj, 'direction', 0)) > 0)
    # Color scheme for entry markers — primary triangle:
    #   FVG long  → darkgreen
    #   FVG short → darkred
    #   iFVG long → cyan (the now-polarity-flipped color: a bear FVG
    #              that inverted is now a bull iFVG, drawn in green
    #              tones — but we want this to be visually distinct
    #              from a regular FVG long, so cyan/magenta).
    #   iFVG short → magenta
    if is_inv:
        if direction == 1:
            entry_color = 'cyan'
        else:
            entry_color = 'magenta'
    else:
        if direction == 1:
            entry_color = 'darkgreen'
        else:
            entry_color = 'darkred'
    marker = '^' if direction == 1 else 'v'
    ax.scatter(entry_t, ep, marker=marker, color=entry_color,
               s=160, zorder=7, edgecolor='black', linewidth=0.8)

    # If the trade was sourced from an FVG / iFVG zone, draw a
    # dashed connector from the zone's right edge to the entry
    # triangle AND an explicit `ENTRY↑` / `ENTRY↓` text label
    # (the new bold label, 2026-09-15) so entry fires are
    # unmistakable on the chart.
    if fvg_zone_obj is not None and triggered_by in ('fvg', 'ifvg'):
        zl = float(fvg_zone_obj.zone_low)
        zh = float(fvg_zone_obj.zone_high)
        entry_x = mdates.date2num(entry_t)
        zone_edge = 'darkblue' if is_bull_z else 'darkred'
        # Connector: vertical line from the zone edge nearest the
        # entry price down/up to the entry triangle.
        connector_y = zh if abs(ep - zh) < abs(ep - zl) else zl
        ax.plot([entry_x, entry_x], [connector_y, ep],
                color=zone_edge, linewidth=1.2, alpha=0.55,
                linestyle=(0, (2, 2)),
                solid_capstyle='butt', zorder=5.5)
        # NEW (2026-09-15): iFVG→entry connector. When the source
        # zone has inverted=True, draw a horizontal dashed line from
        # the inversion bar (mapped to chart x) to the entry bar.
        # This visually links "this trade fired because of the iFVG
        # that started at this bar" — without it the user has to
        # manually trace from the iFVG rectangle to the entry.
        if is_inv:
            inv_bar = int(getattr(fvg_zone_obj, 'inverted_bar', -1) or -1)
            if inv_bar >= 0 and inv_bar < len(df):
                inv_x = _bar_to_x(inv_bar)
                # Connector y at the entry price (so the line is
                # horizontal — easier to read at chart-glance level).
                # Color matches the iFVG's polarity-flipped color.
                ifvg_connector_color = 'red' if is_bull_z else 'limegreen'
                ax.plot([inv_x, entry_x], [ep, ep],
                        color=ifvg_connector_color, linewidth=1.4,
                        alpha=0.75, linestyle=(0, (5, 3)),
                        solid_capstyle='butt', zorder=5.8)
                # Small "inv" tick at the inversion-bar end so the
                # user can see WHERE on the chart the polarity flip
                # happened (separate from the iFVG rectangle's left
                # edge, which can be visually busy).
                ax.scatter([inv_x], [ep], marker='|', s=80,
                           color=ifvg_connector_color, zorder=5.9,
                           linewidths=2.0)
    er = t['exit_reason']
    if er == 'tp':
        color = 'blue'
    elif er == 'sl':
        color = 'orange'
    elif er == 'inv':
        color = 'purple'  # soft-stop
    elif er == 'eod':
        color = 'gray'
    else:
        color = 'black'
    marker = 'x' if er != 'inv' else 'D'  # diamond for soft-stops
    ax.scatter(exit_t, t['exit_price'], marker=marker, color=color, s=100, zorder=6, linewidths=2)

ax.set_title(f'ICT viz — {VIZ_DATE} {VIZ_SEGMENT_START} ({VIZ_SEGMENT_HOURS}h, '
             f'{(df_1m.index[-1] - df_1m.index[0]).total_seconds()/3600:.1f}h plotted) — '
             f'{len(trades)} trades, '
             f'{n_drawn} zones ({n_superseded_drawn} superseded, {n_played_out_drawn} played out, '
             f'{n_mitigated_drawn} mitigated), {res.n_soft_stops} soft-stops)')
ax.set_ylabel('Price (USD)')
ax.set_xlabel('Time (UTC)')
ax.grid(True, alpha=0.3)
ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'{v:.2f}'))
# 2026-09-15 follow-up #3: 12 ticks instead of 10 because the chart
# is now a 6h slice (not 2 days), so 10 ticks were too sparse.
ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=12))
ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M\n%b %d'))
ax.set_xlim(df_1m.index[0], df_1m.index[-1])
# NOTE: ylim is set AFTER legend so it accounts for the
# candle-relative bracket spine offsets (defined inline below).

legend_handles = [
    # ── FVG zones (live / mitigated / played-out / iFVG) ─────────
    # 2026-09-15 polish: non-inverted FVGs now use green/red
    # (was cyan/magenta). iFVG polarity-flipped colors unchanged.
    mpatches.Patch(facecolor=BULL_FVG_FACE, edgecolor=BULL_FVG_EDGE, alpha=0.22,
                   label='Bull FVG (live)'),
    mpatches.Patch(facecolor=BEAR_FVG_FACE, edgecolor=BEAR_FVG_EDGE, alpha=0.22,
                   label='Bear FVG (live)'),
    mpatches.Patch(facecolor=BULL_FVG_FACE, edgecolor=BULL_FVG_EDGE, alpha=0.05,
                   label='Bull FVG (mitigated ≥50%)'),
    mpatches.Patch(facecolor=BEAR_FVG_FACE, edgecolor=BEAR_FVG_EDGE, alpha=0.05,
                   label='Bear FVG (mitigated ≥50%)'),
    mpatches.Patch(facecolor='wheat', edgecolor='saddlebrown', alpha=0.10, hatch='..',
                   label='FVG played out (direction ran past)'),
    # iFVG — polarity-FLIPPED colors (LuxAlgo parity). The original
    # bull FVG that inverted draws RED; the original bear FVG that
    # inverted draws GREEN.
    mpatches.Patch(facecolor=BULL_IFVG_FACE, edgecolor='darkred', alpha=0.45, hatch='\\',
                   label='iFVG (bull FVG inverted → now bearish)'),
    mpatches.Patch(facecolor=BEAR_IFVG_FACE, edgecolor='darkgreen', alpha=0.45, hatch='\\',
                   label='iFVG (bear FVG inverted → now bullish)'),
    # ── Mitigation dots (2026-09-15 polish: downsized from s=12 to s=7
    # to match the new on-chart marker size; previously the legend
    # swatch was larger than the actual dots).
    plt.Line2D([0], [0], marker='o', color='darkgreen', linestyle='', markersize=7,
               markeredgecolor='white', markeredgewidth=1.0,
               label='Mitigation point (depth ≥50%)'),
    plt.Line2D([0], [0], marker='o', color='orange', linestyle='', markersize=7,
               markeredgecolor='white', markeredgewidth=1.0,
               label='Mitigation point (depth 25-50%)'),
    # ── Trade entries & exits ─────────────────────────────────────
    # 2026-09-15 polish: 4-way split (FVG long/short + iFVG
    # long/short). FVG entries now green/red to match the FVG
    # rectangles; iFVG entries stay cyan/magenta so they're
    # visually distinct from regular FVG entries.
    plt.Line2D([0], [0], color=BULL_FVG_EDGE, marker='^', linestyle='', markersize=12,
               markeredgecolor='black', markeredgewidth=0.8,
               label='Long entry (from FVG)'),
    plt.Line2D([0], [0], color=BEAR_FVG_EDGE, marker='v', linestyle='', markersize=12,
               markeredgecolor='black', markeredgewidth=0.8,
               label='Short entry (from FVG)'),
    plt.Line2D([0], [0], color='cyan', marker='^', linestyle='', markersize=12,
               markeredgecolor='black', markeredgewidth=0.8,
               label='Long entry (from iFVG)'),
    plt.Line2D([0], [0], color='magenta', marker='v', linestyle='', markersize=12,
               markeredgecolor='black', markeredgewidth=0.8,
               label='Short entry (from iFVG)'),
    plt.Line2D([0], [0], color=BULL_IFVG_FACE, linewidth=1.4, alpha=0.75,
               linestyle=(0, (5, 3)),
               label='iFVG → entry connector (bull FVG inverted → short)'),
    plt.Line2D([0], [0], color=BEAR_IFVG_FACE, linewidth=1.4, alpha=0.75,
               linestyle=(0, (5, 3)),
               label='iFVG → entry connector (bear FVG inverted → long)'),
    plt.Line2D([0], [0], color='blue', marker='x', linestyle='', markersize=10,
               markeredgewidth=2, label='TP exit'),
    plt.Line2D([0], [0], color='orange', marker='x', linestyle='', markersize=10,
               markeredgewidth=2, label='SL exit'),
    plt.Line2D([0], [0], color='purple', marker='D', linestyle='', markersize=10,
               label='Soft-stop (FVG inverted)'),
    # NOTE: BoS / CHoCH / CHoCH+ are NOT in the legend — they're
    # drawn as floating text next to each bracket on the chart.
]
ax.legend(handles=legend_handles, loc='upper left', ncol=4, fontsize=7, framealpha=0.92)
# Ylim extends well above/below the chart so per-candle-relative
# bracket spines (offset = candle_wick_range * 10.0, 2026-09-15
# polish — was 1.5× then 6.0×) and trade entry labels never get
# clipped. Bull brackets sit above y_max by up to ~10× the biggest
# candle's wick range; bear brackets sit below y_min by the same.
candle_wick_max = (df_1m['high'] - df_1m['low']).max()
# 2026-09-15 follow-up #4: ylim pad reduced from 11× → 2× candle
# wick range so the candles fill more vertical canvas. The
# per-candle bracket offset is still 5× candle wick, but brackets
# only span the candle's local wick range (not the full y-range),
# so the bracket labels comfortably fit within the tighter pad.
ylim_pad = max(candle_wick_max * 2.0, y_range * 0.05)
ax.set_ylim(df_1m['low'].min() - ylim_pad,
            df_1m['high'].max() + ylim_pad)
fig.subplots_adjust(top=0.93, bottom=0.07, left=0.05, right=0.97)

# 2026-09-02: render INLINE (no PNG file). The notebook now embeds
# the figure directly in the cell output instead of writing
# ``notebooks/nb34_chart.png``. Use ``IPython.display`` so Jupyter
# shows the live figure object — this works in any kernel that has
# IPython (the project's standard kernel). The figure is kept alive
# (no ``plt.close``) so the user can interact with the axes toolbar
# (zoom / pan) without a re-render.
try:
    from IPython.display import display as _ipy_display
    _ipy_display(fig)
except ImportError:
    # Fallback when run as a plain script (no IPython kernel): save to
    # the legacy path so the chart still has a PNG representation.
    out = ROOT / 'notebooks' / 'nb34_chart.png'
    fig.savefig(out, dpi=160)
    print(f'Chart saved (no IPython): {out}')
plt.close(fig)

# %% [markdown]
# ## Inspect the soft-stops
#
# If the backtest reported any soft-stops, list them here so we can
# visually verify they're the trades we'd expect to be closed by an
# FVG inversion.

# %%
soft = trades[trades.exit_reason == 'inv']
if len(soft) == 0:
    print('No soft-stops in this run. Try a longer window or a smaller fvg_min_zone_usd.')
else:
    print(f'{len(soft)} soft-stops:')
    cols = ['entry_time', 'exit_time', 'direction', 'entry_price', 'exit_price',
            'hold_secs', 'layer_idx', 'is_ifvg', 'entry_triggered_by']
    print(soft[cols].to_string(index=False))
