"""nb52_sniper_sl_sweep.py — v14 sniper SL sensitivity sweep notebook.

User directive (2026-09-18):
    "current sniper mode has very tight SLs, it could mean that we get
     stopped out in live, and trades are also very sparse. do a study on
     how much we can increase SL without impacting profits much but also
     give us the insurance in live trading"

Companion to ``src/tools/run_sniper_sl_sweep.py`` (which generated the data
in ``notebooks/sniper_sl_full_*.csv``). This notebook reads the CSV and
presents the headline finding.

Run:
    cd c:/coding/ict_tier_v2
    python notebooks/nb52_sniper_sl_sweep.py
    # or as a notebook:
    uv run jupytext --to notebook nb52_sniper_sl_sweep.py \
      && uv run jupyter nbconvert --to notebook --execute nb52_sniper_sl_sweep.ipynb \
           --output nb52_sniper_sl_sweep.ipynb
"""
# ---
# jupytext: percent
# ---
# %% [markdown]
# # nb52 — v14 sniper SL sensitivity sweep (XAUUSD, N=100)
#
# ## Question
#
# The v7 SNIPER recipe sets ``fvg_inv_trade_sl_zone_mult=1.0`` — SL is
# 1× the iFVG zone width. On 1s XAUUSD that's typically **$0.30-$0.80**,
# roughly the scale of a single 1s bar's noise range. In live trading
# (slippage, spread widening, quote delay), this leaves no headroom — a
# SL that's mathematically tight gets systematically worse in production.
#
# **The question**: how much can we widen the SL multiplier (1× → 1.5× →
# 2× → 3× → 5× → 8×) without hurting PnL/day, AND gain live-trading
# insurance as a side benefit?
#
# ## Method
#
# Driver: ``src/tools/run_sniper_sl_sweep.py 100`` (600 backtests, ~2 min).
# Vary only ``fvg_inv_trade_sl_zone_mult``; everything else at v7 SNIPER
# canonical (TP=22.0, lots=0.01, contract_size=100.0).
#
# Sample: 100 random Mon-Fri UTC days from the 1y XAUUSD corpus, seed
# `20260917` (same as v6/v7/v8/v9/v10).
#
# ## Headline result
#
# | SL mult | PnL/day | EV/trade | t-stat | Pos days | sub-sec SL% | Med hold |
# |---:|---:|---:|---:|---:|---:|---:|
# | 1.0× (current) | +$20.62 | +$2.42 | 4.14 | 63/97 | 2.27% | 42s |
# | 1.5× | +$24.91 | +$2.92 | 4.89 | 71/97 | 1.36% | 75s |
# | 2.0× | +$27.95 | +$3.27 | 5.39 | 76/97 | 0.61% | 109s |
# | 3.0× | +$29.51 | +$3.46 | 5.78 | 78/97 | 0.51% | 183s |
# | **5.0×** | **+$30.91** | **+$3.62** | 5.01 | 76/97 | 0.31% | 342s |
# | 8.0× | +$28.89 | +$3.38 | 4.50 | 71/97 | 0.31% | 615s |
#
# **The v7 SL=1.0× is NOT optimal — it leaves money on the table.**
# Widening to SL=2.0× to 5.0× produces a **+35% to +50% PnL/day
# improvement** at N=100, with **lower sub-second SL rate** and
# **higher positive-day ratio**.
#
# ## Mechanism (exit-reason conversion)
#
# At SL=1.0×, 340 trades hit SL (41% of trades). Widening the SL converts
# many of these into TP wins — at SL=5.0× we freed **111 SL exits** and
# **87 of them became TP wins** (78% conversion). INV exits are stable
# at 333 across all configs (inversions are independent of SL width).
#
# ## Recommendation
#
# **Promote SL=2.0× to canonical.** This is the cleanest point on the
# SL=2-3-5 plateau where:
# - PnL/day is +$27.95 (+35% vs baseline)
# - EV/trade is +$3.27 (+35% vs baseline)
# - sub-sec SL drops from 2.27% → 0.61%
# - positive days goes from 63/97 → 76/97
# - trade count is unchanged (sniper trigger rate is invariant)
#
# SL=3.0× is also acceptable (slightly higher EV, slightly longer hold
# time); SL=5.0× is the peak but has higher daily std (60.74 vs 49.04).
#
# ## Caveats
#
# 1. **N=100 is not N=full corpus.** v11 TP=22 was full-corpus-validated
#    on 654 days; this SL sweep is N=100 only. Recommend a walk-forward
#    validation before promoting to canonical.
# 2. **The wider SL is held longer** — median hold goes 42s → 342s at
#    SL=5.0×. That's an exposure concern for live trading (overnight
#    swap costs NOT modeled in this backtest).
# 3. **The mean/std ratio (Sharpe-like)** peaks at SL=3.0× (5.78), then
#    drops at SL=5.0× and SL=8.0× due to higher daily variance.

