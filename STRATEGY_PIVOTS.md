# Strategy Pivots — ICT v2 Backtest Engine

**Date:** 2026-09-17  
**Repo:** `ict_tier_v2`  
**Status:** Backlog — to be implemented and validated in order

This document records the logical strategy pivots surfaced during the
2026-09-17 code audit. Each pivot changes the *shape* of the
strategy rather than tuning an existing knob. They are ranked by
prior. Bugs found in the engine are listed separately.

---

## Bugs Fixed (2026-09-17)

All bugs were in `src/backtest/ict_backtest.py` unless noted.

### Bug #1 — Limit-order fill price gave fictitious price improvement

**File:** `src/backtest/ict_backtest.py` lines 1117–1170  
**Severity:** Medium — makes the backtest look slightly better than live execution

**Problem:** Long limit orders filled at `b_open` when the bar opened
below the limit price:

```python
# BEFORE (buggy):
ep = target_price if b_open <= target_price else b_open  # free improvement!
```

A limit order at price P cannot receive a fill better than P — it can
only fill at P or worse (higher for a buy, lower for a sell). Using
`b_open` when `b_open < P` pretends the broker gave free price
improvement. On XAUUSD at $2000, one bar of gap-down can be $0.10+.
With thousands of fills, this materially biases PnL upward.

**Fix:** Fill limit orders at exactly `target_price`. Sweep
(stop-order) fills use `max/min(target, b_open)` — stops CAN be
filled worse than the trigger price when markets gap.

```python
# AFTER (fixed 2026-09-17):
is_sweep = (layer["triggered_by"] == "sweep")
if layer["direction"] > 0:
    if b_low <= target_price:
        if is_sweep:
            ep = max(target_price, b_open)  # stop: gap-up = worse fill
        else:
            ep = target_price               # limit: never better than limit
```

**Impact on past results:** Positive — all EV/trade figures were
slightly inflated. The effect is small per-trade (~$0.01–0.05)
but compounds over thousands of fills. Re-run all prior experiments
to get corrected baselines.

---

### Bug #2 — Same-bar SL/TP tiebreak was hardcoded

**File:** `src/backtest/ict_backtest.py` lines 1450–1468  
**Severity:** Low — affects only bars where both SL and TP are hit

**Problem:** The `elif` chain meant SL always won tiebreaks. For
strategies with very wide TPs (sniper TP=22× zone width), this is
an overly pessimistic assumption. The code pretended the worst
outcome always happened.

**Fix:** Added `sl_tp_tiebreak: str = "sl_first"` to
`TrendStrategyParams` with three modes:

| Mode | Behaviour |
|---|---|
| `"sl_first"` (default) | Conservative, matches legacy |
| `"tp_first"` | Optimistic — lets winners run |
| `"tp_if_wider"` | Symmetry heuristic: if TP distance ≥ SL distance, TP fires first |

Also added `"tp_first"` / `"tp_if_wider"` variants to the exit block
so the engine can evaluate which tiebreak assumption is more realistic.

---

### Bug #3 — `invalidation_grace_secs` was measured in bars, not seconds

**File:** `src/backtest/ict_backtest.py` line 1268  
**Severity:** Low — latent bug if data cadence changes

**Problem:** The docstring says "suppress soft-stop for first N seconds
after entry". The code used:

```python
in_grace = grace_bars > 0 and (i - tr.entry_bar) < grace_bars
```

On 1s data this accidentally equals seconds (1 bar = 1 second), but
on 1-minute data `grace_bars=5` would suppress for 5 *minutes*, not
5 seconds. The knob was miscalibrated.

**Fix:** Changed to wall-clock seconds:

```python
in_grace = grace_secs > 0 and (b_time - tr.entry_time) / 1e9 < grace_secs
```

`b_time` and `tr.entry_time` are both int64 nanoseconds, so this
works at any bar cadence.

---

### Bug #4 — `signal_id` used bar index, causing collisions

**File:** `src/backtest/ict_backtest.py` lines 817, 874, 1004  
**Severity:** Low — diagnostics only

**Problem:** Every layer submission stored `"signal_id": i` (the bar
index). When multiple signals fired on the same bar (common on 1s
XAUUSD), they all got the same ID, making per-signal diagnostics
(e.g. "which signal caused this trade?") unreliable.

**Fix:** Replaced with an incrementing counter:

```python
_sig_id = signal_id_counter
signal_id_counter += 1
# used in the layer dict
"signal_id": _sig_id,
```

Each submission gets a globally unique ID, regardless of how many
fire on the same bar.

---

### Bug #5 — `n_inversions` counted once per *trade*, not once per *zone inversion*

**File:** `src/backtest/ict_backtest.py` lines 1328, 1406, 1487  
**Severity:** Low — diagnostics inflation

