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
# # nb46 — sniper × inverse_breadth=False stacking A/B on 100 days
#
# **User directive (2026-09-17):** "suggests what new experiments to
# do, that changes logically the strategy, then run another backtest
# with the updated agents.md". The logical strategy-change question
# was: does the v8 winner (`inverse_breadth=False`) STACK with the v7
# SNIPER mode?
#
# v9 closes this question with a 6-config sweep on 100 days. Two
# cross-mode questions are answered:
#
# 1. **Stacking**: does `inverse_breadth=False` help or hurt
#    `entry_mode='sniper'`? (v8 only tested immediate mode)
# 2. **TP scaling**: at N=100, does TP=15 / TP=22 / TP=30 produce
#    different EV? (v7 sweep stopped at TP=22 on N=56)
#
# ## Design
#
# 6 single-knob configurations built off the v7 OPTIMAL base:
#
# | # | Config | `entry_mode` | `inverse_breadth` | TP mult |
# |---|---|---|---|---|
# | 1 | SNIPER_TP22_BREADTH_ON  | sniper   | True  | 22.0 |
# | 2 | SNIPER_TP22_BREADTH_OFF | sniper   | False | 22.0 |
# | 3 | SNIPER_TP15_BREADTH_OFF | sniper   | False | 15.0 |
# | 4 | SNIPER_TP30_BREADTH_OFF | sniper   | False | 30.0 |
# | 5 | IMMEDIATE_BREADTH_OFF   | immediate | False | 1.8 |
# | 6 | IMMEDIATE_BREADTH_ON    | immediate | True  | 1.8 |
#
# The headline comparison is row 1 vs row 2 (does stacking help?).
# Row 1 vs rows 3-4 (TP grid). Row 5 vs row 6 (v8 reference).
#
# ## Output files
#
# * `notebooks/sniper_breadth_per_day.csv`   — 600 per-day rows
# * `notebooks/sniper_breadth_summary.csv`   — per-config totals
#
# ## Reference
#
# See AGENTS.md § "v9 — sniper × inverse_breadth=False stacking A/B
# (2026-09-17)" for the full findings.

# %% [markdown]
# ## Edit knobs

# %%
# Knobs - edit and re-run
N_DAYS = 100             # how many random days
RNG_SEED = 20260917      # reproducible day selection (same as v8/v9)
MIN_BARS_PER_DAY = 30_000

# Run as a subprocess so output is captured in the cell.
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

tool_path = ROOT / 'src' / 'tools' / 'run_sniper_breadth.py'
assert tool_path.is_file(), f'missing: {tool_path}'
print(f'Tool: {tool_path}')

# %% [markdown]
# ## Run v9 sweep — 100 random Mon-Fri UTC days

# %%
print(f'\n>>> V9 SWEEP: {N_DAYS} days <<<\n')
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

summary_path = ROOT / 'notebooks' / 'sniper_breadth_summary.csv'
per_day_path = ROOT / 'notebooks' / 'sniper_breadth_per_day.csv'

summary = pd.read_csv(summary_path)
per_day = pd.read_csv(per_day_path)

print(f'V9 summary: {summary.shape[0]} configs, '
      f'{len(per_day["day"].unique())} days, '
      f'{int(summary["trades"].sum()):,} trades total')

print('\nTop configs by PnL/day:')
top = summary.sort_values('pnl_per_day', ascending=False)
print(top[['config', 'trades', 'ev_per_trade', 'pnl_per_day', 'trades_per_day']]
      .to_string(index=False))

# %% [markdown]
# ## Per-day distribution analysis

# %%
print('Per-day PnL distribution by config (mean, std, se, % positive days):')
print('=' * 80)
for cfg, g in per_day.groupby('config'):
    pnls = g['pnl_total']
    se = pnls.std() / (len(pnls) ** 0.5)
    t = pnls.mean() / se if se > 0 else 0.0
    print(f'{cfg:<32}  mean=${pnls.mean():+8.2f}/d  '
          f'std=${pnls.std():7.2f}  se=${se:6.2f}  '
          f't={t:+5.2f}  '
          f'pos={(pnls>0).sum():>3}/100  neg={(pnls<0).sum():>3}/100')

# %% [markdown]
# ## Headline answers
#
# **Q1: does `inverse_breadth=False` stack with sniper?**
# A: NO. Rows SNIPER_TP22_BREADTH_ON and SNIPER_TP22_BREADTH_OFF
# produce identical PnL to 4 decimal places (~$2042). The sniper
# path uses `fvg_inv_trade_sl_zone_mult` / `fvg_inv_trade_tp_zone_mult`
# for SL/TP — completely independent of `inverse_breadth`.
#
# **Q2: which TP wins at N=100?**
# A: TP=30 wins (+$26.46/day) — beats TP=22 (+$20.42/day) by +29.5%.
# TP=15 is worse (+$17.21/day). Monotonic 15→22→30 — peak may
# continue higher (needs v10 to confirm).
#
# **Q3: does SNIPER beat IMMEDIATE on PnL/day?**
# A: Yes at N=100. SNIPER_TP30 +$26.46/day vs IMMEDIATE_BREADTH_OFF
# +$16.08/day. Trade count is much lower (8.3/day vs 61.5/day) but
# EV per trade is 12× higher (+$3.20 vs +$0.26).
#
# **Decision: do NOT promote TP=30 to canonical yet** — full-corpus
# walk-forward (v10) required first. The current v7 recipe (TP=22)
# is full-corpus-validated on 654 days. TP=30 is only N=100.
