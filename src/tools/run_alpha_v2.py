#!/usr/bin/env python
"""alpha_v2 — explore rule removals + BOS/CHoCH gating + new candle rules.

This script answers the user directive issued 2026-09-15:

  > execute the new categorization. if there is alpha to be extracted, we
  > will write new rules on how to categorize them without lookahead bias.
  > these should be done in the new repo. there would also be new changes
  > to the previous scripts. re-copy them. remove the 30 trades a day cap.
  > it would seem that fvgs, without an additional signal source, does not
  > seem to work. we can capture price movement magnitude, but
  > directionally we are wrong due to not knowing when to ignore
  > inversions. run a backtest using BOS and CHOCH as directional gating.
  > be sure to not introduce lookahead bias (i.e knowing BOS and entering
  > a trade before it fully forms). check out why trades are happening
  > much too infrequently relative to number of fvgs drew. we can remove
  > EMA trend gating as it seems like it has no effect on profitability.
  > suggest other gates/rules that should be removed, and also what other
  > methods to combine with fvg for profit. do not use simple filters
  > like RSI MACD. perhaps something like swing high/low to detect
  > liquidity-sweeps, order block detection, consolidation/manipulation/
  > distribution detection etc

  > hunt drop as logic should also be dropped because it is lookahead bias.
  > any post-hoc classification to further improve on the algo should not
  > exist.

The script runs 10+ configurations on N random Mon-Fri UTC days across the
1y parquet and reports per-config EV/trade, PnL, n_trades, and signal-to-
fill ratios.

Configurations tested:

  BASELINE       : default v2 (HUNT drop OFF, bias gate OFF, 2 h lifetime,
                   parallel signals, supersession ON)
  NO_BIAS_GATE   : explicit disable (same as baseline v2)
  BIAS_GATE      : gate_on_gmma_bias=True (legacy)
  NO_SUPERSEDE   : fvg_supersede_on_new=False
  NO_SUPERSEDE_NO_BIAS : combine the two
  NO_RETEST_REQ  : fvg_require_retest_to_invert=False
  BOS_GATE       : ms_min_conviction=0.5 (CHoCH-against kills signals)
  BOS_BOOST_ONLY : ms_min_conviction=0.0, ms_boost_conviction=1.5
  DROP_INVERTED  : drop_inverted_fvg=True (legacy)
  DROP_INVERTED_OFF : drop_inverted_fvg=False (both paths fire on inverted)
  INV_TRADE      : fvg_inv_trade_enabled=True

  ALPHA_CANDIDATES (anti-lookahead new rules, see AGENTS.md):
  CAND_DISPLACE  : fvg_displacement_ratio=2.0 (c2 body >= 2x larger)
  CAND_DISP15    : fvg_displacement_ratio=1.5
  CAND_DISP_ENGULF : fvg_displacement_ratio=2.0 + engulfing c2 over c1/c3
  CAND_BODY_ONLY : fvg_body_only_invalidation=True
  CAND_ALL       : combine all alpha candidates

Output:
  - notebooks/alpha_v2_per_day.csv   per-day breakdown
  - notebooks/alpha_v2_summary.csv   per-config totals
  - notebooks/alpha_v2_run.log       stdout

Usage:
  python src/tools/run_alpha_v2.py [N_DAYS]
  python src/tools/run_alpha_v2.py 30    # 30 days (default 60)
"""
# %%
from __future__ import annotations
import os
import sys
import time
import io
import contextlib
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pds
import pyarrow.parquet as pq

NS_PER_DAY = 86_400_000_000_000

# Knobs
N_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
RNG_SEED = 20260915
MIN_BARS_PER_DAY = 30_000

# %%
def _find_root():
    here = Path('.').resolve()
    for p in [here, *here.parents]:
        if (p / 'src' / 'core' / 'ict_signals.py').is_file():
            return p
    raise RuntimeError('no repo root')


ROOT = _find_root()
sys.path.insert(0, str(ROOT))

# %%
os.environ.setdefault('MPLBACKEND', 'Agg')

from src.core.ict_strategy import TrendStrategyParams
from src.backtest.ict_backtest import run_ict_backtest

