"""1-month smoke first to validate timing + memory."""
import sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import polars as pl

from src.core.optimal_config import optimal_params
from src.backtest.ict_backtest import run_ict_backtest

fp = ROOT / "data" / "binance_um_aggtrades" / "BTCUSDT" / "bars" / "BTCUSDT-bars-2025-10-1s.parquet"
print(f"Loading {fp.name}...")
df = pl.read_parquet(fp).select(["ts", "open", "high", "low", "close", "volume"])
df = df.rename({"ts": "time"}).to_pandas()
df["time"] = pd.to_datetime(df["time"], utc=True)
print(f"  {len(df):,} bars")
print(f"  Range: {df['time'].min()} -> {df['time'].max()}")
print(f"  Mean price: ${df['close'].mean():,.2f}")

p = optimal_params(contract_size=0.001)
print(f"\nRecipe: contract_size={p.contract_size}, lots={p.lots}")

print("\nRunning backtest...")
t0 = time.time()
result = run_ict_backtest(df, p, strategy_label="btc_1mo_v7")
elapsed = time.time() - t0
print(f"  Elapsed: {elapsed:.1f}s ({len(df)/elapsed:,.0f} bars/s)")

s = result.summary()
print(f"\n  Trades:     {s['n_trades']:,}")
print(f"  EV/trade:   ${s['ev_per_trade']:+.4f}")
print(f"  PnL/day:    ${s['pnl_per_trade']*s['trades_per_day']:+.2f}")
print(f"  Win rate:   {s['win_rate']:.1%}")
print(f"  Trades/day: {s['trades_per_day']:.1f}")
print(f"  Avg win:    ${s['avg_win']:.2f}")
print(f"  Avg loss:   ${s['avg_loss']:.2f}")

# Save per-trade for nb49 reuse
trades_df = result.trades_df()
trades_df["entry_dt"] = pd.to_datetime(trades_df["entry_time"], unit="ns", utc=True)
trades_df["exit_dt"] = pd.to_datetime(trades_df["exit_time"], unit="ns", utc=True)
trades_df["trade_day"] = trades_df["entry_dt"].dt.strftime("%Y-%m-%d")
trades_df["month"] = "2025-10"
out_fp = ROOT / "scratch" / "btc_1mo_trades.parquet"
trades_df.to_parquet(out_fp, index=False)
print(f"\nSaved {len(trades_df):,} trades to {out_fp}")
