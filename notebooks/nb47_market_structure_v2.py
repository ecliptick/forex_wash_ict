# ---
# jupyter:
#   jupytext:
#     formats: py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
# ---
# %% [markdown]
# # nb47 — BoS/CHoCH conviction tuning on SNIPER base (v10)
#
# **User directive (2026-09-17):** "is market structure BoS and
# CHoCH doing anything to the strat? can we optimize this to be
# more of an edge?"
#
# The v6 BoS/CHoCH A/B (`run_boschoch_ab.py`, N=59) found that
# conviction knobs produce identical PnL on the immediate-mode
# baseline — `ms_min_conviction=0.0` and `ms_boost_conviction=1.0`
# mean conviction is "computed but unused". But that was on v6.
# The v7 SNIPER mode has 8.3 trades/day (vs ~64/day immediate),
# so each signal has higher EV potential and conviction filtering
# is *more* likely to surface a real edge.
#
# v10 closes this question. **And discovers `fvg_invalidate_on_structure`
# is a NEW ALPHA SOURCE** that was completely untested (Tier-3
# backlog row #18).
#
# ## Design
#
# 8 single-knob configs on the v7 SNIPER base:
#
# | # | Config | Knob flipped |
# |---|---|---|
# | 1 | SNIPER_BASE | (v7 defaults) |
# | 2 | MS_OFF | `use_market_structure=False` |
# | 3 | CONV_05 | `ms_min_conviction=0.5` |
# | 4 | CONV_07 | `ms_min_conviction=0.7` |
# | 5 | BOOST_15 | `ms_boost_conviction=1.5` |
# | 6 | BOS_ALIGN_OFF | `bos_choch_ignore_invert_when_aligned=False` |
# | 7 | FVG_INV_STRUCT_ON | `fvg_invalidate_on_structure=True` (default 300s) |
# | 8 | FVG_INV_STRUCT_1800 | `fvg_invalidate_on_structure=True, age=1800` |
#
# ## Headline finding
#
# **`FVG_INV_STRUCT_1800` is the winner** — kills live FVGs when
# an opposing BoS/CHoCH event fires within 30 min of zone formation.
# On N=100:
# * PnL/day: **+$21.25** vs SNIPER_BASE +$20.42 (+$0.82/day, +4.0%)
# * Trade count: 656 vs 828 (172 fewer trades/day on aggregate)
# * Per-day t-stat: **+3.92** (highly significant)
# * Days won: **70/100** vs days lost: 5/100 (25 tied)
# * Min PnL/day improved: −$18.19 → −$14.64 (less extreme losses)
#
# **Mechanism**: when an opposing BoS/CHoCH fires within 30 min of
# an FVG being formed, the FVG's structural thesis is rejected. The
# filter prevents the strategy from entering those doomed setups.
# The 30-min window is wide enough to catch the typical "thesis-flip"
# pattern but narrow enough to avoid over-filtering on stale zones.

# %% [markdown]
# ## Edit knobs

# %%
N_DAYS = 100
RNG_SEED = 20260917  # same as v8/v9 for comparability
MIN_BARS_PER_DAY = 30_000

import subprocess

# %% [markdown]
# ## Locate repo + verify tool exists

# %%
import os
import sys
from pathlib import Path

os.environ.setdefault('MPLBACKEND', 'Agg')


def _find_root() -> Path:
    here = Path('.').resolve()
    candidates = [p for p in [here, *here.parents]
                  if (p / 'src' / 'core' / 'ict_signals.py').is_file()]
    if not candidates:
        raise RuntimeError('Could not find ICT repo root')

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
print(f'Repo root: {ROOT}')

tool_path = ROOT / 'src' / 'tools' / 'run_market_structure_v2.py'
assert tool_path.is_file(), f'missing: {tool_path}'
print(f'Tool: {tool_path}')

# %% [markdown]
# ## Run v10 sweep — 100 random Mon-Fri UTC days

# %%
print(f'\n>>> V10 SWEEP: {N_DAYS} days <<<\n')
res = subprocess.run(
    [sys.executable, str(tool_path), str(N_DAYS)],
    cwd=str(ROOT),
    capture_output=True,
    text=True,
)
print(res.stdout)
if res.returncode != 0:
    print('STDERR:', res.stderr)

# %% [markdown]
# ## Inspect results

# %%
import pandas as pd

summary_path = ROOT / 'notebooks' / 'market_structure_v2_summary.csv'
per_day_path = ROOT / 'notebooks' / 'market_structure_v2_per_day.csv'

summary = pd.read_csv(summary_path)
per_day = pd.read_csv(per_day_path)

print(f'V10 summary: {summary.shape[0]} configs, '
      f'{len(per_day["day"].unique())} days, '
      f'{int(summary["trades"].sum()):,} trades total')

print('\nConfigs ranked by EV/trade:')
top = summary.sort_values('ev_per_trade', ascending=False)
print(top[['config', 'trades', 'ev_per_trade', 'pnl_per_day', 'pnl_per_day_t']]
      .to_string(index=False))

