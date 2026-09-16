"""Quick A/B test of the new BOS/CHoCH alignment rule on N days.

Compares:
  * OFF: bos_choch_ignore_invert_when_aligned=False (legacy: soft-stop always fires on inversion)
  * ON:  bos_choch_ignore_invert_when_aligned=True  (new: soft-stop suppressed when aligned)
"""
import sys
import time
import pandas as pd
import numpy as np
import pyarrow.parquet as pq

sys.path.insert(0, '.')
from src.core.ict_strategy import TrendStrategyParams
from src.backtest.ict_backtest import run_ict_backtest


def main(n_days: int = 30) -> int:
    parquet_path = 'data/XAUUSD_S1_1y.parquet'
    # Read just the time column to enumerate UTC days. The time
    # column is stored as timestamp[ms, tz=UTC] in pyarrow, so
    # pandas reads it as datetime64[ms, UTC].
    t_table = pq.read_table(parquet_path, columns=['time'])
    t_np = t_table['time'].to_numpy(zero_copy_only=False)
    NS_PER_DAY = 86_400_000_000_000
    # Convert via .astype('datetime64[ns]') so the underlying int64
    # is in nanoseconds, then divide.
    t_int_ns = t_np.astype('datetime64[ns]').astype(np.int64)
    unique_days = np.unique(t_int_ns // NS_PER_DAY)
    print(f'Enumerated {len(unique_days):,} UTC days', flush=True)
    np.random.seed(42)
    random_days = np.random.choice(unique_days, size=min(n_days, len(unique_days)), replace=False)
    random_days.sort()
    print(f'Sampled {len(random_days)} days', flush=True)

    # Pre-load all days once.
    print('Pre-loading...', flush=True)
    t_pre = time.time()
    day_data: dict[int, pd.DataFrame] = {}
    df_full = pd.read_parquet(parquet_path)
    print(f'Loaded {len(df_full):,} rows', flush=True)
    # Time is datetime64[ms, UTC] — convert to ns first.
    df_full['day'] = (
        df_full['time'].dt.tz_convert('UTC').dt.tz_localize(None)
        .astype('datetime64[ns]').astype('int64').to_numpy()
        // NS_PER_DAY
    )
    for d in random_days:
        ddf = df_full[df_full['day'] == d].sort_values('time').drop(columns=['day']).reset_index(drop=True)
        if len(ddf) >= 1000:
            day_data[int(d)] = ddf
    print(f'Pre-load: {time.time() - t_pre:.1f}s, {len(day_data)} valid days', flush=True)

    p_off = TrendStrategyParams(bos_choch_ignore_invert_when_aligned=False)
    p_on = TrendStrategyParams(bos_choch_ignore_invert_when_aligned=True)

    results = {'OFF': [], 'ON': []}
    t0 = time.time()
    for di, (d, ddf) in enumerate(day_data.items()):
        for label, params in [('OFF', p_off), ('ON', p_on)]:
            r = run_ict_backtest(ddf, params)
            results[label].append({
                'day': int(d),
                'n_trades': len(r.trades),
                'pnl': sum(t.pnl_usd for t in r.trades),
                'n_soft_stops': r.n_soft_stops,
                'n_alignment_skipped': r.n_alignment_skipped_inversions,
                'n_aligned_entries': r.n_entry_alignment_aligned,
                'n_opposed_entries': r.n_entry_alignment_opposed,
                'n_unknown_entries': r.n_entry_alignment_unknown,
            })
        if (di + 1) % 5 == 0:
            print(f'  [{di+1}/{len(day_data)}] elapsed {time.time()-t0:.1f}s', flush=True)

    print(f'\n=== A/B test results ({len(results["OFF"])} days, elapsed {time.time()-t0:.1f}s) ===\n', flush=True)
    for label, rs in results.items():
        n_t = sum(r['n_trades'] for r in rs)
        pnl = sum(r['pnl'] for r in rs)
        soft = sum(r['n_soft_stops'] for r in rs)
        skip = sum(r['n_alignment_skipped'] for r in rs)
        ali = sum(r['n_aligned_entries'] for r in rs)
        opp = sum(r['n_opposed_entries'] for r in rs)
        unk = sum(r['n_unknown_entries'] for r in rs)
        print(f'{label}: trades={n_t}, PnL=${pnl:+.2f} (${pnl/len(rs):+.2f}/day), '
              f'EV=${pnl/max(1,n_t):+.4f}, '
              f'soft_stops={soft}, skipped_inv={skip}, '
              f'aligned={ali}, opposed={opp}, unknown={unk}', flush=True)
    delta = sum(r['pnl'] for r in results['ON']) - sum(r['pnl'] for r in results['OFF'])
    print(f'\nDelta PnL ON-OFF: ${delta:+.2f} (${delta/len(results["OFF"]):+.2f}/day)', flush=True)
    return 0


def pandas_time_int64_type():
    """Cast helper: pyarrow may store time as int64 ms or ns depending on version."""
    import pyarrow as pa
    return pa.schema([pa.field('time', pa.int64())])


if __name__ == '__main__':
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 30))

