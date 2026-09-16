#!/usr/bin/env python
"""nb39 — investigate the trade-the-D-inversion idea.

The user asked: if D-tier zones ALWAYS invert, can we trade the
OPOSITE direction of the original FVG (because the inverted
level acts as the new resistance/support)?

Method:
  For each D-tier zone, walk the post-inversion price path and
  check whether price:
    a) Returns inside the zone after the inversion_bar
    b) Continues moving away (the inversion was a real level flip)
  Also measure the move after inversion:
    - Price delta in trade-direction (long: low-after-inv; short: high-after-inv)
    - Maximum favorable excursion (MFE) within K seconds
    - Maximum adverse excursion (MAE) within K seconds

If the post-inversion move reliably continues in the
"opposite-of-original-direction" direction, the inverse-D trade
is alpha.

Knobs:
  POST_INV_HORIZONS_SECS : list[int] -- windows to measure over
"""
from __future__ import annotations
import os, sys, time
from pathlib import Path

os.environ.setdefault('MPLBACKEND', 'Agg')
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pds
import pyarrow.parquet as pq

NS_PER_DAY = 86_400_000_000_000

# Knobs
N_DAYS = 200
RNG_SEED = 20260915
MIN_BARS_PER_DAY = 30_000

# Same as nb38
TIER_A_DISPLACEMENT_RATIO = 2.0
TIER_AB_DISPLACEMENT_RATIO = 1.5
TIER_A_MIN_DEPTH = 0.50
TIER_AB_MIN_DEPTH = 0.25
D_PIERCED_AND_INVERTED_MAX_AGE_BARS = 600

# Post-inversion horizons to measure
POST_INV_HORIZONS = [60, 300, 900, 3600]   # 1m, 5m, 15m, 1h

# Repo root
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