# %%
# Data
data_dir = ROOT / 'data'
if not (data_dir / 'XAUUSD_S1_1y.parquet').exists():
    for ancestor in [ROOT.parent, *ROOT.parent.parents]:
        sibling = ancestor / 'data'
        if (sibling / 'XAUUSD_S1_1y.parquet').exists():
            data_dir = sibling
            break
parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'

# %%
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
print(f'Sampled {n_sample} days')

# %%
# Common baseline (v2 defaults — no HUNT drop, bias gate off, 2 h lifetime)
def _common() -> TrendStrategyParams:
    return TrendStrategyParams(
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
        ms_boost_conviction=1.0,           # OFF by default in v2
        use_atr_scaling=True,
        atr_len=1200,
        sl_atr_mult=0.25, tp_atr_mult=0.55,
        sl_usd=0.80, tp_usd=1.80, lots=0.01,
        fvg_require_retest_to_invert=True,
        fvg_invalidation_min_pierce_usd=0.05,
        fvg_invalidation_min_consecutive_bars=2,
        fvg_supersede_on_new=True,
        renko_drive_invalidation=False,
        fvg_sweep_enabled=False,
        fvg_invalidate_on_structure=False,
        fvg_body_only_mitigation=False,
        fvg_body_only_invalidation=False,
        fvg_ifvg_min_inversion_age_secs=0,
        fvg_inv_trade_enabled=False,
        gate_on_gmma_bias=False,
        layer_lifetime_secs=7200,
    )


CONFIGURATIONS = {
    # Baseline + single-knob experiments
    "BASELINE":              {},
    "BIAS_GATE":             {"gate_on_gmma_bias": True},
    "NO_SUPERSEDE":          {"fvg_supersede_on_new": False},
    "NO_RETEST_REQ":         {"fvg_require_retest_to_invert": False},
    "BOS_BOOST_ONLY":        {"ms_boost_conviction": 1.5},
    "DROP_INVERTED_OFF":     {"drop_inverted_fvg": False},
    "INV_TRADE":             {"fvg_inv_trade_enabled": True},
    # New anti-lookahead alpha candidates (CAUSAL — only c1, c2, c3 fields)
    "CAND_DISPLACE":         {"fvg_displacement_ratio": 2.0},
    "CAND_DISP15":           {"fvg_displacement_ratio": 1.5},
    "CAND_BODY_ONLY_INV":    {"fvg_body_only_invalidation": True},
    "CAND_BODY_ONLY_MIT":    {"fvg_body_only_mitigation": True},
    "CAND_IFVG_AGE_60":      {"fvg_ifvg_min_inversion_age_secs": 60},
    # Layer lifetime A/B test (user directive 2026-09-15)
    "LIFETIME_30MIN":        {"layer_lifetime_secs": 1800},
    "LIFETIME_UNLIMITED":    {"layer_lifetime_secs": 0},
    # BoS/CHoCH directional gate (new implementation 2026-09-15)
    "BOS_GATE_STRICT":       {"bos_choch_directional_gate": True,
                              "bos_choch_gate_pass_neutral": False},
    "BOS_GATE_PASS_NEUTRAL": {"bos_choch_directional_gate": True,
                              "bos_choch_gate_pass_neutral": True},
    "BOS_GATE_60S":          {"bos_choch_directional_gate": True,
                              "bos_choch_gate_age_bars": 60,
                              "bos_choch_gate_pass_neutral": False},
    # Combined
    "ALPHA_STACK": {
        "fvg_displacement_ratio": 1.5,
        "fvg_body_only_invalidation": True,
        "fvg_ifvg_min_inversion_age_secs": 60,
    },
    # NOTE (2026-09-17): DROP_D_HUNT removed (fvg_drop_qualities /
    # fvg_drop_tiers knobs are gone — the underlying candle-pattern
    # A/B/C/D classifier and price-rank tier system were look-
    # forward biased and have been deleted).
}


def _make_config(name: str) -> TrendStrategyParams:
    p = _common()
    for k, v in CONFIGURATIONS[name].items():
        setattr(p, k, v)
    return p


# %%
# Per-day backtest loop
dataset = pds.dataset(str(parquet_path), format='parquet')

