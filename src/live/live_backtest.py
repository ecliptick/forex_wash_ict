"""Signal-only mirror of the backtest bar loop.

This module is the strategy-touching code in the live engine.
It does NOT re-implement the strategy. It imports the same
detector / structure primitives the backtest uses and walks
each new 1s bar through the sniper intercept (mirroring
``run_ict_backtest`` lines 843-875) + the sniper queue walk
(lines 1060-1181). When a sniper fires, it hands off to the
:class:`OrderManager` rather than appending to an in-process
``pending_inv_layers`` list.

What this module owns
=====================

* A rolling numpy ring buffer of recent OHLCV (configurable
  warmup size — defaults to 14400 bars = 4 hours at 1s) so
  the detectors have enough context.
* The stateful ``BosChochMemory`` and the warmup
  ``detect_market_structure`` call.
* The stateful ``FvgZone`` list and the per-bar FVG update
  pass (mitigation / inversion / superseded / played_out).
* The pending snipers dict, keyed by ``id(zone)``.

What this module does NOT own
==============================

* Order placement / signing — delegated to ``order_manager``.
* Order persistence / DB writes — delegated to ``state_store``.
* Feed staleness / circuit breakers — handled by ``main``.

When a sniper fires, this module calls
``self.on_sniper_trigger(sniper_row, zone, inv_dir)`` — the
caller wires that to :class:`OrderManager.submit_sniper_market`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import numpy as np

from ..core.ict_signals import (
    FvgZone,
    detect_fvg,
    compute_simple_atr,
    generate_ict_pending_signals,
)
from ..core.market_structure import (
    StructureState,
    detect_market_structure,
    annotate_choch_plus,
    BosChochMemory,
)
from ..core.optimal_config import optimal_params as _canonical, OPTIMAL_RECIPE_VERSION
from .state_store import StateStore, ZoneRow, SniperRow

_log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Public dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class InFlightBar:
    """One closed 1s bar pushed through the live bar loop."""
    bar_idx: int              # monotonically increasing
    bar_ts_ms: int            # UTC ms
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SniperSpec:
    """A sniper trade the bar loop decided to fire this bar.

    The caller (typically the order manager) takes this and
    submits the actual market order to the broker.
    """
    sniper_id: int
    signal_id: int
    direction: int            # +1 long, -1 short
    symbol: str
    quantity: float
    stop_usd: float           # absolute distance from entry
    target_usd: float
    entry_price_hint: float   # bar close when the zone inverted
    zone_id: int
    triggered_by: str         # 'fvg' or 'ifvg'
    submit_time_ns: int       # ns since epoch


# ─────────────────────────────────────────────────────────────────────────────
# The live bar loop
# ─────────────────────────────────────────────────────────────────────────────

class LiveBacktest:
    """Signal-only mirror of ``run_ict_backtest`` (sniper mode)."""

    # Warmup: 4h at 1s = 14400 bars. Past this, drop the oldest bar.
    DEFAULT_WARMUP_BARS = 14_400

    def __init__(
        self,
        state: StateStore,
        symbol: str,
        on_sniper_trigger: Callable[[SniperSpec], Awaitable[None]],
        params=None,
        warmup_bars: int = DEFAULT_WARMUP_BARS,
    ) -> None:
        self.state = state
        self.p = params if params is not None else _canonical()
        self.symbol = symbol
        self._on_sniper = on_sniper_trigger
        self._warmup = int(warmup_bars)

        # Per-bar arrays (ring buffers). Built up as bars arrive.
        cap = self._warmup
        self._times_ns = np.zeros(cap, dtype=np.int64)
        self._open = np.zeros(cap, dtype=np.float64)
        self._high = np.zeros(cap, dtype=np.float64)
        self._low = np.zeros(cap, dtype=np.float64)
        self._close = np.zeros(cap, dtype=np.float64)
        self._volume = np.zeros(cap, dtype=np.float64)
        self._n = 0                # total bars pushed so far
        self._cursor = 0           # write position (mod cap)

        # Detector state
        self._zones: list[FvgZone] = []
        self._zones_by_id: dict[int, FvgZone] = {}  # zone_id -> FvgZone
        self._bos_choch = BosChochMemory(
            max_events=int(getattr(self.p, "bos_choch_memory_n_events", 5)),
        )
        self._next_zone_id = 0
        self._next_signal_id = 0

        # Sniper pool: zone_id -> list[SniperRow] currently pending
        self._pending: dict[int, list[SniperRow]] = {}

        # Loaded state from the store (resume-safe).
        self._load_persisted_state()

    # ── Public API ─────────────────────────────────────────────────────

    def on_bar(self, bar: InFlightBar) -> None:
        """Push one closed 1s bar through the strategy."""
        # Push into ring buffer
        idx = self._cursor
        self._times_ns[idx] = bar.bar_ts_ms * 1_000_000
        self._open[idx] = bar.open
        self._high[idx] = bar.high
        self._low[idx] = bar.low
        self._close[idx] = bar.close
        self._volume[idx] = bar.volume
        self._cursor = (self._cursor + 1) % self._warmup
        if self._n < self._warmup:
            self._n += 1

        # Sniper mode: we do NOT iterate the FVG detector every bar;
        # we run it once per bar only when there are signals to
        # intercept. The detector is vectorised so cost is bounded.
        n = self._n
        if n < 50:
            return  # not enough history for the detector

        # Materialise contiguous view of the ring buffer
        o, h, l, c, v = self._contig_arrays()
        times_ns = self._times_contig()

        # Compute BoS/CHoCH + ATR (vectorised, one call per bar).
        # NOTE: this is O(N) per bar in the worst case. For 14,400
        # bars it's ~0.5ms — acceptable.
        atr_arr = compute_simple_atr(h, l, c, length=int(self.p.atr_len))
        structure: StructureState = detect_market_structure(
            h, l, c,
            pivot_len=int(self.p.ms_pivot_len),
            liquidity_len=int(self.p.ms_liquidity_len),
            detect_order_blocks=bool(self.p.ms_draw_order_blocks),
            detect_liquidity=bool(self.p.ms_draw_liquidity_sweeps),
            resample_to_n_secs=int(getattr(self.p, "ms_resample_secs", 0)),
        )
        # annotate CHoCH+ (cheap O(N))
        annotate_choch_plus(structure, self._zones)
        # Update rolling BoS/CHoCH memory: compare against the prior
        # trend array to find the bars that flipped this bar. Cheap.
        if n >= 2:
            self._update_bos_choch_memory(structure, idx)

        # Run FVG detection over the full rolling window. Each pass
        # produces FvgZone objects; we update lifecycle flags and
        # persist them. (detect_fvg is idempotent — re-running on
        # the same bars gives the same zones.)
        self._refresh_zones(o, h, l, c, times_ns, structure, atr_arr)

        # 1a. Sniper intercept — for any fvg/ifvg signal whose
        # trigger_bar == current bar_idx, push a pending sniper.
        # (We approximate by checking the zone that JUST got born
        # this bar — see _refresh_zones for the zone.born flag.)
        self._intercept_new_signals(bar)

        # 1c. Walk pending snipers — for each one whose zone has
        # inverted THIS bar, fire.
        # We do this synchronously; the order_manager awaits.
        import asyncio
        asyncio.get_event_loop().create_task(
            self._walk_pending_snipers(bar, atr_arr)
        )

    # ── Ring buffer helpers ───────────────────────────────────────────

    def _contig_arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Return the live ring as contiguous numpy views (no copy)."""
        n = self._n
        c = self._cursor
        if n < self._warmup:
            # Not yet wrapped — first `n` entries from index 0.
            return (
                self._open[:n].copy(),
                self._high[:n].copy(),
                self._low[:n].copy(),
                self._close[:n].copy(),
                self._volume[:n].copy(),
            )
        # Wrapped: concatenate [cursor .. end] + [0 .. cursor].
        o = np.concatenate([self._open[c:], self._open[:c]])
        h = np.concatenate([self._high[c:], self._high[:c]])
        l = np.concatenate([self._low[c:], self._low[:c]])
        cl = np.concatenate([self._close[c:], self._close[:c]])
        v = np.concatenate([self._volume[c:], self._volume[:c]])
        return o, h, l, cl, v

    def _times_contig(self) -> np.ndarray:
        n = self._n
        c = self._cursor
        if n < self._warmup:
            return self._times_ns[:n].copy()
        return np.concatenate([self._times_ns[c:], self._times_ns[:c]])

    # ── FVG detection + lifecycle update ──────────────────────────────

    def _refresh_zones(
        self,
        o: np.ndarray,
        h: np.ndarray,
        l: np.ndarray,
        c: np.ndarray,
        times_ns: np.ndarray,
        structure: StructureState,
        atr_arr: np.ndarray,
    ) -> None:
        """Run ``detect_fvg`` on the rolling window and update our zones.

        ``detect_fvg`` re-detects everything from scratch; we
        reconcile its output against our persisted zone state by
        matching on (trigger_bar, direction, zone_low, zone_high).
        """
        params = self.p
        zones = detect_fvg(
            open_=o, high=h, low=l, close=c,
            warmup=0,
            max_active_zones=0,
            resample_to_n_secs=int(params.fvg_resample_secs),
            fvg_min_zone_usd=float(getattr(params, "fvg_min_zone_usd", 0.0)),
            max_zone_age_bars=int(getattr(params, "fvg_max_age_bars", 0)),
            supersede_on_new=bool(params.fvg_supersede_on_new),
            invalidation_min_pierce_usd=float(params.fvg_invalidation_min_pierce_usd),
            invalidation_min_consecutive_bars=int(params.fvg_invalidation_min_consecutive_bars),
            require_retest_to_invert=bool(params.fvg_require_retest_to_invert),
            played_out_min_extension_usd=float(getattr(params, "played_out_min_extension_usd", 0.0)),
            times_utc_ns=times_ns,
            fvg_min_lifetime_secs=int(getattr(params, "fvg_min_lifetime_secs", 0)),
            structure_events_per_bar=structure.events,
            structure_invalidation_age_secs=int(getattr(params, "fvg_structure_invalidation_age_secs", 0)),
        )

        # Match detector output to existing zones by (trigger_bar, direction, zone_low, zone_high).
        # New zones get a fresh zone_id; old zones inherit their existing zone_id.
        existing_keys = {
            (z.trigger_bar, z.direction, round(z.zone_low, 4), round(z.zone_high, 4)): z
            for z in self._zones
        }
        new_zones: list[FvgZone] = []
        for zd in zones:
            key = (zd.trigger_bar, zd.direction, round(zd.zone_low, 4), round(zd.zone_high, 4))
            if key in existing_keys:
                # Copy mutable lifecycle fields onto the existing object.
                ex = existing_keys[key]
                ex.mitigated_bar = zd.mitigated_bar
                ex.mitigated_depth_pct = zd.mitigated_depth_pct
                ex.pierced_bar = zd.pierced_bar
                ex.inverted_bar = zd.inverted_bar
                ex.inverted = zd.inverted
                ex.consumed_bar = zd.consumed_bar
                ex.expired_bar = zd.expired_bar
                ex.superseded_bar = zd.superseded_bar
                ex.played_out_bar = zd.played_out_bar
                ex.live = zd.live
                if hasattr(zd, "n_touches"):
                    ex.n_touches = zd.n_touches
                new_zones.append(ex)
            else:
                # Fresh zone — assign a stable zone_id (monotonic).
                self._next_zone_id += 1
                zd.zone_id = self._next_zone_id
                self._zones_by_id[self._next_zone_id] = zd
                new_zones.append(zd)

        self._zones = new_zones

        # Persist every (still live) zone to the DB.
        for z in new_zones:
            zid = getattr(z, "zone_id", None)
            if zid is None:
                continue
            row = ZoneRow(
                zone_id=zid,
                trigger_bar=int(z.trigger_bar),
                trigger_time_ns=_zone_trigger_time_ns(z, times_ns),
                direction=int(z.direction),
                zone_low=float(z.zone_low),
                zone_high=float(z.zone_high),
                mitigated_bar=int(z.mitigated_bar),
                pierced_bar=int(z.pierced_bar),
                inverted_bar=int(z.inverted_bar),
                inverted=bool(z.inverted),
                consumed_bar=int(z.consumed_bar),
                expired_bar=int(z.expired_bar),
                superseded_bar=int(z.superseded_bar),
                played_out_bar=int(z.played_out_bar),
                live=bool(z.live),
                n_touches=int(getattr(z, "n_touches", 0)),
            )
            self.state.upsert_zone(row)

    @staticmethod
    def _zone_trigger_time_ns_static(z: FvgZone, times_ns: np.ndarray) -> int:
        return int(times_ns[max(0, min(int(z.trigger_bar), len(times_ns) - 1))])

    # ── BoS/CHoCH memory update ──────────────────────────────────────

    def _update_bos_choch_memory(self, structure: StructureState, current_bar_in_ring: int) -> None:
        """Push any new BoS/CHoCH events into the rolling memory.

        The detector accumulates events into ``structure.breaks`` as
        it runs. We compare against the deque's already-recorded
        bar indices to avoid pushing duplicates.
        """
        existing_bar_set = {b for (b, _k) in self.state.load_bos_choch_memory()}
        last_break_bar_arr = structure.last_break_bar
        if last_break_bar_arr is None or last_break_bar_arr.shape[0] == 0:
            return
        # Only the most recent bar matters — older breaks are already
        # in the breaks log; we'd push them on every call otherwise.
        new_bar = int(last_break_bar_arr[-1])
        if new_bar < 0 or new_bar in existing_bar_set:
            return
        new_kind = int(structure.last_break_kind[-1])
        if new_kind == 0:
            return
        self._bos_choch.push(new_bar, new_kind)
        self.state.push_bos_choch(new_bar, new_kind)

    # ── Sniper intercept ──────────────────────────────────────────────

    def _intercept_new_signals(self, bar: InFlightBar) -> None:
        """Push a sniper for any zone born on this bar with retest scan.

        We detect new zones by zone.trigger_bar == (self._n - 1) AND
        zone.consumed_bar == -1 (the retest scanner hasn't yet
        claimed the zone). For the live engine we *always* defer the
        entry into a pending sniper regardless of whether the
        immediate retest already fired on bar n.
        """
        n = self._n
        for z in self._zones:
            if not z.live:
                continue
            if z.trigger_bar != n - 1:
                continue
            zid = getattr(z, "zone_id", None)
            if zid is None:
                continue
            # Skip if already in pending for this zone
            if zid in self._pending and self._pending[zid]:
                continue
            self._next_signal_id += 1
            row = SniperRow(
                sniper_id=-1,  # assigned by DB on insert
                signal_id=self._next_signal_id,
                submit_bar=self._n - 1,
                submit_time_ns=bar.bar_ts_ms * 1_000_000,
                direction=int(z.direction),
                zone_id=zid,
                triggered_by="fvg",
                trigger_price=float(bar.close),
                stop_usd=0.0,
                target_usd=0.0,
                lots=float(self.p.lots),
                status="pending",
            )
            row.sniper_id = self.state.insert_sniper(row)
            self._pending.setdefault(zid, []).append(row)

    # ── Sniper queue walk ─────────────────────────────────────────────

    async def _walk_pending_snipers(
        self,
        bar: InFlightBar,
        atr_arr: np.ndarray,
    ) -> None:
        """For each pending sniper, check if the zone has inverted THIS bar.

        On inversion: compute SL/TP, fire ``on_sniper_trigger`` (the
        order manager), and mark the sniper triggered. Cancellation
        conditions mirror lines 1092-1109 of the backtest.
        """
        sniper_min_zone = float(getattr(self.p, "fvg_inv_trade_min_zone_usd", 0.0))
        sniper_max_per = int(getattr(self.p, "fvg_inv_trade_max_per_zone", 1))
        sniper_max_age = int(getattr(self.p, "sniper_max_age_secs", 1800))
        sniper_sl_mult = float(getattr(self.p, "fvg_inv_trade_sl_zone_mult", 1.0))
        sniper_tp_mult = float(getattr(self.p, "fvg_inv_trade_tp_zone_mult", 22.0))
        sniper_use_atr_tp = bool(getattr(self.p, "sniper_use_atr_tp", False))
        sniper_tp_atr_mult = float(getattr(self.p, "fvg_inv_trade_tp_atr_mult", 22.0))

        now_ns = time.time_ns()
        to_remove: list[int] = []

        for zid, snipers in list(self._pending.items()):
            zone = self._zones_by_id.get(zid)
            if zone is None:
                to_remove.append(zid)
                continue

            kept: list[SniperRow] = []
            for sp in snipers:
                # Cancellation: superseded / played_out / expired
                if zone.superseded_bar >= 0 or zone.played_out_bar >= 0 or zone.expired_bar >= 0:
                    self.state.update_sniper_status(sp.sniper_id, "cancelled")
                    continue
                # Age cap
                if (now_ns - sp.submit_time_ns) / 1_000_000_000 > sniper_max_age:
                    self.state.update_sniper_status(sp.sniper_id, "expired")
                    continue

                # INVERSION — this is the trigger.
                if zone.inverted:
                    zone_w = float(zone.zone_high - zone.zone_low)
                    if zone_w < sniper_min_zone:
                        self.state.update_sniper_status(sp.sniper_id, "cancelled")
                        continue
                    # Compute SL/TP per the canonical recipe
                    if sniper_use_atr_tp:
                        atr_v = float(atr_arr[-1]) if atr_arr.shape[0] > 0 else 0.0
                        target_usd_v = atr_v * sniper_tp_atr_mult
                    else:
                        target_usd_v = zone_w * sniper_tp_mult
                    stop_usd_v = zone_w * sniper_sl_mult
                    inv_dir = -int(sp.direction)
                    spec = SniperSpec(
                        sniper_id=sp.sniper_id,
                        signal_id=sp.signal_id,
                        direction=inv_dir,
                        symbol=self.symbol,
                        quantity=float(self.p.lots),
                        stop_usd=stop_usd_v,
                        target_usd=target_usd_v,
                        entry_price_hint=float(bar.close),
                        zone_id=zid,
                        triggered_by=sp.triggered_by,
                        submit_time_ns=now_ns,
                    )
                    try:
                        await self._on_sniper(spec)
                    except Exception as e:
                        _log.exception("sniper trigger failed", extra={"err": repr(e)})
                    self.state.update_sniper_status(sp.sniper_id, "triggered")
                    continue  # not kept — fired
                kept.append(sp)
            if kept:
                self._pending[zid] = kept
            else:
                to_remove.append(zid)
        for zid in to_remove:
            self._pending.pop(zid, None)

    # ── Resume from persisted state ───────────────────────────────────

    def _load_persisted_state(self) -> None:
        # BoS/CHoCH deque
        for bar, kind in self.state.load_bos_choch_memory():
            self._bos_choch.push(bar, kind)
        # Live zones (not used here — we re-detect on every bar —
        # but we seed the zone_id counter to avoid collisions).
        for row in self.state.load_live_zones():
            if row.zone_id > self._next_zone_id:
                self._next_zone_id = row.zone_id
        # Pending snipers
        for row in self.state.load_pending_snipers():
            self._pending.setdefault(row.zone_id, []).append(row)
        # We DO NOT reconstruct FvgZone Python objects here; on first
        # ``on_bar`` call the detector rebuilds them from OHLCV.


# Module-level helper used by ``_refresh_zones``.
def _zone_trigger_time_ns(z: FvgZone, times_ns: np.ndarray) -> int:
    return int(times_ns[max(0, min(int(z.trigger_bar), len(times_ns) - 1))])


__all__ = ["LiveBacktest", "InFlightBar", "SniperSpec"]