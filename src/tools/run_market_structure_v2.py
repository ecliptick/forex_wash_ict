"""v10 — BoS/CHoCH conviction tuning on SNIPER base (added 2026-09-17).

Per AGENTS.md v10: the v6 BoS/CHoCH A/B found that conviction is
"computed but unused" on the immediate-mode baseline
(`ms_min_conviction=0.0`, `ms_boost_conviction=1.0`). But that was
on v6 OPTIMAL — never tested on the v7 SNIPER base. This sweep
tests ALL 5 BoS/CHoCH intervention points on the sniper path:

  1. use_market_structure=False     → kill ALL structure detection
  2. ms_min_conviction=0.5          → drop CHoCH-against trades
  3. ms_min_conviction=0.7          → stricter conviction floor
  4. ms_boost_conviction=1.5        → widen TP on conviction > 1.0
  5. bos_choch_ignore_invert_when_aligned=False → soft-stop fires
                                       even on aligned entries
  6. fvg_invalidate_on_structure=True → structural FVG kill
                                       (UNTESTED — Tier-3 #18)
  7. fvg_invalidate_on_structure=True + age=1800 → wider window
  8. SNIPER_BASE                    → reference (v7 OPTIMAL)

Configs tested:
  1. SNIPER_BASE
  2. MS_OFF
  3. CONV_05
  4. CONV_07
  5. BOOST_15
  6. BOS_ALIGN_OFF
  7. FVG_INV_STRUCT_ON
  8. FVG_INV_STRUCT_1800

Output:
  - notebooks/market_structure_v2_per_day.csv   per-day breakdown
  - notebooks/market_structure_v2_summary.csv   per-config totals
  - stdout summary table

Usage:
  python src/tools/run_market_structure_v2.py [N_DAYS]
"""
from __future__ import annotations
import os, sys, time, io, contextlib
from pathlib import Path
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pds
import pyarrow.parquet as pq

NS_PER_DAY = 86_400_000_000_000
N_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 100
RNG_SEED = 20260917  # same seed as v8/v9 for comparability
MIN_BARS_PER_DAY = 30_000


def _find_root():
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


# ── Configs ─────────────────────────────────────────────────────────────
def _common() -> TrendStrategyParams:
    """v7 OPTIMAL SNIPER base — used for ALL configs (single-knob flips)."""
    return TrendStrategyParams(
        signal_source='fvg',
        additional_sources=['ifvg'],
        fvg_resample_secs=60,
        fvg_min_zone_usd=0.30,
        num_layers=3,
        inverse_breadth=True,
        invalidation_sl_usd=0.05,
        invalidation_buffer_usd=0.02,
        # ── BoS/CHoCH baseline ──
        use_market_structure=True,
        ms_min_conviction=0.0,
        ms_boost_conviction=1.0,
        ms_max_boost_age_bars=60,
        ms_choch_caution_age_bars=60,
        ms_apply_to_fvg=True,
        ms_apply_to_ifvg=True,
        ms_apply_to_orb=True,
        ms_apply_to_wyckoff=True,
        ms_pivot_len=9,
        ms_liquidity_len=30,
        ms_resample_secs=60,
        ms_draw_order_blocks=True,
        ms_draw_liquidity_sweeps=True,
        # ── Standard knobs ──
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
        fvg_invalidate_on_structure=False,   # master switch — flipped in #7, #8
        gate_on_gmma_bias=False,
        layer_lifetime_secs=7200,
        bos_choch_ignore_invert_when_aligned=True,
        bos_choch_memory_n_events=5,
        fvg_min_lifetime_secs=3,
        # ── v7 SNIPER ──
        entry_mode='sniper',
        fvg_inv_trade_sl_zone_mult=1.0,
        fvg_inv_trade_tp_zone_mult=22.0,
        fvg_inv_trade_min_zone_usd=0.30,
        fvg_inv_trade_max_per_zone=1,
        sniper_max_age_secs=1800,
    )