**Problem:** When a zone inverted and 3 trade layers were open on it,
`n_inversions` was incremented 3 times. A zone inversion is one
event; the counter was reporting 3× the real count.

**Fix:** Track zone IDs that have already fired their soft-stop this
bar with a per-bar set:

```python
_zones_inverted_this_bar: set[int] = set()  # fresh each bar

# inside the per-trade loop:
zid = id(zone)
if zid in _zones_inverted_this_bar:
    continue  # another layer on the same zone already fired this bar
_zones_inverted_this_bar.add(zid)
n_inversions += 1
```

---

## Structural Blind Spots (not yet bugs, but risky)

### Blind Spot #1 — ATR calculation on bars with bad data

**File:** `src/core/ict_signals.py`, `compute_simple_atr`  
**Severity:** Medium — may cause NaN SL/TP on illiquid bars

XAUUSD 1s data can have duplicate timestamps or zero-volume bars.
`compute_simple_atr` uses `np.nanmax(high[i-k:i+1])` which propagates
NaN if any bar in the window is bad. If the last K bars include one
bad bar, `atr_arr` has a NaN, and `compute_layer_sl_tp` may produce
NaN SL/TP, silently creating un-filled or broken orders.

**Risk:** Uncommon but not impossible on real tick data. A NaN SL/TP
in the layer dict bypasses the float guard on `tr.stop_usd` and only
fails when the order tries to fill or when `b_low <= NaN` is evaluated.

**Recommended fix:** Sanitize ATR array after computation:
```python
atr_arr = np.nan_to_num(atr_arr, nan=atr_arr[0])  # forward-fill NaN
```

---

### Blind Spot #2 — No guard on `tr.target_usd == 0`

**File:** `src/backtest/ict_backtest.py`  
**Severity:** Low — edge case

If `compute_layer_sl_tp` returns `target_usd = 0` (possible if
`tp_floor=0` and `tp_atr_mult=0`), the TP is at the entry price.
The exit logic `elif b_high >= tp_price` fires immediately on the
entry bar. The trade wins with 0 duration and a 0-distance TP —
technically correct but economically meaningless and clutters diagnostics.

**Recommended fix:** Guard in `_close_trade` or in the exit block:
```python
if tr.target_usd <= 0:
    exit_reason = "dead_tp"  # flag rather than record as "tp"
```

---

### Blind Spot #3 — `lot_size` not validated against account balance

**File:** `src/core/ict_strategy.py`, `TrendStrategyParams`  
**Severity:** Low — backtest only

`lots=0.01` is hardcoded as default. If someone sets `lots=100` for
a stress test, the PnL numbers are meaningless (10,000× the normal
scale). The backtest doesn't model margin or balance.

**Recommended fix:** Add a `max_lots` guard in `run_ict_backtest`:
```python
assert 0 < p.lots <= 10.0, f"lots={p.lots} out of reasonable range"
```

---

### Blind Spot #4 — No EOD timestamp handling in `layer_lifetime_secs`

**File:** `src/backtest/ict_backtest.py`  
**Severity:** Low — subtle position-bleed risk

`layer_lifetime_secs` is checked against `b_time - layer["submit_time_ns"]`.
If the day ends with pending layers, they expire based on wall-clock
age, not based on the EOD session close. On a 24-hour dataset this
is fine. But if the corpus has a session break (e.g. weekend gap),
the pending layers from Friday carry their age into Monday and may
expire immediately on the Monday open — which is correct behaviour
but not clearly documented.

**Recommended fix:** Document the weekend-gap behaviour in the
`layer_lifetime_secs` docstring and add an optional
`session_end_time` parameter.

---

### Blind Spot #5 — `cum_pnl` uses running float; trade PnL stored separately

**File:** `src/backtest/ict_backtest.py`  
**Severity:** None — by design, but worth documenting

`cum_pnl` is a running float updated on every close. `Trade.pnl_usd`
stores the per-trade figure. The two should always agree. At
the end of the bar loop, `cum_pnl` is not re-derived from the sum
of `Trade.pnl_usd`, so floating-point drift is possible if many
small trades are closed in rapid succession.

**Recommended fix:** Add an assertion at the end of the bar loop:
```python
assert abs(cum_pnl - sum(t.pnl_usd for t in closed_trades)) < 1e-6
```

---

## Strategy Pivots (ranked by prior)

These are structural changes — they alter what the strategy *does*,
not how it parameterises existing behaviour.

---

### Pivot A — Close-commitment entry filter

**Hypothesis:** The dominant SL-loss pattern is the sub-second
wick-and-reject. Requiring the entry bar's *close* to commit
on the favourable side of the zone edge filters these out.