# %%
import sys, time
from pathlib import Path

# nbconvert strips __file__, so fall back to walking up from CWD
try:
    _here = Path(__file__).resolve()
except NameError:
    _here = Path.cwd()
# Walk up until we find the repo root (has src/core/ict_signals.py)
ROOT = _here
for _ in range(6):
    if (ROOT / 'src' / 'core' / 'ict_signals.py').is_file():
        break
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

# Force UTF-8 for Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CSV_FULL = ROOT / "notebooks" / "sniper_sl_full_per_day.csv"
CSV_SUMMARY = ROOT / "notebooks" / "sniper_sl_full_summary.csv"
print(f"Reading: {CSV_FULL}")

per_day = pd.read_csv(CSV_FULL)
summary = pd.read_csv(CSV_SUMMARY)
per_day.columns = per_day.columns.str.strip()
summary.columns = summary.columns.str.strip()

print(f"per_day shape: {per_day.shape}")
print(f"summary shape: {summary.shape}")
print()

# %% [markdown]
# # Part 1 — Headline aggregate table

# %%
print("=== Aggregate summary (N=100 days, seed 20260917) ===\n")
display_cols = [
    'config', 'sl_zone_mult', 'n_trades', 'PnL_per_day', 'EV_per_trade',
    't_stat', 'positive_days', 'avg_pct_subsec_sl', 'avg_median_hold_secs',
]
hdr = f"{'Config':<9} {'SL_mult':>8} {'PnL/day':>10} {'EV/trade':>10} {'t-stat':>7} {'Pos_d':>6} {'Pos%':>6} {'Subsec%':>8} {'Med_hold_s':>11}"
print(hdr)
print('-' * len(hdr))
for _, r in summary.iterrows():
    cfg = str(r['config'])
    sm = float(r['sl_zone_mult'])
    pnl_day = float(r['PnL_per_day'])
    ev = float(r['EV_per_trade'])
    t_stat = float(r['t_stat'])
    pos = int(r['positive_days'])
    n_days = int(r['n_days_ok'])
    subsec = float(r['avg_pct_subsec_sl'])
    med_hold = float(r['avg_median_hold_secs'])
    pos_pct = pos / n_days * 100
    print(f"{cfg:<9} {sm:>8.1f} {pnl_day:>+10.4f} {ev:>+10.4f} {t_stat:>7.2f} {pos:>6} {pos_pct:>5.1f}% {subsec:>7.2f}% {med_hold:>11.1f}")

# %% [markdown]
# # Part 2 — Delta vs SL=1.0× baseline

# %%
print("=== Delta vs SL=1.0x baseline (v7 canonical) ===\n")
base = summary[summary['config'] == 'SL_1x'].iloc[0]
base_pnl_day = float(base['PnL_per_day'])
base_ev = float(base['EV_per_trade'])
base_subsec = float(base['avg_pct_subsec_sl'])
print(f"Baseline (SL=1.0x): PnL/day=${base_pnl_day:+.4f}, "
      f"EV/trade=${base_ev:+.4f}, subsec_SL={base_subsec:.2f}%")
