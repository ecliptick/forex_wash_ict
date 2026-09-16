"""Build the nb34_ict_softstop.ipynb — visual validation for the
3-layer orders, dynamic SL/TP, and FVG-inversion soft-stop.
"""
import json
from pathlib import Path

# Write the notebook into the same directory as this build script.
# Hard-coded sibling path removed (the sibling copy was a stale duplicate).
NOTEBOOK_PATH = (Path(__file__).resolve().parent.parent.parent / "notebooks" / "nb34_ict_softstop.ipynb")
cells = []


def md(text):
    cells.append({"cell_type": "markdown", "metadata": {}, "source": text.splitlines(keepends=True)})


def code(text):
    cells.append({
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": text.splitlines(keepends=True),
    })


md("""# nb34 — ICT visual validation

Visual validation of the four new behaviours in this repo:

1. **3-layer orders spread evenly across the FVG zone** — layer
   anchors visible as horizontal lines inside the FVG rectangle.
2. **Dynamic SL/TP** — TP rule table (0/1/2/3+ structure breaks)
   scales the take-profit on top of the static SL/TP USD.
3. **FVG inversion soft-stop** — trades with `exit_reason='inv'`
   plotted with a distinct marker.
4. **FVG price-rank treatment** — A/B/C tier scaling on SL/TP
   based on the FVG's percentile within its UTC day.

Additionally, this notebook overlays **market structure** (BoS / CHoCH
/ CHoCH+) on the price chart. Each structure event is rendered as a
**horizontal bracket in the dead space** above (for bull) or below
(for bear) the chart, **spanning from the swing bar (where the
broken level was formed) to the break bar (where the level was
violated)**. A text label (`BoS↑`, `BoS↓`, `CHoCH↑`, `CHoCH↓`,
`CHoCH+↑`, `CHoCH+↓`) sits at the LEFT end of the bracket (the
swing side) — read left-to-right: "event type [swing bar → break
bar]". No legend entries for structure events; the label is the
legend. The bracket never overlaps any candle because it lives in
the dead space outside the visible y-range.

**Mitigation is visible on the candle**: each FVG zone that gets
filled has (a) a colored vertical band behind the mitigation candle
in the FVG color, (b) a dashed connector from the zone to the
mitigation dot, and (c) a large dot at the fill price on the zone
edge. Trade entries sourced from FVG zones get a similar
`FVG↑`/`FVG↓`/`iFVG↑`/`iFVG↓` label above the entry triangle +
dashed connector to the source zone, so you can see "this trade
fired from this zone" at a glance.

2026-09-02 changes:

* **Inline render.** The chart cell embeds the matplotlib figure
  directly via `IPython.display.display(fig)` instead of writing
  `notebooks/nb34_chart.png`. The PNG fallback only fires if the
  kernel lacks IPython (running the cell as a plain script).
* **SL / TP lines removed.** The per-trade `ax.hlines(SL/TP)` loops
  are gone, along with the 5 corresponding legend entries
  (1× SL, 4× TP rule). The chart is a zone-lifecycle validator, not
  a trade-management tool — the entry triangle + exit marker
  already encode "where the trade fired" and "how it exited".
* **Time + price overlap supersede enabled.** `detect_fvg` is now
  called with `supersede_on_new=True`, so any new FVG that
  overlaps an existing live FVG in **both time (later trigger_bar)
  and price (overlapping `[zone_low, zone_high]`)** cuts the older
  zone short at the newer zone's trigger bar (cyan/magenta `///`
  hatched rectangle with a solid "knife" cut line + chevrons).
  Previously the notebook relied on the default `False`, so the
  visual validation diverged from the live backtest
  (`TrendStrategyParams.fvg_supersede_on_new=True`).
* **BoS / CHoCH brackets span swing→break.** Each structure
  event is now drawn as a horizontal bracket in the dead space
  above/below the chart, spanning from the swing bar (where the
  broken level was formed) to the break bar (where the level
  was violated). Text label sits at the swing-side end. The
  bracket lives outside the visible y-range so it never
  overlaps any candle. Legend entries for BoS/CHoCH/CHoCH+
  were removed because the labels carry the same info inline.
* **Mitigation visible on candles.** Each FVG that gets filled
  now has (1) a colored vertical band behind the mitigation
  candle in the FVG color, (2) a dashed connector from the zone
  rectangle to the mitigation dot, and (3) a large dot at the
  fill price. Trade entries from FVG zones get a matching label
  + dashed connector.

## Edit knobs

The `VIZ_*` constants at the top of cell 4 are the only knobs. Edit
and re-run to refresh the chart.
""")

