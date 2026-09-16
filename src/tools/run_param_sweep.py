#!/usr/bin/env python
"""run_param_sweep.py - sweep the top-10 most-impactful causal params.

User directive (2026-09-17):
  > the previous backtest tested too few params. create a new notebook
  > and expose all possible params. run backtest on which param would
  > give highest returns i.e infer which param is most impactful and
  > does change the alpha to generate edge. run on 100 day random days
  > and save the results. results shld have n trade ev per trade and a
  > few percentiles of trade duration. also why a trade is entered i.e
  > bear fvg mitigation

Workflow:
  * DRY_RUN on 20 random days (fast), full report in
    notebooks/param_sweep_dry_per_day.csv +
    notebooks/param_sweep_dry_summary.csv.
  * EXTEND run adds 80 more days (for a 100-day total) and re-uses the
    20 days from DRY_RUN. Final CSV: notebooks/param_sweep_full_*.csv.

Each config is BASELINE + ONE knob flipped. The delta-vs-baseline table
in the summary identifies the most-impactful params by absolute dEV/trade.

Configs (10 knobs x 1-2 settings = 14 configs + 1 baseline = 15 total):

  BASELINE                - v2 defaults (no extra knobs)
  INV_TRADE_ON            - fvg_inv_trade_enabled=True
  MIN_ZONE_050            - fvg_min_zone_usd=0.50 (was 0.30)
  MIN_ZONE_080            - fvg_min_zone_usd=0.80
  SOFT_STOP_OFF           - invalidation_sl_usd=0.0 (no soft-stop)
  PIERCE_TIGHT            - fvg_invalidation_min_pierce_usd=0.10
  SL_ATR_050              - sl_atr_mult=0.50 (was 0.25)
  TP_ATR_080              - tp_atr_mult=0.80 (was 0.55)
  INVERSE_BREADTH_OFF     - inverse_breadth=False (wider zone -> wider SL)
  BOS_ALIGN_OFF           - bos_choch_ignore_invert_when_aligned=False
  SWEEP_ON                - fvg_sweep_enabled=True + additional_sources=['sweep']
  RENKO_INV_ON            - renko_drive_invalidation=True
  NUM_LAYERS_1            - num_layers=1 (was 3)
  RESAMPLE_1S             - fvg_resample_secs=0 (was 60)
  IFVG_AGE_60             - fvg_ifvg_min_inversion_age_secs=60

Usage:
  python src/tools/run_param_sweep.py 20            # DRY_RUN: 20 days
  python src/tools/run_param_sweep.py 20 extend     # add 80 more days
"""
from __future__ import annotations
import os
import sys
import io
import contextlib
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pds
import pyarrow.parquet as pq

NS_PER_DAY = 86_400_000_000_000
MIN_BARS_PER_DAY = 30_000

# Knobs at top of file (per notebook conventions)
N_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 20
EXTEND = len(sys.argv) > 2 and sys.argv[2].lower() in ("extend", "ext", "+")
RNG_SEED = 20260917      # distinct from alpha_v2 (20260915) + optimal (20260917)

DURATION_PCTILES = (25, 50, 75, 90, 95)


def _find_root() -> Path:
    here = Path('.').resolve()
    for p in [here, *here.parents]:
        if (p / 'src' / 'core' / 'ict_signals.py').is_file():
            return p
    raise RuntimeError('no repo root')


ROOT = _find_root()
sys.path.insert(0, str(ROOT))
os.environ.setdefault('MPLBACKEND', 'Agg')

from src.core.ict_strategy import TrendStrategyParams
from src.backtest.ict_backtest import run_ict_backtest


# Common baseline params (v2 defaults, no extra knobs)
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
        ms_boost_conviction=1.0,
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
        bos_choch_ignore_invert_when_aligned=True,
        bos_choch_memory_n_events=5,
    )