# %% [markdown]
# ## Per-day PnL distribution analysis

# %%
print('Per-day PnL distribution by config (sorted by mean PnL/day):')
print('=' * 100)
config_by_mean = sorted(
    summary['config'].unique(),
    key=lambda c: summary.loc[summary['config'] == c, 'pnl_per_day'].iloc[0],
    reverse=True,
)
for cfg_name in config_by_mean:
    row = summary[summary['config'] == cfg_name].iloc[0]
    print(f'{cfg_name:<24}  '
          f'mean=${row["pnl_per_day"]:>+7.2f}/d  '
          f'se=${row["pnl_per_day_se"]:>5.2f}  '
          f't={row["pnl_per_day_t"]:+5.2f}  '
          f'pos_days={int(row["pct_pos_days"])}/100')

# %% [markdown]
# ## The FVG_INV_STRUCT_1800 win — detailed analysis

# %%
# Compare SNIPER_BASE vs FVG_INV_STRUCT_1800 day-by-day
ref = per_day[per_day['config'] == 'SNIPER_BASE'].set_index('day')
new = per_day[per_day['config'] == 'FVG_INV_STRUCT_1800'].set_index('day')

merged = pd.DataFrame({
    'base_fills': ref['n_fills'],
    'struct_fills': new['n_fills'],
    'base_pnl': ref['pnl_total'],
    'struct_pnl': new['pnl_total'],
})
merged['delta_fills'] = merged['struct_fills'] - merged['base_fills']
merged['delta_pnl'] = merged['struct_pnl'] - merged['base_pnl']

print('Comparison: SNIPER_BASE vs FVG_INV_STRUCT_1800 (day-by-day, N=100)')
print('=' * 80)
print(f'Days where struct-version had FEWER fills: {(merged["delta_fills"] < 0).sum()}/100')
print(f'Days where struct-version had EQUAL fills: {(merged["delta_fills"] == 0).sum()}/100')
print(f'Days where struct-version had MORE fills:  {(merged["delta_fills"] > 0).sum()}/100')
print()
filtered = merged[merged['delta_fills'] < 0]
print(f'On days with reduced fills (n={len(filtered)}):')
print(f'  mean fills removed: {-filtered["delta_fills"].mean():.2f}/d')
print(f'  mean PnL change:    ${filtered["delta_pnl"].mean():+.2f}/d')
print(f'  wins: {(filtered["delta_pnl"] > 0).sum()}/{len(filtered)}  '
      f'losses: {(filtered["delta_pnl"] < 0).sum()}/{len(filtered)}')

total_removed = -filtered['delta_fills'].sum()
total_pnl_change = filtered['delta_pnl'].sum()
if total_removed > 0:
    print(f'\nNet PnL impact per filtered trade: '
          f'${total_pnl_change / total_removed:+.4f}/trade')
    print('  (positive = removing the trade was a net win — '
          'the removed trades were net-negative on average)')

# %% [markdown]
# ## Headline answers
#
# **Q1: is BoS/CHoCH doing ANYTHING on the sniper path?**
# A: mostly no — `MS_OFF`, `CONV_05`, `BOOST_15` are bit-for-bit
# identical to SNIPER_BASE. The conviction classifier is "computed
# but unused" on the sniper path too (because most sniper entries
# happen AT a CHoCH event, so conviction is usually 1.0 or similar
# neutral).
#
# **Q2: does alignment-aware soft-stop suppression help?**
# A: NO on sniper mode — `BOS_ALIGN_OFF` produces identical PnL
# to SNIPER_BASE (-$0.01/day, noise). The v8 finding that
# alignment-suppression matters was on IMMEDIATE mode where soft-
# stops are frequent. On sniper mode the soft-stop fires much less
# often, so the suppression rule doesn't move the needle.
#
# **Q3: does `fvg_invalidate_on_structure` help?**
# A: **YES** — but only with a 30-min window (NOT the default 5 min).
# `fvg_invalidate_on_structure=True, age=1800s` improves PnL/day
# by +$0.82 (t=3.92, highly significant). The 5-min default
# is too aggressive — kills 21 zones that turned out to be
# profitable.
#
# **Q4: does CONV_07 (drop signals with conviction < 0.7) help?**
# A: SLIGHTLY NEGATIVE (-$0.21/day, t=-0.04). The conviction
# threshold is dropping real signals without finding alpha.
#
# **Decision**: **do NOT promote to canonical yet** — N=100 only.
# Need full-corpus walk-forward (654 days) to confirm the +$0.82/day
# delta holds across regime changes. The v9 SNIPER_TP22 baseline
# is +$43.62/day on the 2026 holdout; v10 OPTIMAL would need to
# beat that to be promoted.
#
# **Recommended next experiment (v11)**: full-corpus walk-forward
# on `fvg_invalidate_on_structure=True, age=1800s` stacked with
# the v9 SNIPER base. Use `run_sniper_full.py` as a template.