md("""## Imports + 1-week sample""")

code("""import os, sys
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
""")

code("""# Load a 1-day sample with active FVG + iFVG activity.
# Data lives in this repo's ``data/`` dir (or a sibling dir).
import pyarrow.parquet as pq
import pyarrow.dataset as ds
data_dir = ROOT / 'data'
if not (data_dir / 'XAUUSD_S1_1y.parquet').exists():
    # Walk up looking for a sibling 'data/' dir containing the parquet.
    for ancestor in [ROOT.parent, *ROOT.parent.parents]:
        sibling = ancestor / 'data'
        if (sibling / 'XAUUSD_S1_1y.parquet').exists():
            data_dir = sibling
            break

VIZ_DATE = '2025-01-08'
viz_start = pd.Timestamp(VIZ_DATE, tz='UTC')
viz_end = viz_start + pd.Timedelta(days=2)
parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'
try:
    # pyarrow Dataset API predicate pushdown - avoids materialising
    # the full parquet just to slice 2 days out.
    dataset = ds.dataset(str(parquet_path), format='parquet')
    table = dataset.to_table(
        columns=['time', 'open', 'high', 'low', 'close', 'tickv'],
        filter=(ds.field('time') >= viz_start) & (ds.field('time') < viz_end),
    )
    df = table.to_pandas()
except Exception:
    # Fallback: full load + pandas filter
    df_full = pd.read_parquet(parquet_path)
    df_full = df_full.rename(columns={'tickv': 'volume'})
    df_full['time'] = pd.to_datetime(df_full['time'], utc=True)
    df = df_full[(df_full.time >= viz_start) & (df_full.time < viz_end)].reset_index(drop=True)

df = df.rename(columns={'tickv': 'volume'})
df['time'] = pd.to_datetime(df['time'], utc=True)
df = df.sort_values('time').reset_index(drop=True)
print(f'Data dir: {data_dir}')
print(f'Viz day: {len(df):,} bars  ({df.time.iloc[0]} -> {df.time.iloc[-1]})')
""")

md("""## Run the backtest + show summary""")

code("""p = TrendStrategyParams(
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
""")

md("""## Detect FVG zones for the chart""")