# Pre-load all sampled days ONCE
print('Pre-loading all days...', flush=True)
t_pre = time.time()
day_data: dict[str, pd.DataFrame] = {}
for di, day_id in enumerate(sample_days):
    viz_start = pd.Timestamp(int(day_id) * NS_PER_DAY, unit='ns', tz='UTC')
    viz_end = viz_start + pd.Timedelta(days=1)
    table = dataset.to_table(
        columns=['time', 'open', 'high', 'low', 'close'],
        filter=(pds.field('time') >= viz_start) & (pds.field('time') < viz_end),
    )
    if len(table) < MIN_BARS_PER_DAY:
        continue
    df_day = table.to_pandas().sort_values('time').reset_index(drop=True)
    day_data[str(viz_start.date())] = df_day
print(f'Pre-load: {time.time() - t_pre:.1f}s, {len(day_data)} days', flush=True)


def _run_one(cfg_name: str, day_label: str, df_day: pd.DataFrame) -> dict:
    p = _make_config(cfg_name)
    buf = io.StringIO()
    err = ""
    try:
        with contextlib.redirect_stdout(buf):
            res = run_ict_backtest(df_day, p, strategy_label=f'alpha_v2_{cfg_name}')
    except Exception as e:
        err = str(e)
        res = None
    if res is None:
        return {
            'config': cfg_name, 'day': day_label, 'n_trades': 0,
            'pnl_total': 0.0, 'ev_per_trade': 0.0, 'n_fills': 0,
            'n_signals_emitted': 0, 'n_signals_consumed': 0,
            'n_soft_stops': 0, 'n_inv_trades_submitted': 0,
            'n_inv_trades_filled': 0, 'n_fvg_zones': 0,
            'n_fvg_live': 0, 'n_fvg_inverted': 0,
            # NOTE (2026-09-17): n_fvg_marginal / n_fvg_hunt
            # removed (the candle-quality classifier that populated
            # them was look-forward biased).
            'trades_per_zone': 0.0, 'fill_rate': 0.0,
            'error': err,
        }
    n_t = len(res.trades)
    pnl = float(sum(t.pnl_usd for t in res.trades))
    ev = pnl / n_t if n_t > 0 else 0.0
    fill_rate = res.n_fills / max(1, res.n_signals_emitted * p.num_layers)
    return {
        'config': cfg_name, 'day': day_label, 'n_trades': n_t,
        'pnl_total': pnl, 'ev_per_trade': ev,
        'n_fills': res.n_fills,
        'n_signals_emitted': res.n_signals_emitted,
        'n_signals_consumed': res.n_signals_consumed,
        'n_soft_stops': res.n_soft_stops,
        'n_inv_trades_submitted': res.n_inv_trades_submitted,
        'n_inv_trades_filled': res.n_inv_trades_filled,
        # NOTE (2026-09-17): n_fvg_zones / n_fvg_marginal /
        # n_fvg_hunt removed (depended on removed A/B/C/D
        # classifier fields).
        'n_fvg_zones': 0,
        'n_fvg_live': 0,  # not exposed; we have total counts
        'n_fvg_inverted': res.n_inversions_detected,
        'n_fvg_marginal': 0,
        'n_fvg_hunt': 0,
        'trades_per_zone': 0.0,
        'fill_rate': fill_rate,
        'error': err,
    }


# %%
t_start = time.time()
all_runs = []
configs = list(CONFIGURATIONS.keys())
nb_dir = ROOT / 'notebooks'
csv_path = nb_dir / 'alpha_v2_per_day.csv'

total_jobs = len(configs) * len(day_data)
print(f'Running {total_jobs} backtests ({len(configs)} configs x {len(day_data)} days)...',
      flush=True)
job_idx = 0
for cfg_name in configs:
    print(f'\n=== {cfg_name} ===', flush=True)
    for day_label, df_day in day_data.items():
        job_idx += 1
        if job_idx % 50 == 0:
            elapsed = time.time() - t_start
            eta = elapsed * (total_jobs - job_idx) / job_idx
            print(f'  [{job_idx}/{total_jobs}] {cfg_name} {day_label} '
                  f'(elapsed {elapsed:.1f}s, ETA {eta:.1f}s)', flush=True)
        result = _run_one(cfg_name, day_label, df_day)
        all_runs.append(result)

