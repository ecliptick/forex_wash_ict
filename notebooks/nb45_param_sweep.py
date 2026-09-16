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
# # nb45 — Top-10 parameter sweep on 100 random Mon-Fri UTC days
#
# **User directive (2026-09-17):** the previous backtest tested too few
# params. Create a new notebook and expose all possible params. Run the
# backtest on which param would give highest returns — infer which
# param is most impactful and does change the alpha to generate edge.
# Run on 100 day random days and save the results. Results should have
# n trade, EV per trade, and a few percentiles of trade duration. Also
# why a trade is entered i.e. bear fvg mitigation.
#
# ## Design
#
# We sweep **the top 10 most-impactful CAUSAL knobs** (one knob flipped
# per config, all else equal to BASELINE). The 10 knobs were picked from
# the existing alpha studies (see `AGENTS.md`) as those with the largest
# empirical effect on EV/trade:
#
# | # | Knob | Config(s) tested |
# |---|---|---|
# | 1 | `fvg_inv_trade_enabled` | `INV_TRADE_ON` (best single-knob from v3) |
# | 2 | `fvg_min_zone_usd` | `MIN_ZONE_050`, `MIN_ZONE_080` |
# | 3 | `invalidation_sl_usd` | `SOFT_STOP_OFF` (off) |
# | 4 | `fvg_invalidation_min_pierce_usd` | `PIERCE_TIGHT` (0.10) |
# | 5 | `sl_atr_mult` | `SL_ATR_050` |
# | 6 | `tp_atr_mult` | `TP_ATR_080` |
# | 7 | `inverse_breadth` | `INVERSE_BREADTH_OFF` |
# | 8 | `bos_choch_ignore_invert_when_aligned` | `BOS_ALIGN_OFF` |
# | 9 | `fvg_sweep_enabled` | `SWEEP_ON` |
# | 10 | `num_layers` | `NUM_LAYERS_1` |
# | (extra) | `fvg_resample_secs`, `renko_drive_invalidation`, `fvg_ifvg_min_inversion_age_secs` | 3 more configs for breadth |
#
# **Total configs:** 15 (1 baseline + 14 single-knob variants).
#
# ## Workflow
#
# The notebook is split into two passes:
#
# 1. **DRY_RUN** — 20 random days, fast (~50 seconds). Own report.
# 2. **EXTEND** — 80 more random days (~3-4 minutes). Merges with the
#    dry-run days for the full 100-day report.
#
# Run the dry-run cell first. Inspect its findings, then run the extend
# cell. The full-100-day output overwrites the dry-only summary tables
# (CSV names carry the `_dry_` vs `_full_` prefix).
#
# ## Output files
#
# * `notebooks/param_sweep_dry_per_day.csv`  — per-day breakdown, 20 days
# * `notebooks/param_sweep_dry_summary.csv`  — per-config totals, 20 days
# * `notebooks/param_sweep_full_per_day.csv` — per-day breakdown, 100 days
# * `notebooks/param_sweep_full_summary.csv` — per-config totals, 100 days
#
# Each summary CSV has:
#   `n_trades`, `pnl_total`, `EV/trade`, `PnL/day`, `trades/day`,
#   `p25_hold_sec`, `p50_hold_sec`, `p75_hold_sec`, `p90_hold_sec`,
#   `p95_hold_sec`, `n_fvg`/`n_ifvg`/`n_orb`/`n_wyckoff`/`n_sweep`/`n_inv`
#   (counts and percentages by `entry_triggered_by`),
#   `n_signals_emitted`, `n_soft_stops`.

# %% [markdown]
# ## Edit knobs

# %%
# Knobs - edit and re-run
DRY_DAYS = 20            # how many random days for DRY_RUN
EXTEND_DAYS = 80         # how many MORE days for EXTEND (total = DRY + EXTEND = 100)
RNG_SEED = 20260917      # reproducible day selection (distinct from alpha_v2)
MIN_BARS_PER_DAY = 30_000

# Run as a subprocess so output is captured in the cell (the tool
# prints its own per-config + delta-vs-baseline summary tables).
import subprocess

# %%
## Locate repo + verify tool exists

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

tool_path = ROOT / 'src' / 'tools' / 'run_param_sweep.py'
assert tool_path.is_file(), f'missing: {tool_path}'
print(f'Tool: {tool_path}')

# %% [markdown]
# ## DRY_RUN — 20 random days

# %%
# First pass: 20 days, ~50 seconds. Inspect the report below.
print(f'\n>>> DRY_RUN: {DRY_DAYS} days <<<\n')
res = subprocess.run(
    [sys.executable, str(tool_path), str(DRY_DAYS)],
    cwd=str(ROOT),
    capture_output=True,
    text=True,
)
print(res.stdout)
if res.returncode != 0:
    print('STDERR:', res.stderr)

# %% [markdown]
# ## Inspect DRY_RUN results

# %%
import pandas as pd

dry_summary_path = ROOT / 'notebooks' / 'param_sweep_dry_summary.csv'
dry_per_day_path = ROOT / 'notebooks' / 'param_sweep_dry_per_day.csv'

dry_summary = pd.read_csv(dry_summary_path)
dry_per_day = pd.read_csv(dry_per_day_path)

print(f'DRY_RUN summary: {dry_summary.shape[0]} configs, '
      f'{len(dry_per_day["day"].unique())} days, '
      f'{int(dry_summary["n_trades"].sum()):,} trades total')
print('\nTop 5 configs by EV/trade:')
print(dry_summary[['config', 'n_trades', 'ev_per_trade', 'pnl_per_day', 'p50_hold_sec']]
      .head(5).to_string(index=False))

print('\nConfigs with positive EV/trade:')
pos = dry_summary[dry_summary['ev_per_trade'] > 0]
if len(pos):
    print(pos[['config', 'ev_per_trade', 'pnl_per_day']].to_string(index=False))