# Configurations: BASELINE + ONE knob flipped
CONFIGURATIONS = {
    # Reference
    "BASELINE":              {},

    # Trade-management knobs
    "INV_TRADE_ON":          {"fvg_inv_trade_enabled": True},
    "SOFT_STOP_OFF":         {"invalidation_sl_usd": 0.0},
    "PIERCE_TIGHT":          {"fvg_invalidation_min_pierce_usd": 0.10},
    "INVERSE_BREADTH_OFF":   {"inverse_breadth": False},
    "NUM_LAYERS_1":          {"num_layers": 1},

    # SL/TP scaling
    "SL_ATR_050":            {"sl_atr_mult": 0.50},
    "TP_ATR_080":            {"tp_atr_mult": 0.80},

    # Detector knobs
    "MIN_ZONE_050":          {"fvg_min_zone_usd": 0.50},
    "MIN_ZONE_080":          {"fvg_min_zone_usd": 0.80},
    "RESAMPLE_1S":           {"fvg_resample_secs": 0},

    # Anti-noise rules
    "BOS_ALIGN_OFF":         {"bos_choch_ignore_invert_when_aligned": False},
    "SWEEP_ON":              {"fvg_sweep_enabled": True,
                              "additional_sources": ["ifvg", "sweep"]},
    "RENKO_INV_ON":          {"renko_drive_invalidation": True,
                              "renko_brick_size_usd": 0.30,
                              "renko_invalidation_min_bricks": 2,
                              "renko_invalidation_buffer_usd": 0.05},
    "IFVG_AGE_60":           {"fvg_ifvg_min_inversion_age_secs": 60},
}


def _make_config(name: str) -> TrendStrategyParams:
    p = _common()
    for k, v in CONFIGURATIONS[name].items():
        setattr(p, k, v)
    return p


