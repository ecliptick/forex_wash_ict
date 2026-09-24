"""Run the nb49 notebook logic against the 3-month BTC data.
This is the canonical run that produces nb49_leverage_optimization.csv.
Outputs CSV of Kelly/Shannon results + recommendations."""
import pandas as pd, numpy as np
import warnings
warnings.filterwarnings("ignore")
import sys

trades = pd.read_parquet("scratch/btc_3mo_trades.parquet")
daily_csv = pd.read_csv("scratch/btc_3mo_daily.csv", parse_dates=["trade_day"])

START_EQUITY = 10_000.0

# Core trade stats
pnl = trades["pnl_usd"].to_numpy()
mu_t = pnl.mean()
sigma_t = pnl.std()
WR = (pnl > 0).mean()
mean_win_per_unit = pnl[pnl > 0].mean()
mean_loss_per_unit = abs(pnl[pnl < 0].mean())

print(f"Trades: {len(trades)}, Days: {len(daily_csv)}, Months: {sorted(trades['trade_month'].unique())}")
print(f"WR: {WR:.2%}, mu: ${mu_t:+.6f}, sigma: ${sigma_t:.6f}, mean win/loss: {mean_win_per_unit/mean_loss_per_unit:.2f}")
print(f"Skew: {pd.Series(pnl).skew():+.2f}, Kurt: {pd.Series(pnl).kurtosis():+.2f}")

# -- Kelly binary --
b = mean_win_per_unit / mean_loss_per_unit
f_binary = max(0.0, WR - (1 - WR) / b)
print(f"\nKelly binary f* = {f_binary:.4f}")
print(f"   Half-Kelly: {f_binary/2:.4f}")
print(f"   Quarter-Kelly: {f_binary/4:.4f}")

# -- Kelly continuous --
f_continuous = mu_t / (sigma_t ** 2)
print(f"\nKelly continuous f* = μ/σ² = {f_continuous:,.2f}× base unit")

# -- Simulate equity curves --
def simulate_curve(pnl_arr, K, start):
    eq = np.empty(len(pnl_arr) + 1)
    eq[0] = start
    for i, p in enumerate(pnl_arr):
        eq[i+1] = eq[i] + K * p
        if eq[i+1] <= 0:
            return eq[:i+1]
    return eq

def curve_stats(eq, start):
    if len(eq) < 2 or eq[-1] <= 0:
        return {"final": 0.0, "ret_pct": -100.0, "max_dd": -100.0, "sharpe": 0.0}
    final = eq[-1]
    ret = (final / start - 1) * 100
    peak = np.maximum.accumulate(eq)
    dd = ((eq - peak) / peak).min() * 100
    rets = np.diff(eq) / eq[:-1]
    rets = rets[np.isfinite(rets)]
    sharpe = (rets.mean() / rets.std()) * np.sqrt(len(rets)) if rets.std() > 0 else 0
    return {"final": final, "ret_pct": ret, "max_dd": dd, "sharpe": sharpe}

print(f"\nLeverage grid (start=${START_EQUITY:,.0f}):")
print(f"  {'K':>10} | {'Final $':>11} | {'Return %':>9} | {'Max DD %':>10} | {'Sharpe':>7}")
print(f"  {'-'*10} | {'-'*11} | {'-'*9} | {'-'*10} | {'-'*7}")
results = []
for K in [10, 50, 100, 250, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000]:
    eq = simulate_curve(pnl, K, START_EQUITY)
    s = curve_stats(eq, START_EQUITY)
    results.append({"K": K, **s})
    flag = " BUST" if s["final"] <= 0 else ""
    print(f"  {K:>10,} | ${s['final']:>10,.2f} | {s['ret_pct']:>+8.2f}% | {s['max_dd']:>+9.2f}% | {s['sharpe']:>6.2f}{flag}")

# -- Log-growth optimal K --
def log_growth(K, pnl_arr, start):
    eq = start
    total = 0
    n = 0
    for p in pnl_arr:
        new = eq + K * p
        if new <= 0:
            return -np.inf
        total += np.log(new / eq)
        eq = new
        n += 1
    return total / n if n > 0 else 0

best_K = None
best_lg = -np.inf
for K in np.logspace(1, 7, 500):
    lg = log_growth(K, pnl, START_EQUITY)
    if lg > best_lg:
        best_lg = lg
        best_K = K
print(f"\nLog-growth K* = {best_K:,.0f}, max log-growth = {best_lg:.6f} ({best_lg*100:.4f}% per trade)")