else:
    print('  (none)')

# %% [markdown]
# ## EXTEND — add 80 more days (total = 100)

# %%
# Second pass: 80 more days. Reuses the 20 days from DRY_RUN
# (the EXTEND mode skips the first N_DAYS).
print(f'\n>>> EXTEND: adding {EXTEND_DAYS} more days <<<\n')
res = subprocess.run(
    [sys.executable, str(tool_path), str(DRY_DAYS), 'extend'],
    cwd=str(ROOT),
    capture_output=True,
    text=True,
)
print(res.stdout)
if res.returncode != 0:
    print('STDERR:', res.stderr)

# %% [markdown]
# ## Inspect FULL results (100 days)

# %%
full_summary_path = ROOT / 'notebooks' / 'param_sweep_full_summary.csv'
full_per_day_path = ROOT / 'notebooks' / 'param_sweep_full_per_day.csv'

full_summary = pd.read_csv(full_summary_path)
full_per_day = pd.read_csv(full_per_day_path)

print(f'FULL summary: {full_summary.shape[0]} configs, '
      f'{len(full_per_day["day"].unique())} days, '
      f'{int(full_summary["n_trades"].sum()):,} trades total')

# %% [markdown]
# ## Per-config report (n_trade, EV, PnL/day, percentiles, breakdown by triggered_by)

# %%
print('=' * 100)
print(f'PER-CONFIG REPORT (FULL, 100 days, seed {RNG_SEED})')
print('=' * 100)
report_cols = [
    'config', 'n_trades', 'ev_per_trade', 'pnl_per_day', 'trades_per_day',
    'p25_hold_sec', 'p50_hold_sec', 'p75_hold_sec', 'p90_hold_sec', 'p95_hold_sec',
    'pct_fvg', 'pct_ifvg', 'pct_orb', 'pct_wyckoff', 'pct_sweep', 'pct_inv',
]
print(full_summary[report_cols].to_string(index=False, float_format='%.4f'))

# %% [markdown]
# ## Most impactful knob (delta-EV vs BASELINE)

# %%
baseline_row = full_summary[full_summary['config'] == 'BASELINE'].iloc[0]
baseline_ev = baseline_row['ev_per_trade']
baseline_pnl = baseline_row['pnl_total']
baseline_n = baseline_row['n_trades']

ranking_rows = []
for _, row in full_summary.iterrows():
    if row['config'] == 'BASELINE':
        continue
    ranking_rows.append({
        'config': row['config'],
        'delta_ev_per_trade': row['ev_per_trade'] - baseline_ev,
        'delta_pnl_per_day': (row['pnl_total'] - baseline_pnl) / len(full_per_day['day'].unique()),
        'delta_n_trades': int(row['n_trades'] - baseline_n),
        'absolute_ev_per_trade': row['ev_per_trade'],
        'positive_ev': row['ev_per_trade'] > 0,
    })
ranking = pd.DataFrame(ranking_rows)
ranking = ranking.sort_values('delta_ev_per_trade', key=lambda x: x.abs(), ascending=False)

print('=' * 100)
print(f'MOST IMPACTFUL KNOB (FULL, baseline EV=${baseline_ev:+.4f}, PnL=${baseline_pnl:+.2f})')
print('=' * 100)
print('Ranked by |delta EV/trade| desc -- the biggest absolute change is the MOST IMPACTFUL knob.')
print()
print(ranking.to_string(index=False, float_format=lambda x: f'{x:+.4f}'))

print('\nConfigs with positive EV/trade:')
pos = full_summary[full_summary['ev_per_trade'] > 0]
if len(pos):
    print(pos[['config', 'ev_per_trade', 'pnl_per_day', 'p50_hold_sec']].to_string(
        index=False, float_format='%.4f'))
else:
    print('  (none -- no config produced positive EV at N=100)')

# %% [markdown]
# ## Why a trade is entered — entry_triggered_by breakdown

# %%
print('=' * 80)
print('WHY TRADES ARE ENTERED (FULL, per-config breakdown by triggered_by)')
print('=' * 80)
trigger_cols = [
    'config', 'n_trades',
    'n_fvg', 'n_ifvg', 'n_orb', 'n_wyckoff', 'n_sweep', 'n_inv',
    'pct_fvg', 'pct_ifvg', 'pct_orb', 'pct_wyckoff', 'pct_sweep', 'pct_inv',
]
print(full_summary[trigger_cols].to_string(index=False, float_format='%.1f'))

# %% [markdown]
# ## Notes & next steps

# %%
print("""
Findings to look for in the output above:

1. **Most impactful knob**: the top of the delta-vs-baseline table. A
   delta-EV/trade of magnitude > $0.05 means the knob is the
   single-knob lever with the biggest alpha effect.

2. **Positive-EV configs**: only configs with EV/trade > 0 are
   candidates. The user directive says "infer which param is most
   impactful and does change the alpha to generate edge" -- only
   positive-EV configs count.

3. **Trade-duration percentiles (p25/p50/p75/p90/p95)**: shows the
     shape of the trade-lifetime distribution. A spike at low p25
     means many trades exit quickly (typical for stop-loss hits); a
     long p95 tail means a few trades carry for a long time (typical
     for take-profit runs on extended moves).

4. **triggered_by breakdown**: shows whether the FVG path, the iFVG
   path, the ORB/Wyckoff paths, or the inverse-trade edge is doing
   the work. Configs that lean heavily on one path tell us which
   signal source is the alpha source.

5. **DRY_RUN vs FULL**: if the most-impactful knob changes rank
   between DRY_RUN (20) and FULL (100), the signal is noisy at
   small sample size and the FULL report is the one to act on.
""")