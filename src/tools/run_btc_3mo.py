"""3-month BTC smoke: pick 3 random months from available 20, run backtest,
save per-trade + daily PnL streams for nb49.
"""
import sys, time, random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import polars as pl

from src.core.optimal_config import optimal_params
from src.backtest.ict_backtest import run_ict_backtest

AVAILABLE = [
    "2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06",
    "2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12",
    "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
    "2026-07", "2026-08",
]

random.seed(20260918)
picked = sorted(random.sample(AVAILABLE, 3))
print(f"Picked: {picked}", flush=True)

BARS_DIR = ROOT / "data" / "binance_um_aggtrades" / "BTCUSDT" / "bars"

dfs = []
for m in picked:
    fp = BARS_DIR / f"BTCUSDT-bars-{m}-1s.parquet"
    print(f"  {fp.name} ({fp.stat().st_size / 1e6:.1f} MB)...", flush=True)
    t0 = time.time()
    df = pl.read_parquet(fp).select(["ts", "open", "high", "low", "close", "volume"])
    df = df.rename({"ts": "time"}).to_pandas()
    df["time"] = pd.to_datetime(df["time"], utc=True)
    print(f"    -> {len(df):,} bars in {time.time()-t0:.1f}s", flush=True)
    dfs.append(df)

df_3mo = pd.concat(dfs, ignore_index=True)
print(f"\nCombined: {len(df_3mo):,} bars over 3 months", flush=True)
print(f"Range:    {df_3mo['time'].min()} -> {df_3mo['time'].max()}", flush=True)

p = optimal_params(contract_size=0.001)
print(f"\nRecipe: contract_size={p.contract_size}", flush=True)
print("Running backtest...", flush=True)
t0 = time.time()
result = run_ict_backtest(df_3mo, p, strategy_label="btc_3mo_v7")
elapsed = time.time() - t0
print(f"  Elapsed: {elapsed:.1f}s ({len(df_3mo)/elapsed:,.0f} bars/s)", flush=True)

s = result.summary()
print(f"\n  Trades:     {s['n_trades']:,}", flush=True)
print(f"  Win rate:   {s['win_rate']:.1%}", flush=True)
print(f"  Trades/day: {s['trades_per_day']:.1f}", flush=True)

trades_df = result.trades_df()
trades_df["entry_dt"] = pd.to_datetime(trades_df["entry_time"], unit="ns", utc=True)
trades_df["exit_dt"] = pd.to_datetime(trades_df["exit_time"], unit="ns", utc=True)
trades_df["trade_day"] = trades_df["entry_dt"].dt.strftime("%Y-%m-%d")
trades_df["trade_month"] = trades_df["entry_dt"].dt.strftime("%Y-%m")
out = ROOT / "scratch" / "btc_3mo_trades.parquet"
trades_df.to_parquet(out, index=False)
print(f"\nSaved {len(trades_df):,} trades to {out}", flush=True)

# Daily PnL stream (for Kelly/Shannon analysis)
daily = trades_df.groupby("trade_day").agg(
    pnl_usd=("pnl_usd", "sum"),
    n_trades=("pnl_usd", "count"),
    avg_pnl=("pnl_usd", "mean"),
    std_pnl=("pnl_usd", "std"),
).reset_index()
daily["trade_day"] = pd.to_datetime(daily["trade_day"])
daily = daily.sort_values("trade_day").reset_index(drop=True)
daily_csv = ROOT / "scratch" / "btc_3mo_daily.csv"
daily.to_csv(daily_csv, index=False)
print(f"Saved {len(daily)} daily rows to {daily_csv}", flush=True)

meta = ROOT / "scratch" / "btc_3mo_meta.txt"
with open(meta, "w") as f:
    f.write(f"months={picked}\nseed=20260918\n")
    f.write(f"n_trades={s['n_trades']}\nwin_rate={s['win_rate']:.4f}\n")
    f.write(f"ev_per_trade_lots001={s['ev_per_trade']:.6f}\n")
    f.write(f"total_pnl_lots001={trades_df['pnl_usd'].sum():.6f}\n")
    f.write(f"median_sl={trades_df['stop_usd'].median():.4f}\n")
    f.write(f"median_tp={trades_df['target_usd'].median():.4f}\n")
print(f"Saved meta to {meta}", flush=True)