**Implementation:** In `fvg_retest_signals()` (or in the bar loop
entry block), add:
```python
# Long entry: require close ≥ zone_high - epsilon
if sig.direction > 0 and sig.bar_close < zone_high - 0.01:
    return []  # reject — wick-only entry
# Short entry: require close ≤ zone_low + epsilon
```

**Expected impact:** Remove 60–80% of sub-second SL losses.
Conservative EV improvement estimate: +$0.005–0.025/trade.

**Effort:** Low (~20 lines in `fvg_retest_signals`).

**Status:** Not yet tested

---

### Pivot B — Entry-bar volatility gate

**Hypothesis:** Sub-second SL exits happen during high-volatility
spikes. If the entry bar's range exceeds K × ATR, the trade is in
a volatility explosion that will SL-hunt.

**Implementation:**
```python
bar_range = b_high - b_low
atr_v = atr_arr[i]
if bar_range > k * atr_v:
    continue  # skip signal — high-vol spike entry
```

Test grid: `k = [2.0, 3.0, 4.0]`.

**Expected impact:** Capture 30–50% of sub-second SL losses at
moderate cost to TP rate.

**Effort:** Low (~5 lines in bar loop signal block).

**Status:** Not yet tested

---

### Pivot C — Trailing stop (the +EV missing piece)

**Hypothesis:** The ATR-anchor study showed that **widening SL
doesn't improve EV** — but a **trailing SL that locks in profit
after a move** does. The breakeven analysis: if realised losses
were capped at $0.10 via a trail, `atr_anchor m=4.0/8.0` would
produce **+$0.021 EV/trade** (+EV).

**Implementation:**
1. Add `trailing_sl: bool = False`, `trailing_sl_atr_mult: float = 0.5`
   to `TrendStrategyParams`.
2. After `trailing_sl_atr_mult × ATR` in profit, lift SL to
   `entry_price + trailing_sl_atr_mult × ATR` for longs (move it up).
3. Continue tightening as price extends.

**Expected impact:** If the trail fires on 30% of losing trades
and reduces their loss from avg $0.11 to avg $0.06, the breakeven
win-rate drops from 23% to 19%, matching the observed 22% win-rate
for `m=4.0/8.0`.

**Effort:** Medium (~50 lines in bar loop exit block).

**Status:** Not yet tested — this is the highest-priority structural
experiment.

---

### Pivot D — Wait-for-Nth-retest rule

**Hypothesis:** The FIRST retest of an FVG is the SL-hunt setup;
the SECOND retest (after the first SL-hunt is swept) is where the
real thesis plays out.

**Implementation:**
```python
fvg_min_retest_count: int = 0  # default 0 = legacy (enter on 1st retest)
# Only fire entry on retest #N+1
if zone.n_retests < fvg_min_retest_count:
    return []  # skip — not enough retests yet
```

Test grid: `fvg_min_retest_count = [1, 2, 3]`.

**Expected impact:** Lower volume, higher quality. If 80% of
first-retests are sub-second SLs and 60% of second-retests are
real entries, skipping the first improves EV significantly.

**Effort:** Low (~10 lines in `fvg_retest_signals`).

**Status:** Not yet tested

---

### Pivot E — Session-bias R:R asymmetry

**Hypothesis:** XAUUSD has a documented session-vol profile.
NY open (13:00–16:00 UTC) is most volatile. A directional R:R
asymmetry by session could flip EV positive.

**Implementation:**
```python
session_bias = {
    "asian":  {"bull": (1.0, 1.2), "bear": (1.0, 0.8)},
    "london":  {"bull": (1.0, 1.0), "bear": (1.0, 1.0)},
    "ny":      {"bull": (1.0, 0.8), "bear": (1.0, 1.2)},
}
# SL/TP = base × session_bias[session][direction]
```

**Expected impact:** Depends on whether session bias is consistent
across the corpus. London raids go both ways — the bias may
cancel out.

**Effort:** Medium.

**Status:** Not yet tested

---

### Pivot F — FVG-family detection

**Hypothesis:** Chains of 3+ overlapping FVGs represent real
displacement legs, not noise. Treating a family as one higher-quality
signal filters out noise without a new feature.

**Implementation:**
1. Tag each `FvgZone` with `family_id` and `family_size` in
   `detect_fvg`.
2. `fvg_only_families: bool = False` — emit signals only when
   `family_size >= 3`.
3. Scale TP by `family_size`.

**Expected impact:** If FVG families are 5% of signals but 50%
of winners, trading only families flips EV positive.

**Effort:** Medium (requires new detector-level tagging).

**Status:** Not yet tested

---

### Pivot G — Decoupled liquidity-sweep as primary source

**Hypothesis:** The existing `sweep` signal requires a live FVG
nearby. The actual sweep reversal (wick-through-pivot, close-back)
is structurally valid WITHOUT an adjacent FVG.