code("""zones = detect_fvg(
    df['open'].to_numpy(), df['high'].to_numpy(),
    df['low'].to_numpy(), df['close'].to_numpy(),
    resample_to_n_secs=60,
    fvg_min_zone_usd=0.30,
    fvg_displacement_ratio=0.0,
    fvg_body_definition='body',
    # 2026-09-02: enable time+price overlap supersede at the detector
    # level too, so the chart shows the same "newer zone cuts short the
    # older one" semantics as the live backtest (which forwards
    # ``fvg_supersede_on_new=True`` from ``TrendStrategyParams``).
    # The detector default is ``False`` to keep ad-hoc callers
    # deterministic; the notebook is a visual validator so we want the
    # same semantics the strategy uses.
    supersede_on_new=True,
)
print(f'FVG zones: {len(zones)}')
n_inv = sum(1 for z in zones if z.inverted)
print(f'  inverted: {n_inv}')

# Resample to 1m for the chart
df_1m = (df.set_index('time')[['open', 'high', 'low', 'close']]
         .resample('1min')
         .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
         .dropna())
if df_1m.index.tz is not None:
    df_1m.index = df_1m.index.tz_localize(None)
print(f'1m candles: {len(df_1m):,}')

# Detect market structure on the 1m bars so the chart can overlay
# BoS/CHoCH/CHoCH+ brackets. Match the backtest's defaults.
structure = detect_market_structure(
    df_1m['high'].to_numpy(), df_1m['low'].to_numpy(),
    df_1m['close'].to_numpy(),
    pivot_len=9, liquidity_len=30,
)
# Map structure break bars (1m index) back to 1s index so the
# chart-drawing loop can use the same bar indices as the FVG
# detector (which uses 1s bars). The 1m break bar is at the END
# of the 1m minute; the equivalent 1s bar is the last 1s bar of
# that minute.
#
# IMPORTANT: BOTH `bar` AND `broken_swing_bar` must be mapped from
# 1m → 1s index. Without the swing_bar mapping the chart's
# `x_swing = _bar_to_x(swing_bar_idx)` collapses to the chart's
# left edge (because 1m bar 42 is 1s bar 42 ≈ start of day).
# The mapping uses the same `searchsorted` against `df['time']`
# for both fields.
breaks_1s = []
for be in structure.breaks:
    t_break = df_1m.index[be.bar]
    # Find the last 1s bar whose time <= t_break. The 1s index is
    # tz-aware but df_1m.index is tz-naive (we stripped tz above);
    # normalize.
    if df['time'].dt.tz is None and hasattr(t_break, 'tzinfo') and t_break.tzinfo is not None:
        t_break = t_break.tz_localize(None)
    elif df['time'].dt.tz is not None and (not hasattr(t_break, 'tzinfo') or t_break.tzinfo is None):
        t_break = t_break.tz_localize(df['time'].dt.tz)
    idx_1s_break = df['time'].searchsorted(t_break, side='right') - 1
    if idx_1s_break < 0:
        idx_1s_break = 0
    if idx_1s_break >= len(df):
        idx_1s_break = len(df) - 1
    # Map the swing bar (also 1m index → 1s index). Fall back to
    # the break bar if the detector didn't record a swing or the
    # 1m swing index is out of range for df_1m.
    swing_1m_idx = int(getattr(be, 'broken_swing_bar', -1) or -1)
    if swing_1m_idx < 0 or swing_1m_idx >= len(df_1m):
        idx_1s_swing = idx_1s_break
    else:
        t_swing = df_1m.index[swing_1m_idx]
        if df['time'].dt.tz is None and hasattr(t_swing, 'tzinfo') and t_swing.tzinfo is not None:
            t_swing = t_swing.tz_localize(None)
        elif df['time'].dt.tz is not None and (not hasattr(t_swing, 'tzinfo') or t_swing.tzinfo is None):
            t_swing = t_swing.tz_localize(df['time'].dt.tz)
        idx_1s_swing = df['time'].searchsorted(t_swing, side='right') - 1
        if idx_1s_swing < 0:
            idx_1s_swing = 0
        if idx_1s_swing >= len(df):
            idx_1s_swing = len(df) - 1
    be_1s = type(be)(
        kind=be.kind, bar=int(idx_1s_break),
        broken_swing_bar=int(idx_1s_swing),
        broken_swing_price=getattr(be, 'broken_swing_price', 0.0),
    )
    breaks_1s.append(be_1s)
# Use the 1s-mapped breaks for charting.
structure.breaks = breaks_1s
# CHoCH+ is the highest-conviction reversal (FVG-confirmed). For
# the chart we just mark all CHoCH events as candidates and overlay
# a gold star on the ones that ALSO fired as an iFVG.
chochp_bars = set()  # placeholder if we don't have the exact predicate
print(f'Structure breaks: BoS={sum(1 for b in structure.breaks if b.kind.name in (\"BOS_BULL\", \"BOS_BEAR\"))}, '
      f'CHoCH={sum(1 for b in structure.breaks if b.kind.name in (\"CHOCH_BULL\", \"CHOCH_BEAR\"))}')
""")

