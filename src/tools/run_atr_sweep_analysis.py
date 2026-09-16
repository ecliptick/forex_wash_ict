"""ATR-anchor sweep: detailed breakeven analysis.

For each config, compute:
  - breakeven_win_rate = avg_loss / (avg_win - avg_loss)
    (the win rate at which EV/trade = 0)
  - current vs breakeven gap
  - if widening SL just moves the breakeven line

This tells us where the strategy is wedged.
"""
from __future__ import annotations
import pandas as pd
from pathlib import Path

ROOT = Path('.').resolve()
if not (ROOT / 'src' / 'tools').is_file():
    for p in [ROOT, *ROOT.parents]:
        if (p / 'src' / 'tools').is_file():
            ROOT = p
            break

df = pd.read_csv(ROOT / 'notebooks' / 'atr_anchor_sweep_summary.csv')
df['breakeven_wr'] = df['avg_loss'].abs() / (df['avg_win'] + df['avg_loss'].abs())
df['wr_minus_be'] = df['win_rate'] - df['breakeven_wr']
# Margin: how far above the breakeven line we currently sit (positive = above)

print('=' * 110)
print(f'{"Config":<28} {"WR":>6} {"BE_WR":>6} {"gap":>6} {"avg_win":>9} {"avg_loss":>9} {"EV/trade":>10}')
print('=' * 110)
for _, r in df.iterrows():
    flag = ''
    if r['wr_minus_be'] > 0.005:
        flag = ' (+EV)'
    elif r['wr_minus_be'] > -0.005:
        flag = ' (breakeven)'
    else:
        flag = ' (-EV)'
    print(f'{r["config"]:<28} {r["win_rate"]*100:>5.1f}% {r["breakeven_wr"]*100:>5.1f}% '
          f'{r["wr_minus_be"]*100:>+5.1f}%p '
          f'${r["avg_win"]:>+7.3f} ${r["avg_loss"]:>+7.3f} '
          f'${r["ev_per_trade"]:>+8.4f}{flag}')

# Pattern search: what does the breakeven WR look like as SL widens?
print('\n' + '=' * 110)
print('BREAKEVEN STRUCTURE: how the constraint depends on (avg_win, avg_loss)')
print('=' * 110)
print('Breakeven WR = |avg_loss| / (avg_win + |avg_loss|)')
print('  - avg_win: $-per-win (set by TP at ATR-anchor)')
print('  - avg_loss: $-per-loss (set by SL at ATR-anchor + extras)')
print()
print('Note: as SL widens, avg_loss grows (closer to the full stop distance), so '
      'the breakeven WR moves UP not down. So widening SL alone can never '
      'cross into positive EV if avg_win scales at the same rate.')
print()
print('The TRUE lever is the EXIT side:')
print('  - HOLD the trade longer when it is in profit (trailing or layer-wise)')
print('  - LIFT SL once in profit (trailing stop)')
print('  - Use the renko signal to detect "real" trend commitment before SL')

# Compute hypothetical: if avg_loss was capped at the v6 average ($0.10) but
# avg_win could be at the full TP ($0.33), what would the breakeven be?
print('\n' + '=' * 110)
print('HYPOTHETICAL: trailing SL that caps avg_loss at $0.10 (like v6 OPTIMAL)')
print('=' * 110)
avg_loss_hypothetical = 0.10
for _, r in df.iterrows():
    be = avg_loss_hypothetical / (r['avg_win'] + avg_loss_hypothetical)
    print(f'  {r["config"]:<28} WR={r["win_rate"]*100:>5.1f}%  '
          f'BE_WR(hyp)={be*100:>5.1f}%  '
          f'gap={r["win_rate"]-be:+.3f}  '
          f'EV/trade(hyp)=${(r["win_rate"]*r["avg_win"] - (1-r["win_rate"])*avg_loss_hypothetical):+.4f}')