CONFIGURATIONS = {
    # 1. SNIPER_BASE — reference (v7 OPTIMAL)
    "SNIPER_BASE": {},
    # 2. MS_OFF — kill ALL structure detection
    "MS_OFF": {"use_market_structure": False},
    # 3. CONV_05 — drop CHoCH-against (conviction < 0.5)
    "CONV_05": {"ms_min_conviction": 0.5},
    # 4. CONV_07 — stricter
    "CONV_07": {"ms_min_conviction": 0.7},
    # 5. BOOST_15 — widen TP on conviction > 1.0
    "BOOST_15": {"ms_boost_conviction": 1.5},
    # 6. BOS_ALIGN_OFF — soft-stop fires even when aligned
    "BOS_ALIGN_OFF": {"bos_choch_ignore_invert_when_aligned": False},
    # 7. FVG_INV_STRUCT_ON — kill opposing FVGs on BoS/CHoCH (untested!)
    "FVG_INV_STRUCT_ON": {
        "fvg_invalidate_on_structure": True,
        "fvg_structure_invalidation_age_secs": 300,  # default
    },
    # 8. FVG_INV_STRUCT_1800 — wider window (30 min)
    "FVG_INV_STRUCT_1800": {
        "fvg_invalidate_on_structure": True,
        "fvg_structure_invalidation_age_secs": 1800,
    },
}


def _make_config(name: str) -> TrendStrategyParams:
    p = _common()
    for k, v in CONFIGURATIONS[name].items():
        setattr(p, k, v)
    return p


# ── Data loading (pyarrow, same pattern as run_sniper_breadth.py) ─────
data_dir = ROOT / 'data'
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
print(f'Day enumeration: {time.time() - t0:.1f}s, {len(unique_days):,} Mon-Fri days',
      flush=True)

rng = np.random.default_rng(RNG_SEED)
n_sample = min(N_DAYS, len(unique_days))
sample_days = rng.choice(unique_days, size=n_sample, replace=False)
sample_days.sort()
print(f'Sampled {n_sample} days with seed {RNG_SEED}', flush=True)

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
print(f'Pre-load: {time.time() - t_pre:.1f}s, {len(day_data)} days', flush=True)


# ── Run + summarize ─────────────────────────────────────────────────────
def _run_one(cfg_name: str, day_label: str, df_day: pd.DataFrame) -> dict:
    p = _make_config(cfg_name)
    buf = io.StringIO()
    err = ""
    try:
        with contextlib.redirect_stdout(buf):
            res = run_ict_backtest(df_day, p, strategy_label=f'v10_{cfg_name}')
    except Exception as e:
        err = str(e)
        res = None
    if res is None:
        return {
            'config': cfg_name, 'day': day_label,
            'n_trades': 0, 'pnl_total': 0.0, 'ev_per_trade': 0.0,
            'n_sniper_submitted': 0, 'n_sniper_triggered': 0,
            'n_sniper_cancelled': 0, 'n_sniper_expired': 0,
            'n_soft_stops': 0, 'n_inversions': 0,
            'n_signals_emitted': 0, 'n_fills': 0,
            'n_alignment_skipped': 0,
            'n_entry_aligned': 0, 'n_entry_opposed': 0, 'n_entry_unknown': 0,
            'n_structure_invalidations': 0,
            'error': err,
        }
    n_t = len(res.trades)
    pnl = float(sum(t.pnl_usd for t in res.trades))
    ev = pnl / n_t if n_t > 0 else 0.0
    return {
        'config': cfg_name, 'day': day_label,
        'n_trades': n_t, 'pnl_total': pnl, 'ev_per_trade': ev,
        'n_sniper_submitted': res.n_sniper_submitted,
        'n_sniper_triggered': res.n_sniper_triggered,
        'n_sniper_cancelled': res.n_sniper_cancelled,
        'n_sniper_expired': res.n_sniper_expired,
        'n_soft_stops': res.n_soft_stops,
        'n_inversions': res.n_inversions_detected,
        'n_signals_emitted': res.n_signals_emitted,
        'n_fills': res.n_fills,
        'n_alignment_skipped': res.n_alignment_skipped_inversions,
        'n_entry_aligned': res.n_entry_alignment_aligned,
        'n_entry_opposed': res.n_entry_alignment_opposed,
        'n_entry_unknown': res.n_entry_alignment_unknown,
        'n_structure_invalidations': res.n_structure_invalidations,
        'error': err,
    }