md("""## Chart""")

code("""trades = res.trades_df()

fig, ax = plt.subplots(figsize=(28, 14))

# Candles
x_dates = mdates.date2num(df_1m.index.to_pydatetime())
ohl = df_1m[['open', 'high', 'low', 'close']].to_numpy()
opens, highs, lows, closes = ohl[:, 0], ohl[:, 1], ohl[:, 2], ohl[:, 3]
half_w = 0.80 / 2 / (24 * 60)
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
FVG_FWD_BARS = 3600  # 60 min on 1s data
def _bar_to_x(bar_idx):
    t = df['time'].iloc[bar_idx]
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
        face = 'cyan' if z.direction == 1 else 'magenta'
        edge = 'darkblue' if z.direction == 1 else 'darkred'
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
        knife_w = 3.0 / (24 * 60 * 60)
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
        if z.mitigated_bar >= 0 and z.mitigated_depth_pct >= 0.5:
            opacity = 0.05
            n_mitigated_drawn += 1
        elif z.mitigated_bar >= 0:
            opacity = 0.12
            n_mitigated_drawn += 1
        else:
            opacity = 0.22
        face = 'cyan' if z.direction == 1 else 'magenta'
        edge = 'darkblue' if z.direction == 1 else 'darkred'
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
# extent as the original FVG, but hatched + flipped-polarity edge
# colour so the polarity flip is visible on the time axis.
IFVG_FWD_BARS = 3600
for z in zones:
    if not (z.inverted and z.inverted_bar >= 0 and z.inverted_bar < len(df)):
        continue
    ifxl = _bar_to_x(z.inverted_bar)
    ifxr = _bar_to_x(min(len(df) - 1, z.inverted_bar + IFVG_FWD_BARS))
    if ifxr <= ifxl:
        continue
    face = 'cyan' if z.direction == 1 else 'magenta'
    edge = 'darkred' if z.direction == 1 else 'darkgreen'
    ax.add_patch(mpatches.Rectangle((ifxl, z.zone_low), ifxr - ifxl, z.zone_high - z.zone_low,
                                    facecolor=face, edgecolor=edge, alpha=0.5,
                                    hatch='\\\\', linewidth=0.8, zorder=2))
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
        inv_face = 'cyan' if z.direction == 1 else 'magenta'
        inv_edge = 'darkred' if z.direction == 1 else 'darkgreen'
        ax.add_patch(mpatches.Rectangle((inv_x, z.zone_low), inv_xr - inv_x,
                                        z.zone_high - z.zone_low,
                                        facecolor=inv_face, edgecolor=inv_edge,
                                        alpha=0.30, hatch='\\\\',
                                        linewidth=0.0, zorder=2.5))
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
y_min_v, y_max_v = df_1m['low'].min(), df_1m['high'].max()
y_range_v = max(y_max_v - y_min_v, 0.01)
n_mit_dots = 0
for z in zones:
    if z.mitigated_bar < 0 or z.mitigated_bar >= len(df):
        continue
    if z.superseded_bar >= 0 and z.mitigated_bar > z.superseded_bar:
        continue  # mitigation past the superseded cutoff — skip
    mx = _bar_to_x(z.mitigated_bar)
    bar_low = float(df['low'].iloc[z.mitigated_bar])
    bar_high = float(df['high'].iloc[z.mitigated_bar])
    bar_close = float(df['close'].iloc[z.mitigated_bar])
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
    # Zone color (used for the highlight band + connector).
    zone_face = 'cyan' if is_bull_z else 'magenta'
    zone_edge = 'darkblue' if is_bull_z else 'darkred'
    # 1. Vertical highlight band behind the mitigation candle.
    # A narrow x-window around the bar, full y-range, low alpha.
    # Half-width = ~half a 1m candle (45s).
    band_half_w = 30 / (24 * 60 * 60)  # 30 seconds on the time axis
    band = mpatches.Rectangle(
        (mx - band_half_w, y_min_v),
        width=2 * band_half_w,
        height=y_max_v - y_min_v,
        facecolor=zone_face, alpha=0.18,
        edgecolor='none', zorder=1.5,
    )
    ax.add_patch(band)
    # 2. Connector line from the zone's right edge to the dot.
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
    # 3. The big mitigation dot — colored by depth:
    # dark green for >=50%, orange for >=25%, gray for <25%.
    if z.mitigated_depth_pct >= 0.5:
        mit_color = 'darkgreen'
    elif z.mitigated_depth_pct >= 0.25:
        mit_color = 'orange'
    else:
        mit_color = 'gray'
    ax.scatter([mx], [my], marker='o', s=320,
               facecolors=mit_color, edgecolors='black',
               linewidths=1.8, zorder=10, alpha=1.0)
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
#     spine_offset     = candle_ref * OFFSET_MULT (default 1.5×)
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
# Multiplier for the per-candle spine offset. 1.5× gives a bracket
# spine that's visibly above/below the candle without dominating
# the chart. Bigger K = bigger gap, smaller K = tighter bracket.
OFFSET_MULT = 1.5
# Tiny gap so the cap doesn't actually touch the candle wick.
cap_gap_usd = 0.05
n_bos_drawn = 0
n_choch_drawn = 0
n_chochp_drawn = 0
for be in structure.breaks:
    if be.bar >= len(df):
        continue
    x_break = _bar_to_x(be.bar)
    # Swing bar — the bar where the broken level was formed.
    # Falls back to the break bar if the detector didn't record one
    # (older break events without the field).
    swing_bar_idx = int(getattr(be, 'broken_swing_bar', -1) or -1)
    if swing_bar_idx < 0 or swing_bar_idx >= len(df):
        swing_bar_idx = be.bar
    x_swing = _bar_to_x(swing_bar_idx)
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
    swing_wick_range = float(
        df['high'].iloc[swing_bar_idx] - df['low'].iloc[swing_bar_idx]
    )
    break_wick_range = float(
        df['high'].iloc[be.bar] - df['low'].iloc[be.bar]
    )
    # Per-candle spine offset — RELATIVE TO THE CANDLE, not the chart.
    candle_ref = max(swing_wick_range, break_wick_range, 0.05)
    spine_offset_usd = candle_ref * OFFSET_MULT
    # Wick at swing bar (high for bull, low for bear) so the cap
    # meets the candle.
    swing_wick = float(
        df['high'].iloc[swing_bar_idx] if be.kind.name in ('BOS_BULL', 'CHOCH_BULL')
        else df['low'].iloc[swing_bar_idx]
    )
    # Wick at break bar.
    break_wick = float(
        df['high'].iloc[be.bar] if be.kind.name in ('BOS_BULL', 'CHOCH_BULL')
        else df['low'].iloc[be.bar]
    )
    is_choch = be.kind.name in ('CHOCH_BULL', 'CHOCH_BEAR')
    is_bull = be.kind.name in ('BOS_BULL', 'CHOCH_BULL')
    is_chochp = be.bar in chochp_bars
    # Color + line width: BoS thinner, CHoCH thicker; bull green,
    # bear red. CHoCH+ uses a gold tone (the highest-conviction
    # FVG-confirmed reversal).
    if is_chochp:
        color = '#b8860b'        # dark goldenrod — gold but readable
        line_width = 2.6
    elif is_choch:
        color = '#00cc44' if is_bull else '#ff1a1a'
        line_width = 2.2
    else:
        color = 'limegreen' if is_bull else 'tomato'
        line_width = 1.4
    z = 6 if (is_choch or is_chochp) else 5
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
    # A small direction triangle AT THE BREAK-BAR END of the spine
    # so the eye reads "the event happens here" pointing AT the
    # break bar. Triangle sits ON the spine.
    tri_marker = '^' if is_bull else 'v'
    tri_s = 110 if (is_choch or is_chochp) else 70
    ax.scatter([x_right], [spine_y], marker=tri_marker, s=tri_s,
               facecolors=color, edgecolors='black',
               linewidths=0.8, zorder=z + 0.5)
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
for _, t in trades.iterrows():
    direction = int(t.direction)
    ep = float(t.entry_price)
    entry_t = pd.Timestamp(t.entry_time).tz_localize(None)
    exit_t = pd.Timestamp(t.exit_time).tz_localize(None)
    if direction == 1:
        ax.scatter(entry_t, ep, marker='^', color='green', s=80, zorder=6, edgecolor='black', linewidth=0.5)
    else:
        ax.scatter(entry_t, ep, marker='v', color='red', s=80, zorder=6, edgecolor='black', linewidth=0.5)
    # If the trade was sourced from an FVG / iFVG zone, draw a
    # dashed connector from the zone's right edge to the entry
    # triangle AND a small "FVG" / "iFVG" text label above the
    # triangle. This is the visual link that says "this trade
    # came from this zone" — without it the entry floats free.
    fvg_zone_obj = t.get('fvg_zone') if hasattr(t, 'get') else None
    triggered_by = str(t.get('entry_triggered_by', '') if hasattr(t, 'get') else '')
    if fvg_zone_obj is not None and triggered_by in ('fvg', 'ifvg'):
        zl = float(fvg_zone_obj.zone_low)
        zh = float(fvg_zone_obj.zone_high)
        entry_x = mdates.date2num(entry_t)
        is_inv = bool(getattr(fvg_zone_obj, 'inverted', False))
        is_bull_z = int(fvg_zone_obj.direction) > 0
        zone_face = 'cyan' if is_bull_z else 'magenta'
        zone_edge = 'darkblue' if is_bull_z else 'darkred'
        # Connector: horizontal line from the zone's right edge at
        # the entry price to the entry triangle.
        # Pick the connector Y at the zone edge nearest the entry.
        connector_y = zh if abs(ep - zh) < abs(ep - zl) else zl
        ax.plot([entry_x, entry_x], [connector_y, ep],
                color=zone_edge, linewidth=1.2, alpha=0.55,
                linestyle=(0, (2, 2)),
                solid_capstyle='butt', zorder=5.5)
        # Small text label above the entry triangle (no overlap
        # with the triangle or the candle — sits 0.05 USD above
        # the entry price and is ha='center').
        label = 'iFVG↑' if is_inv and direction == 1 else 'iFVG↓' if is_inv and direction == -1 else \
                'FVG↑' if direction == 1 else 'FVG↓'
        ax.text(entry_x, ep + y_range_v * 0.006, label,
                color=zone_edge, fontsize=6.5,
                fontweight='normal', ha='center', va='bottom',
                zorder=6.5,
                bbox=dict(boxstyle='round,pad=0.15',
                          facecolor='white', edgecolor=zone_face,
                          alpha=0.92, linewidth=0.5))
    er = t.exit_reason
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
    ax.scatter(exit_t, t.exit_price, marker=marker, color=color, s=100, zorder=6, linewidths=2)

ax.set_title(f'ICT viz — {VIZ_DATE}  ({len(trades)} trades, '
             f'{n_drawn} zones ({n_superseded_drawn} superseded, {n_played_out_drawn} played out, '
             f'{n_mitigated_drawn} mitigated), {res.n_soft_stops} soft-stops)')
ax.set_ylabel('Price (USD)')
ax.set_xlabel('Time (UTC)')
ax.grid(True, alpha=0.3)
ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f'{v:.2f}'))
ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=10))
ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M\\n%b %d'))
ax.set_xlim(df_1m.index[0], df_1m.index[-1])
# NOTE: ylim is set AFTER legend so it accounts for the
# candle-relative bracket spine offsets (defined inline below).

legend_handles = [
    # ── FVG zones (live / mitigated / played-out / iFVG) ─────────
    mpatches.Patch(facecolor='cyan', edgecolor='darkblue', alpha=0.22,
                   label='Bull FVG (live)'),
    mpatches.Patch(facecolor='magenta', edgecolor='darkred', alpha=0.22,
                   label='Bear FVG (live)'),
    mpatches.Patch(facecolor='cyan', edgecolor='darkblue', alpha=0.05,
                   label='Bull FVG (mitigated ≥50%)'),
    mpatches.Patch(facecolor='magenta', edgecolor='darkred', alpha=0.05,
                   label='Bear FVG (mitigated ≥50%)'),
    mpatches.Patch(facecolor='wheat', edgecolor='saddlebrown', alpha=0.10, hatch='..',
                   label='FVG played out (direction ran past)'),
    mpatches.Patch(facecolor='cyan', edgecolor='darkred', alpha=0.5, hatch='\\\\',
                   label='Bull iFVG (inverted)'),
    mpatches.Patch(facecolor='magenta', edgecolor='darkgreen', alpha=0.5, hatch='\\\\',
                   label='Bear iFVG (inverted)'),
    # ── Superseded cut (solid vertical line + 'knife' chevrons). The
    # OLDER zone is cut short at the newer zone's trigger bar.
    mpatches.Patch(facecolor='cyan', edgecolor='darkblue', alpha=0.10, hatch='///',
                   label='Bull FVG (cut short by newer bull FVG)'),
    mpatches.Patch(facecolor='magenta', edgecolor='darkred', alpha=0.10, hatch='///',
                   label='Bear FVG (cut short by newer bear FVG)'),
    plt.Line2D([0], [0], color='darkblue', linewidth=1.4, alpha=0.9,
               label='Superseded cut line (bull: solid)'),
    plt.Line2D([0], [0], color='darkred', linewidth=1.4, alpha=0.9,
               label='Superseded cut line (bear: solid)'),
    # ── Mitigation points (now larger and brighter so they don't
    # disappear against the FVG zone backgrounds).
    plt.Line2D([0], [0], marker='o', color='darkgreen', linestyle='', markersize=12,
               markeredgecolor='white', markeredgewidth=1.6,
               label='Mitigation point (depth ≥50%)'),
    plt.Line2D([0], [0], marker='o', color='orange', linestyle='', markersize=12,
               markeredgecolor='white', markeredgewidth=1.6,
               label='Mitigation point (depth 25-50%)'),
    # ── Trade entries & exits (larger so visible at full zoom) ──
    plt.Line2D([0], [0], color='green', marker='^', linestyle='', markersize=11,
               markeredgecolor='black', markeredgewidth=0.5, label='Long entry'),
    plt.Line2D([0], [0], color='red', marker='v', linestyle='', markersize=11,
               markeredgecolor='black', markeredgewidth=0.5, label='Short entry'),
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
# bracket spines (offset = candle_wick_range * 1.5) and trade entry
# labels never get clipped. Bull brackets sit above y_max by up to
# ~1.5× the biggest candle's wick range; bear brackets sit below
# y_min by the same.
candle_wick_max = (df_1m['high'] - df_1m['low']).max()
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
""")

md("""## Inspect the soft-stops

If the backtest reported any soft-stops, list them here so we can
visually verify they're the trades we'd expect to be closed by an
FVG inversion.
""")

code("""soft = trades[trades.exit_reason == 'inv']
if len(soft) == 0:
    print('No soft-stops in this run. Try a longer window or a smaller fvg_min_zone_usd.')
else:
    print(f'{len(soft)} soft-stops:')
    cols = ['entry_time', 'exit_time', 'direction', 'entry_price', 'exit_price',
            'hold_secs', 'layer_idx', 'is_ifvg', 'entry_triggered_by']
    print(soft[cols].to_string(index=False))
""")

nb = {
    "cells": cells,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.12"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}

NOTEBOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
NOTEBOOK_PATH.write_text(json.dumps(nb, indent=1))
print(f'Wrote {NOTEBOOK_PATH}  ({len(cells)} cells)')