**Implementation:** Add `additional_sources=["sweep_pivot"]` that
fires on swing-high/low liquidity sweeps without FVG dependency.
Stop: 0.5 × ATR past the wick. Target: 1.5 × ATR in reversal
direction.

**Expected impact:** More signals, different quality distribution.
The sweep pattern is anti-SL-hunt by construction.

**Effort:** Medium-large.

**Status:** Not yet tested

---

### Pivot H — Post-retest confirmation bar (2-bar entry)

**Hypothesis:** The current entry fires on the FIRST bar that
enters the zone. If we require the NEXT bar's close to also be
on the favourable side (the "confirmation bar"), we filter the
wick-and-reject pattern at the cost of 1-bar delay.

**Implementation:**
```python
# In fvg_retest_signals: emit a PendingSignal but with a flag
# that the bar loop checks on bar N+1
sig.needs_confirmation = True
sig.confirm_bar = sig.trigger_bar + 1
sig.confirm_price = zone_high if sig.direction > 0 else zone_low
# Bar loop: only fill if b_close is on the correct side
if sig.needs_confirmation and i == sig.confirm_bar:
    if sig.direction > 0 and b_close < sig.confirm_price:
        return  # failed confirmation — skip
```

**Expected impact:** Filters 50–70% of wick-only entries. Trade
count drops but EV/trade improves.

**Effort:** Medium (~30 lines).

**Status:** Not yet tested

---

### Pivot I — ORB at NY open as complementary source

**Hypothesis:** The 13:30 UTC NY open ORB fires in the most
volatile XAUUSD window. It may complement the sniper edge with a
separate signal source.

**Implementation:**
```python
additional_sources=["orb"]
orb_session_hour_utc=13  # NY open
orb_range_minutes=15
```

**Expected impact:** Low-to-moderate. ORB signals are structurally
different from FVG (session-range-based vs displacement-gap-based).

**Effort:** Low — already wired in `detect_orb`.

**Status:** Not yet tested with sniper base

---

### Pivot J — Wyckoff spring/UTAD as regime-change signal

**Hypothesis:** Wyckoff springs and UTADs are rare (~1/day) but
mark structural regime changes. Rare but high-quality.

**Implementation:**
```python
additional_sources=["wyckoff"]
```

**Expected impact:** Low volume, unknown quality. Wyckoff patterns
are structurally sound but the detector hasn't been validated
against manual charts.

**Effort:** Low — already wired in `detect_wyckoff`.

**Status:** Not yet tested

---

### Pivot K — Multi-timeframe FVG composite

**Hypothesis:** Requiring BOTH a 1m AND a 5m FVG at the same price
level filters out 70% of single-timeframe noise.

**Implementation:** Run a second `detect_fvg` pass at 300s (5m) resample
and require both to agree before emitting a signal.

**Expected impact:** If the 5m FVG filters 70% of noise and keeps
30% of signals, the EV improvement depends on whether the 30%
kept are actually higher quality.

**Effort:** Medium-large (requires two-pass detection).

**Status:** Not yet tested

---

## Priority Order for Next Session

| # | Pivot | Why | Effort |
|---|---|---|---|
| **C** | Trailing stop | Highest prior — closes the +EV gap identified in ATR-anchor analysis | Medium |
| **A** | Close-commitment entry | Most direct fix to sub-second SL problem, ~20 lines | Low |
| **B** | Volatility gate | Easy A/B, tests the vol-spike hypothesis | Low |
| **D** | Nth-retest filter | Low effort, high information value | Low |
| **H** | 2-bar confirmation | Similar to A but delays entry by 1 bar | Medium |
| **I** | ORB at NY open | Already wired, just needs enabling on sniper base | Low |
| **F** | FVG-family detection | Higher effort but tests a structural hypothesis | Medium |
| **E** | Session bias | Lower confidence — London raids go both ways | Medium |
| **J** | Wyckoff | Already wired, low cost to test | Low |
| **K** | Multi-TF FVG | Higher effort, uncertain payoff | Medium-large |
| **G** | Decoupled sweep | Structural but requires new detector | Medium-large |

---

## Regressions to Check After Any Pivot

Before promoting any pivot to the canonical recipe, verify:

1. **Soft-stop rate** — should not increase (more inversions = more
   soft-stops = more loss if they're on the wrong side)
2. **Trade count** — should not drop more than 50% (volume matters
   for statistical power of EV estimate)
3. **Win rate** — should improve or stay flat; if it drops more
   than 5pp, the pivot is filtering real entries
4. **Hold-time distribution** — pivots that widen SL should show
   longer median hold time
5. **EV/trade delta** — per the delta-vs-baseline methodology in
   the sweep drivers