elapsed = time.time() - t_start
print(f'\nAll backtests done in {elapsed:.1f}s ({elapsed/total_jobs:.3f}s per backtest)',
      flush=True)

# %%
# Save per-day CSV
df_runs = pd.DataFrame(all_runs)
df_runs.to_csv(csv_path, index=False)
print(f'Per-day CSV: {csv_path}')

# %%
# Per-config summary
agg_cols = ['n_trades', 'pnl_total',
            'n_fills', 'n_signals_emitted',
            'n_signals_consumed', 'n_soft_stops', 'n_inv_trades_submitted',
            'n_inv_trades_filled', 'n_fvg_zones', 'n_fvg_inverted',
            # NOTE (2026-09-17): n_fvg_marginal / n_fvg_hunt
            # removed (depended on removed classifier).
            ]
summary = df_runs.groupby('config')[agg_cols].sum().reset_index()
summary['ev_per_trade'] = summary['pnl_total'] / summary['n_trades'].clip(lower=1)
summary['pnl_per_day'] = summary['pnl_total'] / len(day_data)
summary['trades_per_day'] = summary['n_trades'] / len(day_data)
summary['fill_rate'] = summary['n_fills'] / (summary['n_signals_emitted'] * 3).clip(lower=1)

# Sort by EV/trade (descending) — only positive-EV configs win
summary = summary.sort_values('ev_per_trade', ascending=False)
summary_path = nb_dir / 'alpha_v2_summary.csv'
summary.to_csv(summary_path, index=False)
print(f'\nSummary CSV: {summary_path}')

# %%
# Pretty-print summary
print('\n' + '=' * 100)
print('ALPHA V2 - PER-CONFIG SUMMARY (sorted by EV/trade; only POSITIVE EV matters)')
print('=' * 100)
hdr = (f'{"config":<24} {"trades":>7} {"EV":>10} {"PnL/day":>10} '
       f'{"tr/day":>7} {"fills":>6} {"sigs":>5} {"soft":>5} {"inv_tr":>6} '
       f'{"ev>0?":>6}')
print(hdr)
print('-' * 100)
for _, row in summary.iterrows():
    positive_ev = 'YES' if row['ev_per_trade'] > 0 else 'no'
    print(f'{row["config"]:<24} {int(row["n_trades"]):>7} '
          f'${row["ev_per_trade"]:>+9.4f} ${row["pnl_per_day"]:>+9.2f} '
          f'{row["trades_per_day"]:>7.1f} {int(row["n_fills"]):>6} '
          f'{int(row["n_signals_emitted"]):>5} {int(row["n_soft_stops"]):>5} '
          f'{int(row["n_inv_trades_filled"]):>6} {positive_ev:>6}')

# %%
# Per-config delta vs BASELINE — rank by positive EV/trade delta
baseline_row = summary[summary['config'] == 'BASELINE'].iloc[0]
baseline_pnl = baseline_row['pnl_total']
print(f'\nDelta-PnL vs BASELINE (gross=${baseline_pnl:.2f} over {len(day_data)} days):')
print(f'  baseline trades={int(baseline_row["n_trades"])}, EV/trade=${baseline_row["ev_per_trade"]:+.4f}')
results = []
for _, row in summary.iterrows():
    if row['config'] == 'BASELINE':
        continue
    delta_pnl = row['pnl_total'] - baseline_pnl
    delta_ev = row['ev_per_trade'] - baseline_row['ev_per_trade']
    delta_n = int(row['n_trades'] - baseline_row['n_trades'])
    results.append({
        'config': row['config'],
        'delta_ev_per_trade': delta_ev,
        'delta_pnl_per_day': delta_pnl / len(day_data),
        'delta_n_trades': delta_n,
        'positive_ev': row['ev_per_trade'] > 0,
    })
results.sort(key=lambda r: r['delta_ev_per_trade'], reverse=True)
print(f'  {"config":<24} {"dEV/trade":>10} {"dPnL/day":>10} {"dtrades":>8} {"positive_EV":>10}')
for r in results:
    flag = 'YES' if r['positive_ev'] else 'no'
    print(f'  {r["config"]:<24} ${r["delta_ev_per_trade"]:>+9.4f} '
          f'${r["delta_pnl_per_day"]:>+9.2f} {r["delta_n_trades"]:+8d} {flag:>10}')