configs = list(CONFIGURATIONS.keys())
nb_dir = ROOT / 'notebooks'
csv_path = nb_dir / 'market_structure_v2_per_day.csv'
total_jobs = len(configs) * len(day_data)
print(f'\nRunning {total_jobs} backtests ({len(configs)} configs x {len(day_data)} days)...',
      flush=True)

t_start = time.time()
all_runs = []
job_idx = 0
for cfg_name in configs:
    print(f'\n=== {cfg_name} ===', flush=True)
    for day_label, df_day in day_data.items():
        job_idx += 1
        if job_idx % 30 == 0:
            elapsed = time.time() - t_start
            eta = elapsed * (total_jobs - job_idx) / job_idx
            print(f'  [{job_idx}/{total_jobs}] {cfg_name} {day_label} '
                  f'elapsed={elapsed:.1f}s eta={eta:.1f}s',
                  flush=True)
        row = _run_one(cfg_name, day_label, df_day)
        all_runs.append(row)

# ── Aggregate ───────────────────────────────────────────────────────────
df = pd.DataFrame(all_runs)
df.to_csv(csv_path, index=False)
print(f'\nPer-day CSV: {csv_path} ({len(df)} rows)', flush=True)

# Sort by EV/trade desc for the headline table
config_order = sorted(
    df['config'].unique(),
    key=lambda c: (
        df.loc[df['config'] == c, 'pnl_total'].sum()
        / max(df.loc[df['config'] == c, 'n_trades'].sum(), 1)
    ),
    reverse=True,
)

print(f'\n=== V10 BoS/CHoCH TUNING ON SNIPER BASE — '
      f'{len(day_data)} Mon-Fri UTC days, seed {RNG_SEED} ===', flush=True)
print('=' * 125)
hdr = (
    f'{"config":<22} {"trades":>7} {"EV/trade":>10} {"PnL/day":>10} '
    f'{"tr/day":>7} {"sigs":>5} {"fills":>5} {"aligned":>8} {"opposed":>8} '
    f'{"struct_inv":>10} {"soft":>5}'
)
print(hdr)
print('-' * 125)
for cfg_name in config_order:
    g = df[df['config'] == cfg_name]
    n_tr = int(g['n_trades'].sum())
    pnl = float(g['pnl_total'].sum())
    ev = pnl / n_tr if n_tr else 0.0
    pnl_day = pnl / len(day_data)
    tr_day = n_tr / len(day_data)
    n_aligned = int(g['n_entry_aligned'].sum())
    n_opposed = int(g['n_entry_opposed'].sum())
    n_struct = int(g['n_structure_invalidations'].sum())
    print(
        f'{cfg_name:<22} {n_tr:>7} ${ev:>+8.4f} ${pnl_day:>+8.4f} '
        f'{tr_day:>7.1f} '
        f'{int(g["n_signals_emitted"].sum()):>5} '
        f'{int(g["n_fills"].sum()):>5} '
        f'{n_aligned:>8} {n_opposed:>8} '
        f'{n_struct:>10} '
        f'{int(g["n_soft_stops"].sum()):>5}'
    )

print()
print(f'Total elapsed: {time.time() - t_start:.1f}s')