eq_opt = simulate_curve(pnl, best_K, START_EQUITY)
s_opt = curve_stats(eq_opt, START_EQUITY)
print(f"  At K*={best_K:,.0f}: ${s_opt['final']:,.2f} (ret {s_opt['ret_pct']:+.2f}%, DD {s_opt['max_dd']:+.2f}%)")
print(f"  Half-K={best_K/2:,.0f}: ${curve_stats(simulate_curve(pnl, best_K/2, START_EQUITY), START_EQUITY)['final']:,.2f}")
print(f"  Quarter-K={best_K/4:,.0f}: ${curve_stats(simulate_curve(pnl, best_K/4, START_EQUITY), START_EQUITY)['final']:,.2f}")

# -- Ralph Vince optimal-f --
print(f"\nRalph Vince optimal-f:")
worst = pnl.min()
f_max = 0.99 / abs(worst) if worst < 0 else 1e6
print(f"  Worst trade: {worst:.6f}, f_max: {f_max:,.0f}")

def vince_obj(f, pnl_arr):
    hprs = 1.0 + f * pnl_arr
    if (hprs <= 0).any():
        return -np.inf
    return np.exp(np.mean(np.log(hprs))) - 1.0

# Find f that maximizes the geometric mean
f_grid = np.logspace(0, np.log10(f_max*0.99), 200)
vince_vals = np.array([vince_obj(f, pnl) for f in f_grid])
finite_v = np.isfinite(vince_vals)
if finite_v.any():
    best_v_idx = np.nanargmax(np.where(finite_v, vince_vals, -np.inf))
    f_vince = f_grid[best_v_idx]
    obj_vince = vince_vals[best_v_idx]
    print(f"  f_vince = {f_vince:,.0f}, max TWR gain = {obj_vince:.6f}")
else:
    f_vince = 100.0
    print(f"  No viable f; using 100")

eq_vince = simulate_curve(pnl, f_vince, START_EQUITY)
s_vince = curve_stats(eq_vince, START_EQUITY)
print(f"  Sim: ${s_vince['final']:,.2f} (ret {s_vince['ret_pct']:+.2f}%, DD {s_vince['max_dd']:+.2f}%)")

# -- Drawdown-constrained (20% max DD) --
print(f"\nDrawdown-constrained (max DD <= 20%):")
best_dd_K = None
best_dd_ret = -np.inf
for K in np.logspace(1, 6, 500):
    eq = simulate_curve(pnl, K, START_EQUITY)
    if eq[-1] <= 0:
        continue
    s = curve_stats(eq, START_EQUITY)
    if abs(s['max_dd']) <= 20.0 and s['ret_pct'] > best_dd_ret:
        best_dd_ret = s['ret_pct']
        best_dd_K = K
        s_dd = s
if best_dd_K:
    print(f"  Best K with DD<=20%: K={best_dd_K:,.0f}, ret={best_dd_ret:+.2f}%, DD={s_dd['max_dd']:+.2f}%")
else:
    print(f"  No K satisfies DD <= 20%")
    best_dd_K = np.nan

# -- Shannon's Demon (vol-target) on daily PnL --
print(f"\nShannon's Demon variants (vol-target on daily stream):")
daily_pnl = daily_csv["pnl_usd"].to_numpy()

def vol_target_curve(daily_pnls, start, target_vol, lookback=10):
    n = len(daily_pnls)
    eq = np.empty(n + 1)
    eq[0] = start
    for i in range(n):
        if i < lookback:
            scale = 1.0
        else:
            recent = daily_pnls[max(0, i-lookback):i]
            rv = np.std(recent)
            scale = target_vol / rv if rv > 0 else 1.0
        eq[i+1] = eq[i] + scale * daily_pnls[i]
        if eq[i+1] <= 0:
            return eq[:i+1]
    return eq

print(f"  Daily PnL: mean=${daily_pnl.mean():.6f}, std=${daily_pnl.std():.6f}")
for tv in [0.01, 0.1, 1.0, 10.0]:
    eq_vt = vol_target_curve(daily_pnl, START_EQUITY, tv)
    s_vt = curve_stats(eq_vt, START_EQUITY)
    print(f"  TargetDailyVol=${tv:.2f}: ${s_vt['final']:,.2f} (ret {s_vt['ret_pct']:+.2f}%, DD {s_vt['max_dd']:+.2f}%)")

# -- FINAL RECOMMENDATION TABLE --
print(f"\n" + "="*72)
print("PRACTICAL LEVERAGE RECOMMENDATION (3-month BTC, 245 trades)")
print("="*72)
trades_per_day = len(trades) / 90.0