print()

for _, r in summary.iterrows():
    cfg = str(r['config'])
    if cfg == 'SL_1x':
        continue
    sm = float(r['sl_zone_mult'])
    d_pnl = float(r['PnL_per_day']) - base_pnl_day
    d_ev = float(r['EV_per_trade']) - base_ev
    d_subsec = float(r['avg_pct_subsec_sl']) - base_subsec
    pct = (float(r['PnL_per_day']) / base_pnl_day) * 100
    print(f"  {cfg:<9} SL={sm:.1f}x   "
          f"delta_PnL/day={d_pnl:+.4f}   "
          f"delta_EV/trade={d_ev:+.4f}   "
          f"delta_subsec={d_subsec:+.2f}%   "
          f"pct_of_base={pct:.1f}%")

# %% [markdown]
# # Part 3 — Exit-reason mechanism

# %%
print("=== Exit-reason conversion: SL → TP wins ===\n")
configs_ordered = ['SL_1x', 'SL_1_5x', 'SL_2x', 'SL_3x', 'SL_5x', 'SL_8x']
sl_mults_ordered = [1.0, 1.5, 2.0, 3.0, 5.0, 8.0]

print(f"{'Config':<9} {'SL_mult':>7} {'SL_exit':>7} {'TP_exit':>7} {'INV_exit':>8} {'EOD_exit':>8} {'SL%':>6} {'TP%':>6}")
print('-' * 70)
base_sl_n, base_tp_n = None, None
for cfg, sm in zip(configs_ordered, sl_mults_ordered):
    sub = per_day[per_day['config'] == cfg]
    n_t = int(sub['n_trades'].sum())
    n_sl = int(sub['n_sl_exits'].sum())
    n_tp = int(sub['n_tp_exits'].sum())
    n_inv = int(sub['n_inv_exits'].sum())
    n_eod = int(sub['n_eod_exits'].sum())
    if cfg == 'SL_1x':
        base_sl_n, base_tp_n = n_sl, n_tp
    sl_pct = n_sl / n_t * 100
    tp_pct = n_tp / n_t * 100
    print(f"{cfg:<9} {sm:>7.1f} {n_sl:>7} {n_tp:>7} {n_inv:>8} {n_eod:>8} {sl_pct:>5.1f}% {tp_pct:>5.1f}%")

print()
print("Mechanism: when SL is widened, previously-SL'd trades SURVIVE the")
print("worse-than-entry bar and either (a) hit TP, or (b) exit at EOD.")
print("INV exits (333) are stable — inversions fire regardless of SL width.")
print()

print("Freed SL -> won TP conversion rate per config:")
for cfg, sm in zip(configs_ordered, sl_mults_ordered):
    if cfg == 'SL_1x':
        continue
    sub = per_day[per_day['config'] == cfg]
    n_sl = int(sub['n_sl_exits'].sum())
    n_tp = int(sub['n_tp_exits'].sum())
    freed = base_sl_n - n_sl
    won = n_tp - base_tp_n
    conv = won / freed * 100 if freed > 0 else 0
    print(f"  SL_{sm:.1f}x: freed {freed} SL exits, {won} became TP wins ({conv:.0f}% conversion)")

# %% [markdown]
# # Part 4 — Risk profile (daily PnL stats)

