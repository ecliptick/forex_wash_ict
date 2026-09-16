# ict_tier_v2

ICT-only trading research fork. Continuation / cleanup of the ICT path in
[`gmma_guppy_ict/`](../gmma_guppy_ict/). As of 2026-09-17 the
classification systems that originally distinguished v2 from the parent
repo have been **removed entirely** (look-forward bias — they read
end-of-life zone fields or compared a zone against future zones in the
same UTC day).

## What's different from the original

v2 carried two unrelated classification axes on `PendingSignal` /
`Trade` over its lifecycle (2026-09-15 → 2026-09-17):

| Axis | Field | Labels | Status |
|---|---|---|---|
| Candlestick pattern (per-bar 3-bar + structure alignment) | `candle_quality` | `HUNT` / `STRONG_ALIGNED` / `TRUE_DISPLACEMENT` / `MARGINAL` | **REMOVED 2026-09-16** |
| Day-bucketed price-rank percentile | `rank_tier` | `A` / `B` / `C` | **REMOVED 2026-09-17** |
| Causal rolling ranker (replacement) | `rank_tier` (re-populated by `RollingFvgRanker`) | `A` / `B` / `C` | **ADDED 2026-09-17** |
| Signal source | `triggered_by` | `fvg` / `ifvg` / `orb` / `wyckoff` / `sweep` | unchanged |

The current (v5) state has one tier-classification axis (`rank_tier`,
populated causally by `RollingFvgRanker` against the past N
same-direction zones) plus the existing `triggered_by` signal-source
field. See [`AGENTS.md`](AGENTS.md) → "v3 → v5 redesign" for the full
removal history and the surviving `fvg_rolling_*` knobs.

## Quick start

```bash
python -m venv .venv
. .venv/Scripts/activate  # Windows
pip install -r requirements.txt

# Run the test suite (56 tests)
PYTHONPATH=. python -m pytest tests/ -x -q

# Smoke-test the backtest on 1 day of data
PYTHONPATH=. python -c "
import pandas as pd
from src.core.ict_strategy import TrendStrategyParams
from src.backtest.ict_backtest import run_ict_backtest
df = pd.read_parquet('data/XAUUSD_S1_1d.parquet')
p = TrendStrategyParams(ms_resample_secs=60)
result = run_ict_backtest(df, p)
print(result.summary())
"
```

## Layout

```
ict_tier_v2/
├── AGENTS.md                 # design source of truth (READ THIS FIRST)
├── README.md                 # this file
├── requirements.txt
├── data/
│   ├── XAUUSD_S1_1y.parquet   # primary 1y corpus (~617MB)
│   ├── XAUUSD_S1_1d.parquet   # 1-day smoke-test slice
│   └── XAUUSD_S1_sample.parquet  # 5k-bar test sample
├── notebooks/
│   ├── nb34_ict_softstop.py          # soft-stop validation (script)
│   ├── nb36_ict_renko_sweep.ipynb    # renko + sweep viz
│   ├── nb38_tier_study.py            # 200-day tier study (script — HISTORICAL; classifier removed 2026-09-17)
│   └── nb40_tier_ev.py               # EV-by-tier backtest (script — HISTORICAL; classifier removed 2026-09-17)
├── src/
│   ├── core/
│   │   ├── ict_signals.py     # FVG/iFVG/ORB/Wyckoff detectors + RollingFvgRanker
│   │   ├── ict_strategy.py    # TrendStrategyParams + PendingSignal + Trade
│   │   └── market_structure.py # BoS / CHoCH / order blocks
│   ├── backtest/
│   │   └── ict_backtest.py    # the bar loop + 3-layer orders + SL/TP
│   └── tools/
│       ├── run_nb38.py / run_nb40.py / run_nb41.py / run_nb42.py  # tier studies (HISTORICAL)
│       └── build_nb34.py / build_nb35.py / build_nb36.py           # notebook builders
└── tests/
    └── test_ict_fork.py       # 56 tests
```

## Tests

```bash
PYTHONPATH=. python -m pytest tests/ -x -q
```

The 56-test suite covers the FVG/iFVG/ORB/Wyckoff detectors,
renko bricks, the RollingFvgRanker causal ranker, and the
detector-to-bar-loop pipeline end-to-end.