def main():
    # Data
    data_dir = ROOT / 'data'
    if not (data_dir / 'XAUUSD_S1_1y.parquet').exists():
        for ancestor in [ROOT.parent, *ROOT.parent.parents]:
            sibling = ancestor / 'data'
            if (sibling / 'XAUUSD_S1_1y.parquet').exists():
                data_dir = sibling
                break
    parquet_path = data_dir / 'XAUUSD_S1_1y.parquet'

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
    print(f'Day enumeration: {time.time() - t0:.1f}s, {len(unique_days):,} Mon-Fri days', flush=True)

    rng = np.random.default_rng(RNG_SEED)
    total_to_sample = min(100, len(unique_days))
    all_sampled_days = rng.choice(unique_days, size=total_to_sample, replace=False)
    all_sampled_days.sort()

    if EXTEND:
        dry_n = N_DAYS  # in extend mode N_DAYS is the dry-run count
        sample_days = all_sampled_days[dry_n:100]
        print(f'EXTEND mode: skipping first {dry_n} dry-run days, '
              f'adding {len(sample_days)} new days', flush=True)
    else:
        sample_days = all_sampled_days[:N_DAYS]
        print(f'Sampled {len(sample_days)} days (seed {RNG_SEED})', flush=True)

    # Pre-load day data
    dataset = pds.dataset(str(parquet_path), format='parquet')
    print('Pre-loading all days...', flush=True)
    t_pre = time.time()
    day_data: dict[str, pd.DataFrame] = {}
    for day_id in sample_days:
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
    print(f'Pre-load: {time.time() - t_pre:.1f}s, {len(day_data)} valid days', flush=True)

    # Per-day run
    def _run_one(cfg_name: str, day_label: str, df_day: pd.DataFrame) -> dict:
        p = _make_config(cfg_name)
        buf = io.StringIO()
        err = ""
        try:
            with contextlib.redirect_stdout(buf):
                res = run_ict_backtest(df_day, p, strategy_label=f'sweep_{cfg_name}')
        except Exception as e:
            err = repr(e)
            res = None

        if res is None or len(res.trades) == 0:
            return {
                'config': cfg_name, 'day': day_label, 'n_trades': 0,
                'pnl_total': 0.0, 'ev_per_trade': 0.0,
                'avg_hold_sec': 0.0,
                'p25_hold_sec': 0.0, 'p50_hold_sec': 0.0,
                'p75_hold_sec': 0.0, 'p90_hold_sec': 0.0, 'p95_hold_sec': 0.0,
                'n_fvg': 0, 'n_ifvg': 0, 'n_orb': 0, 'n_wyckoff': 0,
                'n_sweep': 0, 'n_inv': 0,
                'n_fills': 0, 'n_signals_emitted': 0, 'n_soft_stops': 0,
                'error': err,
            }

        holds = np.array([t.hold_secs for t in res.trades], dtype=np.float64)
        triggered = [str(t.entry_triggered_by) for t in res.trades]
        pnl_total = float(sum(t.pnl_usd for t in res.trades))
        n_t = len(res.trades)

        counts = {k: 0 for k in ('fvg', 'ifvg', 'orb', 'wyckoff', 'sweep', 'inv')}
        for tb in triggered:
            if tb in counts:
                counts[tb] += 1

        return {
            'config': cfg_name, 'day': day_label,
            'n_trades': n_t,
            'pnl_total': pnl_total,
            'ev_per_trade': pnl_total / n_t if n_t else 0.0,
            'avg_hold_sec': float(holds.mean()) if len(holds) else 0.0,
            'p25_hold_sec': float(np.percentile(holds, 25)) if len(holds) else 0.0,
            'p50_hold_sec': float(np.percentile(holds, 50)) if len(holds) else 0.0,
            'p75_hold_sec': float(np.percentile(holds, 75)) if len(holds) else 0.0,
            'p90_hold_sec': float(np.percentile(holds, 90)) if len(holds) else 0.0,
            'p95_hold_sec': float(np.percentile(holds, 95)) if len(holds) else 0.0,
            'n_fvg': counts['fvg'], 'n_ifvg': counts['ifvg'],
            'n_orb': counts['orb'], 'n_wyckoff': counts['wyckoff'],
            'n_sweep': counts['sweep'], 'n_inv': counts['inv'],
            'n_fills': res.n_fills,
            'n_signals_emitted': res.n_signals_emitted,
            'n_soft_stops': res.n_soft_stops,
            'error': err,
        }

    # Run loop
    configs = list(CONFIGURATIONS.keys())
    nb_dir = ROOT / 'notebooks'
    run_label = 'dry' if not EXTEND else 'full'
    per_day_csv = nb_dir / f'param_sweep_{run_label}_per_day.csv'
    summary_csv = nb_dir / f'param_sweep_{run_label}_summary.csv'

    total_jobs = len(configs) * len(day_data)
    print(f'\nRunning {total_jobs} backtests '
          f'({len(configs)} configs x {len(day_data)} days, label={run_label})...',
          flush=True)

    t_start = time.time()
    all_runs: list[dict] = []
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
            all_runs.append(_run_one(cfg_name, day_label, df_day))

    elapsed = time.time() - t_start
    print(f'\nAll backtests done in {elapsed:.1f}s '
          f'({elapsed/max(total_jobs,1):.3f}s per backtest)', flush=True)

    # Save per-day CSV
    df_runs = pd.DataFrame(all_runs)
    df_runs.to_csv(per_day_csv, index=False)
    print(f'\nPer-day CSV: {per_day_csv}')

    # Per-config summary (pooled across days)
    agg_cols = ['n_trades', 'pnl_total', 'n_fills', 'n_signals_emitted',
                'n_soft_stops',
                'n_fvg', 'n_ifvg', 'n_orb', 'n_wyckoff', 'n_sweep', 'n_inv']
    summary = df_runs.groupby('config')[agg_cols].sum().reset_index()
    summary['ev_per_trade'] = summary['pnl_total'] / summary['n_trades'].clip(lower=1)
    summary['pnl_per_day'] = summary['pnl_total'] / len(day_data)
    summary['trades_per_day'] = summary['n_trades'] / len(day_data)

    # Aggregate duration percentiles from per-day rows.
    # Per-day percentile values are pooled using n_trades weights.
    def _weighted_pctile(group: pd.DataFrame, col: str) -> float:
        w = group['n_trades'].to_numpy(dtype=np.float64)
        v = group[col].to_numpy(dtype=np.float64)
        if w.sum() == 0:
            return 0.0
        return float((w * v).sum() / w.sum())

    pctile_cols = [f'p{p}_hold_sec' for p in DURATION_PCTILES]
    for pc in pctile_cols:
        summary[pc] = df_runs.groupby('config').apply(
            lambda g: _weighted_pctile(g, pc), include_groups=False,
        ).reindex(summary['config']).values

    # Entry-trigger breakdown (percentages)
    total_triggered = (
        summary['n_fvg'] + summary['n_ifvg'] + summary['n_orb']
        + summary['n_wyckoff'] + summary['n_sweep'] + summary['n_inv']
    ).clip(lower=1)
    summary['pct_fvg'] = 100 * summary['n_fvg'] / total_triggered
    summary['pct_ifvg'] = 100 * summary['n_ifvg'] / total_triggered
    summary['pct_orb'] = 100 * summary['n_orb'] / total_triggered
    summary['pct_wyckoff'] = 100 * summary['n_wyckoff'] / total_triggered
    summary['pct_sweep'] = 100 * summary['n_sweep'] / total_triggered
    summary['pct_inv'] = 100 * summary['n_inv'] / total_triggered

    summary = summary.sort_values('ev_per_trade', ascending=False)
    summary.to_csv(summary_csv, index=False)
    print(f'Summary CSV: {summary_csv}')

    # Print report
    print('\n' + '=' * 130)
    print(f'PARAM SWEEP - {len(configs)} configs x {len(day_data)} days '
          f'(label={run_label}, seed {RNG_SEED})')
    print('=' * 130)
    hdr = (f'{"config":<22} {"trades":>7} {"EV":>10} {"PnL/day":>10} '
           f'{"tr/day":>7} {"p50_hold":>10} {"%fvg":>6} {"%ifvg":>6} '
           f'{"%inv":>6} {"%sweep":>7} {"ev>0?":>6}')
    print(hdr)
    print('-' * 130)
    for _, row in summary.iterrows():
        positive_ev = 'YES' if row['ev_per_trade'] > 0 else 'no'
        print(f'{row["config"]:<22} {int(row["n_trades"]):>7} '
              f'${row["ev_per_trade"]:>+9.4f} ${row["pnl_per_day"]:>+9.2f} '
              f'{row["trades_per_day"]:>7.1f} {row["p50_hold_sec"]:>9.1f}s '
              f'{row["pct_fvg"]:>5.1f}% {row["pct_ifvg"]:>5.1f}% '
              f'{row["pct_inv"]:>5.1f}% {row["pct_sweep"]:>6.1f}% '
              f'{positive_ev:>6}')

    # Delta-vs-baseline ranking (most impactful knob)
    baseline_row = summary[summary['config'] == 'BASELINE'].iloc[0]
    baseline_ev = baseline_row['ev_per_trade']
    baseline_pnl = baseline_row['pnl_total']
    print(f'\nDelta-EV/trade vs BASELINE (${baseline_ev:+.4f}, '
          f'${baseline_pnl:+.2f} over {len(day_data)} days):')
    print('  Ranked by |delta EV/trade| desc - the knob with the biggest')
    print('  absolute change is the MOST IMPACTFUL.')
    print(f'  {"config":<22} {"dEV/trade":>10} {"dPnL/day":>10} {"dtrades":>8} '
          f'{"positive_EV":>10} {"direction":>10}')

    results = []
    for _, row in summary.iterrows():
        if row['config'] == 'BASELINE':
            continue
        delta_ev = row['ev_per_trade'] - baseline_ev
        delta_pnl = row['pnl_total'] - baseline_pnl
        delta_n = int(row['n_trades'] - baseline_row['n_trades'])
        direction = 'better' if delta_ev > 0 else 'worse'
        results.append({
            'config': row['config'],
            'delta_ev_per_trade': delta_ev,
            'delta_pnl_per_day': delta_pnl / len(day_data),
            'delta_n_trades': delta_n,
            'positive_ev': row['ev_per_trade'] > 0,
            'direction': direction,
        })
    results.sort(key=lambda r: abs(r['delta_ev_per_trade']), reverse=True)
    for r in results:
        flag = 'YES' if r['positive_ev'] else 'no'
        print(f'  {r["config"]:<22} ${r["delta_ev_per_trade"]:>+9.4f} '
              f'${r["delta_pnl_per_day"]:>+9.2f} {r["delta_n_trades"]:+8d} '
              f'{flag:>10} {r["direction"]:>10}')

    print()


if __name__ == "__main__":
    main()