# Per-day loop. Only walk D-tier zones through their post-inversion
# price path. This is ~65% of all zones (18432 / 28437), so it's
# the bulk of the data.
rows_post_inv = []
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

    for z in zones:
        if z.trigger_bar < 2 or z.trigger_bar >= n_bars - 1:
            continue
        if not (z.inverted and z.inverted_bar >= 0
                and z.pierced_bar >= 0
                and (z.inverted_bar - z.pierced_bar) <= D_PIERCED_AND_INVERTED_MAX_AGE_BARS):
            continue    # not a D-tier zone
        c1_body = float(body_arr[z.trigger_bar - 2])
        c3_body = float(body_arr[z.trigger_bar])
        c2_body = float(body_arr[z.trigger_bar - 1])
        struct_dir_at = int(trend_per_1s[z.trigger_bar - 1])
        tier = _classify_zone(z, c2_body, c1_body, c3_body, struct_dir_at)
        if tier != 'D':
            continue
        # Walk the post-inversion price path. The trade we want to
        # simulate is "ENTER AT inverted_bar close, DIRECTION =
        # opposite-of-original", target = TP_RULES style on the
        # zone's breadth.
        inv_bar = int(z.inverted_bar)
        if inv_bar >= n_bars - 1:
            continue
        # ANTI-LOOK-AHEAD: enter at bar (inverted_bar + 1) OPEN,
        # NOT at inverted_bar close. The detector's ``inverted_bar``
        # is the bar whose CLOSE confirmed the retest -- we can only
        # act on the NEXT bar's open. Using the inversion-bar close
        # is the same kind of bias as the backtest's
        # ``PendingSignal.trigger_bar + 1`` rule.
        entry_bar = inv_bar + 1
        if entry_bar >= n_bars:
            continue
        entry_price = float(opens[entry_bar])
        trade_dir = -int(z.direction)   # opposite
        zone_width = float(z.zone_high - z.zone_low)
        sl_dist = max(zone_width, 0.10)
        tp_dist = max(zone_width * 1.8, 0.20)
        if trade_dir > 0:
            sl_price = entry_price - sl_dist
            tp_price = entry_price + tp_dist
        else:
            sl_price = entry_price + sl_dist
            tp_price = entry_price - tp_dist

        # Walk bars [entry_bar+1, entry_bar+max_horizon) for SL/TP.
        # NB: the entry_bar's own range can fill the SL/TP intra-bar,
        # but with opens-based entry, only the rest of the bar matters
        # for both directions. We use highs/lows for the rest of the
        # entry bar + all subsequent bars.
        max_horizon = max(POST_INV_HORIZONS)
        end_bar = min(entry_bar + max_horizon, n_bars)
        if end_bar <= entry_bar + 1:
            continue
        post_lows = lows[entry_bar:end_bar]    # includes entry_bar remainder
        post_highs = highs[entry_bar:end_bar]
        # But the entry bar's range *before* our entry is irrelevant.
        # We entered at open; so the worst-case entry-bar high/low
        # is from open forward. Conservatively, use the full bar.
        # Since we're computing outcomes, we accept this.
        if len(post_lows) == 0:
            continue

        hit_sl = False
        hit_tp = False
        hit_idx_sl = -1
        hit_idx_tp = -1
        last_close = float(closes[entry_bar])
        for j_off in range(1, len(post_lows)):
            lo = post_lows[j_off]
            hi = post_highs[j_off]
            if trade_dir > 0:
                if lo <= sl_price and not hit_sl:
                    hit_sl = True
                    hit_idx_sl = j_off
                    break
                if hi >= tp_price and not hit_tp:
                    hit_tp = True
                    hit_idx_tp = j_off
                    break
            else:
                if hi >= sl_price and not hit_sl:
                    hit_sl = True
                    hit_idx_sl = j_off
                    break
                if lo <= tp_price and not hit_tp:
                    hit_tp = True
                    hit_idx_tp = j_off
                    break

        row = {
            'day_id': int(day_id),
            'day_date': str(pd.Timestamp(int(day_id) * NS_PER_DAY,
                                          unit='ns', tz='UTC').date()),
            'direction_orig': int(z.direction),
            'direction_trade': trade_dir,
            'zone_usd': zone_width,
            'mitigated_depth': float(z.mitigated_depth_pct),
            'inv_bar': inv_bar,
            'entry_bar': entry_bar,
            'entry_price': entry_price,
            'sl_dist': sl_dist,
            'tp_dist': tp_dist,
            'sl_price': sl_price,
            'tp_price': tp_price,
            'retest_gap_bars': int(z.inverted_bar - z.pierced_bar),
            'time_to_inv_bars': int(z.inverted_bar - z.trigger_bar),
            'hit_sl': hit_sl,
            'hit_tp': hit_tp,
            'bars_to_sl': hit_idx_sl if hit_sl else -1,
            'bars_to_tp': hit_idx_tp if hit_tp else -1,
        }
        # Per-horizon MFE / MAE / final (close-to-close from entry).
        mfe_per_h = []
        mae_per_h = []
        final_per_h = []
        for h_secs in POST_INV_HORIZONS:
            h_bars = min(h_secs, end_bar - entry_bar - 1)
            if h_bars <= 0:
                mfe_per_h.append(np.nan)
                mae_per_h.append(np.nan)
                final_per_h.append(np.nan)
                continue
            seg_high = float(post_highs[1:h_bars + 1].max())
            seg_low = float(post_lows[1:h_bars + 1].min())
            seg_close = float(closes[entry_bar + h_bars])
            if trade_dir > 0:
                mfe = seg_high - entry_price
                mae = entry_price - seg_low
                final = seg_close - entry_price
            else:
                mfe = entry_price - seg_low
                mae = seg_high - entry_price
                final = entry_price - seg_close
            mfe_per_h.append(mfe)
            mae_per_h.append(mae)
            final_per_h.append(final)
        for i, h_secs in enumerate(POST_INV_HORIZONS):
            row[f'mfe_{h_secs}s'] = mfe_per_h[i]
            row[f'mae_{h_secs}s'] = mae_per_h[i]
            row[f'final_{h_secs}s'] = final_per_h[i]
        rows_post_inv.append(row)

    if (di + 1) % 25 == 0 or (di + 1) == n_sample:
        elapsed = time.time() - t_start
        print(f'  [{di+1:>3d}/{n_sample}] elapsed={elapsed:.1f}s '
              f'rows={len(rows_post_inv):,}', flush=True)

print(f'\nSkipped days: {skipped_days}')
print(f'D-tier rows: {len(rows_post_inv):,}')

df_post = pd.DataFrame(rows_post_inv)

# ── Analysis ──────────────────────────────────────────────────────────────
print('\n=== D-tier inversion-trade outcomes ===')
print(f'{"Horizon":>10s} | {"n":>7s} | {"TP%":>5s} | {"SL%":>5s} | '
      f'{"net USD":>9s} | {"avg MFE":>9s} | {"avg MAE":>9s} | '
      f'{"avg final":>9s}')