# %%
print("=== Risk profile: daily PnL distribution ===\n")
print(f"{'Config':<9} {'mean':>9} {'std':>8} {'median':>9} {'min':>10} {'max':>10} {'sharpe_lk':>10}")
print('-' * 70)
for cfg, sm in zip(configs_ordered, sl_mults_ordered):
    sub = per_day[per_day['config'] == cfg]
    sub_ok = sub[sub['n_trades'] > 0]
    daily = sub_ok['pnl_total']
    mean_p = daily.mean()
    std_p = daily.std()
    med_p = daily.median()
    min_p = daily.min()
    max_p = daily.max()
    se = std_p / np.sqrt(len(daily))
    sharpe = mean_p / se if se > 0 else 0
    print(f"{cfg:<9} {mean_p:>+9.2f} {std_p:>8.2f} {med_p:>+9.2f} {min_p:>+10.2f} {max_p:>+10.2f} {sharpe:>10.2f}")

# %% [markdown]
# # Part 5 — Recommendation
#
# **Promote SL=2.0× to canonical** (pending full-corpus walk-forward validation).
#
# Reasoning:
#
# 1. **+35% PnL/day** at N=100 ($20.62 → $27.95) — the v7 SL=1.0× leaves money on the table.
# 2. **+35% EV/trade** ($2.42 → $3.27).
# 3. **Sub-second SL drops from 2.27% → 0.61%** — directly addresses the live-trading "insurance" concern.
# 4. **Positive days: 63 → 76** (out of 97).
# 5. **Trade count unchanged** (828 trades across all configs) — the sniper trigger rate is SL-invariant.
# 6. **Risk profile slightly better** at SL=2.0× — daily std 49 → 51 (tiny), Sharpe-like ratio 4.14 → 5.39.
#
# Why SL=2.0× over SL=3.0×/SL=5.0×:
#
# - SL=2.0× is the **cleanest point** on the 2×-3×-5× plateau (PnL/day within $3 of peak).
# - SL=2.0× has **shorter hold time** (109s median vs 183s/342s) — less overnight exposure risk.
# - SL=2.0× has **lower worst-day** (−$17.84 vs −$34.89 at SL=5.0×) — better tail-risk profile.
# - SL=2.0× is the most **conservative insurance** point — smallest SL change vs baseline.
#
# Why NOT SL=8.0×:
#
# - Peak is at SL=3-5×; SL=8.0× is past the peak (PnL/day drops back).
# - Daily std jumps to 63.17 — 28% higher than baseline.
# - Worst day is −$62.91 (vs baseline −$9.42) — much worse tail risk.
# - Median hold time is 615s (10+ min) — exposure risk.
#
# ## Next steps
#
# 1. **Full-corpus walk-forward validation** (use `run_sniper_full.py` as
#    template) — confirm SL=2.0× holds up on 654 days with train/test split.
# 2. **Live-pipeline test** — if walk-forward confirms, deploy SL=2.0× as
#    the v14 canonical update.
# 3. **SL=3.0× as a backup** — the empirical peak; consider as the next-step
#    upgrade once SL=2.0× is committed.

# %%
print("=== SUMMARY ===\n")
print(f"Baseline (SL=1.0x): PnL/day=${base_pnl_day:+.4f}, EV/trade=${base_ev:+.4f}")
print()
for _, r in summary.iterrows():
    cfg = str(r['config'])
    sm = float(r['sl_zone_mult'])
    pnl_day = float(r['PnL_per_day'])
    ev = float(r['EV_per_trade'])
    delta_pnl = pnl_day - base_pnl_day
    pct = pnl_day / base_pnl_day * 100
    marker = " <-- recommended" if cfg == 'SL_2x' else ""
    marker = " <-- current canonical" if cfg == 'SL_1x' else marker
    print(f"  {cfg:<9} SL={sm:.1f}x: PnL/day=${pnl_day:+.4f} (delta={delta_pnl:+.4f}, {pct:.1f}% of baseline), "
          f"EV/trade=${ev:+.4f}{marker}")

print()
print("RECOMMENDATION: Promote SL=2.0x to canonical (v16) pending full-corpus walk-forward.")
print("                SL=3.0x is the empirical peak; consider for v17 if walk-forward confirms.")
print("                SL=1.0x (current) is sub-optimal — leaves 35% of EV on the table.")