# ── Delta vs SNIPER_BASE ────────────────────────────────────────────────
print()
print('=== DELTA vs SNIPER_BASE (v7 OPTIMAL baseline) ===')
print('-' * 90)
ref = df[df['config'] == 'SNIPER_BASE']
ref_pnl = float(ref['pnl_total'].sum())
ref_n = int(ref['n_trades'].sum())
ref_ev = ref_pnl / ref_n if ref_n else 0.0
for cfg_name in config_order:
    g = df[df['config'] == cfg_name]
    pnl = float(g['pnl_total'].sum())
    n_t = int(g['n_trades'].sum())
    ev = pnl / n_t if n_t else 0.0
    d_ev = ev - ref_ev
    d_pnl_day = (pnl / len(day_data)) - (ref_pnl / len(day_data))
    is_test = cfg_name != 'SNIPER_BASE'
    flag = ' <-- POSITIVE EV' if ev > 0 else ''
    flag2 = ' <-- STACK WIN' if (
        is_test and ev > ref_ev
    ) else ''
    print(
        f'{cfg_name:<22} dEV=${d_ev:>+7.4f}  dPnL/day=${d_pnl_day:>+8.4f}{flag}{flag2}'
    )

# ── Per-day stats ───────────────────────────────────────────────────────
print()
print('=== Per-day PnL distribution (sorted by mean PnL/day) ===')
print('-' * 90)
config_by_mean = sorted(
    df['config'].unique(),
    key=lambda c: df.loc[df['config'] == c, 'pnl_total'].mean(),
    reverse=True,
)
for cfg_name in config_by_mean:
    g = df[df['config'] == cfg_name]
    pnls = g['pnl_total']
    se = pnls.std() / (len(pnls) ** 0.5)
    t = pnls.mean() / se if se > 0 else 0.0
    print(
        f'{cfg_name:<22}  mean=${pnls.mean():+8.2f}/d  '
        f'std=${pnls.std():7.2f}  se=${se:6.2f}  '
        f't={t:+5.2f}  '
        f'pos={(pnls>0).sum():>3}/{len(pnls)}  neg={(pnls<0).sum():>3}/{len(pnls)}'
    )

# ── Summary CSV ─────────────────────────────────────────────────────────
summary_rows = []
for cfg_name in configs:
    g = df[df['config'] == cfg_name]
    n_tr = int(g['n_trades'].sum())
    pnl = float(g['pnl_total'].sum())
    pnls = g['pnl_total']
    se = pnls.std() / (len(pnls) ** 0.5)
    summary_rows.append({
        'config': cfg_name,
        'n_days': len(day_data),
        'trades': n_tr,
        'pnl_total': pnl,
        'ev_per_trade': pnl / n_tr if n_tr else 0.0,
        'pnl_per_day': pnl / len(day_data),
        'trades_per_day': n_tr / len(day_data),
        'pnl_per_day_se': se,
        'pnl_per_day_t': (pnls.mean() / se) if se > 0 else 0.0,
        'pct_pos_days': 100 * (pnls > 0).sum() / len(pnls),
        'sniper_submitted': int(g['n_sniper_submitted'].sum()),
        'sniper_triggered': int(g['n_sniper_triggered'].sum()),
        'sniper_cancelled': int(g['n_sniper_cancelled'].sum()),
        'sniper_expired': int(g['n_sniper_expired'].sum()),
        'signals_emitted': int(g['n_signals_emitted'].sum()),
        'fills': int(g['n_fills'].sum()),
        'soft_stops': int(g['n_soft_stops'].sum()),
        'inversions': int(g['n_inversions'].sum()),
        'n_alignment_skipped': int(g['n_alignment_skipped'].sum()),
        'n_entry_aligned': int(g['n_entry_aligned'].sum()),
        'n_entry_opposed': int(g['n_entry_opposed'].sum()),
        'n_entry_unknown': int(g['n_entry_unknown'].sum()),
        'n_structure_invalidations': int(g['n_structure_invalidations'].sum()),
    })
summary_df = pd.DataFrame(summary_rows)
summary_df.to_csv(nb_dir / 'market_structure_v2_summary.csv', index=False)
print(f'\nSummary CSV: {nb_dir / "market_structure_v2_summary.csv"}')