print('-' * 100)
# Simple payoff model: $100 per $1 price move per lot (matches the
# backtest's `_close_trade` math).
LOTS = 1
print(f'{"Dir trade":>10s}: long={ (df_post["direction_trade"]==1).sum() }, '
      f'short={ (df_post["direction_trade"]==-1).sum() }')
print()
for i, h_secs in enumerate(POST_INV_HORIZONS):
    h_bars = h_secs
    # Use the "final" column (price move after h_secs in trade direction)
    final_col = f'final_{h_secs}s'
    sub = df_post[final_col].dropna()
    if len(sub) == 0:
        continue
    # We can also estimate the "simple" outcome by assuming we
    # exit at the horizon (close-to-close). That's what `final_*`
    # represents. But it doesn't capture the SL/TP hits within
    # the horizon -- the `hit_sl` / `hit_tp` columns DO, but they
    # use the maximum-horizon walk. For consistency, report
    # BOTH:
    #   (a) "exit at horizon" (close-to-close) -- net USD = mean * lots * 100
    #   (b) "SL/TP/timed-out within max-horizon" -- using hit_sl/hit_tp
    n = len(sub)
    avg_final = sub.mean()
    avg_mfe = df_post[f'mfe_{h_secs}s'].dropna().mean()
    avg_mae = df_post[f'mae_{h_secs}s'].dropna().mean()
    # SL/TP rates: of all D-tier rows, what fraction would have
    # hit SL/TP within the max horizon (3600s)?
    n_sl = int(df_post['hit_sl'].sum())
    n_tp = int(df_post['hit_tp'].sum())
    pct_sl = 100 * n_sl / n
    pct_tp = 100 * n_tp / n
    # Net USD per trade, given the simple 1:1.8 SL:TP model:
    #   if hit_tp: + tp_dist
    #   if hit_sl: - sl_dist
    #   else: net = final_<max>s (close-to-close after max horizon)
    # Use the max horizon for this aggregate.
    max_h = max(POST_INV_HORIZONS)
    final_max = df_post[f'final_{max_h}s'].fillna(0).to_numpy()
    is_tp = df_post['hit_tp'].to_numpy()
    is_sl = df_post['hit_sl'].to_numpy()
    pnl_per_trade = np.where(
        is_tp, df_post['tp_dist'].to_numpy(),
        np.where(is_sl, -df_post['sl_dist'].to_numpy(),
                 final_max)
    )
    avg_pnl_usd = pnl_per_trade.mean() * LOTS * 100
    print(f'{h_secs:>9d}s | {n:>7d} | {pct_tp:>4.0f}% | {pct_sl:>4.0f}% | '
          f'{avg_pnl_usd:>+8.2f}$ | {avg_mfe:>+8.3f} | {avg_mae:>+8.3f} | '
          f'{avg_final:>+8.3f}')

# ── By original direction (split bull-vs-bear inversions) ────────────────
print('\n=== By original FVG direction (post-inversion trade direction) ===')
df_post['inv_dir_label'] = np.where(
    df_post['direction_orig'] == 1,
    'bull_FVG_inv -> short',
    'bear_FVG_inv -> long',
)
max_h = max(POST_INV_HORIZONS)
for label, grp in df_post.groupby('inv_dir_label'):
    n = len(grp)
    final_max = grp[f'final_{max_h}s'].fillna(0).to_numpy()
    pnl_per_trade = np.where(
        grp['hit_tp'].to_numpy(),
        grp['tp_dist'].to_numpy(),
        np.where(grp['hit_sl'].to_numpy(),
                 -grp['sl_dist'].to_numpy(),
                 final_max)
    )
    print(f'  {label}: n={n:>5d}  TP%={grp["hit_tp"].mean()*100:>4.1f}  '
          f'SL%={grp["hit_sl"].mean()*100:>4.1f}  '
          f'avg_pnl=${pnl_per_trade.mean()*LOTS*100:>+7.3f}  '
          f'avg_final_at_{max_h}s={grp[f"final_{max_h}s"].mean():>+6.3f}')

# Save raw data
out_csv = ROOT / 'notebooks' / 'nb39_d_inversion.csv'
df_post.to_csv(out_csv, index=False)
print(f'\nCSV saved: {out_csv}')
print(f'Total runtime: {time.time() - t_start:.1f}s')