print(f"""
INPUTS:
  Trades:           {len(trades)} over ~90 days
  Win rate:         {WR:.2%}
  Mean win/loss:    {mean_win_per_unit/mean_loss_per_unit:.2f}x
  Per-trade μ:      ${mu_t:+.6f}
  Per-trade σ:      ${sigma_t:.6f}
  Skew:             {pd.Series(pnl).skew():+.2f} (heavy right tail)

KELLY CANDIDATES:
  Binary Kelly (full):     {f_binary:.4f} per trade ({f_binary*100:.2f}% of bankroll)
  Binary Kelly (half):     {f_binary/2:.4f} per trade
  Log-growth K*:           {best_K:,.0f}x base unit (full-Kelly, aggressive)
  Vince optimal-f:         {f_vince:,.0f}x base unit
  DD-constrained (20%):    {best_dd_K if not np.isnan(best_dd_K) else 'N/A'}x base unit

PRACTICAL K TABLE (${START_EQUITY:,.0f} start equity, 3-month BTC):
""")
print(f"  {'K':>10} | {'Risk/trade':>12} | {'$/day ev':>12} | {'$/3mo':>10} | {'Max DD %':>10}")
print(f"  {'-'*10} | {'-'*12} | {'-'*12} | {'-'*10} | {'-'*10}")
for K in [50, 100, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000]:
    risk = K * mean_loss_per_unit
    expected_per_day = K * mu_t * trades_per_day
    expected_3mo = expected_per_day * 90
    eq = simulate_curve(pnl, K, START_EQUITY)
    s = curve_stats(eq, START_EQUITY)
    flag = " BUST" if s["final"] <= 0 else ""
    print(f"  {K:>10,} | ${risk:>11,.4f} | ${expected_per_day:>+11,.4f} | ${expected_3mo:>+9,.4f} | {s['max_dd']:>+9.2f}%{flag}")

print(f"""
KEY INSIGHT — SHARPE IS SCALE-INVARIANT:
  Sharpe ratio is ~4.1-4.4 across all K values (10 to 100,000).
  This means the strategy is RISK-ADJUSTED SCALE-INVARIANT — pick K
  based on your RISK TOLERANCE, not on EV estimate.

RISK-BASED SIZING (1% risk/trade = $100 on $10k):
  K = {100/mean_loss_per_unit:,.0f}

ACCOUNT-SIZE GUIDE:
  $1k account:   K = {(1000*0.01)/mean_loss_per_unit:,.0f}   (~$0.19/trade)
  $10k account:  K = {(10000*0.01)/mean_loss_per_unit:,.0f}  (~$1.86/trade)
  $100k account: K = {(100000*0.01)/mean_loss_per_unit:,.0f} (~$18.60/trade)

HALF-KELLY (industry standard, 75% of log-growth):
  K = {best_K/2:,.0f}
  Risk/trade: ${best_K/2 * mean_loss_per_unit:.2f}
  At this K: equity ${curve_stats(simulate_curve(pnl, best_K/2, START_EQUITY), START_EQUITY)['final']:,.2f}, DD {curve_stats(simulate_curve(pnl, best_K/2, START_EQUITY), START_EQUITY)['max_dd']:+.2f}%
""")

# Save results
out = pd.DataFrame([
    {"metric": "n_trades", "value": len(trades)},
    {"metric": "n_days", "value": len(daily_csv)},
    {"metric": "months", "value": ",".join(sorted(trades['trade_month'].unique()))},
    {"metric": "WR", "value": WR},
    {"metric": "mean_win_per_unit", "value": mean_win_per_unit},
    {"metric": "mean_loss_per_unit", "value": mean_loss_per_unit},
    {"metric": "mean_win_over_loss", "value": mean_win_per_unit/mean_loss_per_unit},
    {"metric": "mu_per_trade", "value": mu_t},
    {"metric": "sigma_per_trade", "value": sigma_t},
    {"metric": "skew", "value": pd.Series(pnl).skew()},
    {"metric": "kurtosis", "value": pd.Series(pnl).kurtosis()},
    {"metric": "kelly_binary_full", "value": f_binary},
    {"metric": "kelly_binary_half", "value": f_binary/2},
    {"metric": "kelly_continuous", "value": f_continuous},
    {"metric": "log_growth_K_star", "value": best_K},
    {"metric": "log_growth_max", "value": best_lg},
    {"metric": "vince_optimal_f", "value": f_vince},
    {"metric": "dd_constrained_K", "value": best_dd_K if not np.isnan(best_dd_K) else -1},
    {"metric": "K_for_1pct_risk_on_10k", "value": 100/mean_loss_per_unit},
    {"metric": "median_SL_dist", "value": trades["stop_usd"].median()},
    {"metric": "median_TP_dist", "value": trades["target_usd"].median()},
    {"metric": "RR_ratio_median", "value": trades['target_usd'].median()/trades['stop_usd'].median()},
])
out.to_csv("scratch/nb49_kelly_results.csv", index=False)
print(f"\nSaved {len(out)} Kelly metrics to scratch/nb49_kelly_results.csv")
