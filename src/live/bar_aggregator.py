"""Rolling 1s OHLCV bar aggregator.

Consumes ``AggTrade`` events from :mod:`src.live.binance_client`
and emits closed 1-second bars on each second boundary. This is
the live equivalent of the polars-based resampling in
:mod:`src.tools.fetch_binance_aggtrades` but for a streaming
source.

Algorithm
==========

Maintain a single mutable dict representing the *current*
1-second bar:

* ``bar_ts_ms`` — the start-of-second timestamp (UTC, ms)
* ``open``, ``high``, ``low``, ``close`` — first/highest/lowest/last trade price
* ``volume`` — total base-asset quantity
* ``notional`` — sum(price * quantity)
* ``n_trades`` — number of trades that contributed

On every ``AggTrade``:
* If the trade's ``transact_time_ms`` is in the same second as
  ``bar_ts_ms``: update the current bar in place.
* Otherwise (rollover): ``emit`` the closed bar, then start a
  fresh one for the new second.

This matches Binance's REST 1s kline semantics (the kline is
the closed bar at second ``bar_ts_ms`` to
``bar_ts_ms + 1000``).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional


_log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Local copy of the AggTrade type. We don't import it from
# ``binance_client`` so this module stays importable without
# ``aiohttp`` / ``websockets`` (useful for offline smoke tests).
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AggTrade:
    """A single Binance aggTrade event — mirrors the type in ``binance_client``."""
    event_time_ms: int
    symbol: str
    agg_trade_id: int
    price: float
    quantity: float
    first_trade_id: int
    last_trade_id: int
    transact_time_ms: int
    is_buyer_maker: bool


@dataclass
class Bar1s:
    """A single closed 1-second OHLCV bar."""
    bar_ts_ms: int           # start-of-second UTC ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    notional: float
    n_trades: int
    taker_buy_volume: float  # qty where is_buyer_maker = False
    taker_sell_volume: float # qty where is_buyer_maker = True

    def to_dict(self) -> dict:
        return {
            "ts_ms": self.bar_ts_ms,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "notional": self.notional,
            "n_trades": self.n_trades,
            "taker_buy_volume": self.taker_buy_volume,
            "taker_sell_volume": self.taker_sell_volume,
        }

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0


# ─────────────────────────────────────────────────────────────────────────────
# Aggregator
# ─────────────────────────────────────────────────────────────────────────────

def _second_floor(ts_ms: int) -> int:
    """Round a ms timestamp down to its second boundary."""
    return (int(ts_ms) // 1000) * 1000


class BarAggregator:
    """Maintains the rolling 1s bar; emits closed bars on rollover."""

    def __init__(self) -> None:
        self._bar_ts_ms: int = -1
        self._open: float = 0.0
        self._high: float = 0.0
        self._low: float = 0.0
        self._close: float = 0.0
        self._volume: float = 0.0
        self._notional: float = 0.0
        self._n_trades: int = 0
        self._taker_buy_vol: float = 0.0
        self._taker_sell_vol: float = 0.0
        # last-emitted bar (useful for monitoring / catch-up)
        self._last_emitted_ts: int = -1

    def on_trade(self, t: AggTrade) -> Optional[Bar1s]:
        """Incorporate one aggTrade. Returns a closed Bar1s if this
        trade rolled us into a new second, else None.
        """
        ts_ms = int(t.transact_time_ms)
        sec = _second_floor(ts_ms)
        if self._bar_ts_ms < 0:
            self._init_bar(sec, t)
            return None

        if sec == self._bar_ts_ms:
            self._update_bar(t)
            return None

        # Rollover — emit the old bar (if any trades) then start the new one.
        emitted = None
        if self._n_trades > 0:
            emitted = Bar1s(
                bar_ts_ms=self._bar_ts_ms,
                open=self._open,
                high=self._high,
                low=self._low,
                close=self._close,
                volume=self._volume,
                notional=self._notional,
                n_trades=self._n_trades,
                taker_buy_volume=self._taker_buy_vol,
                taker_sell_volume=self._taker_sell_vol,
            )
            self._last_emitted_ts = emitted.bar_ts_ms
        # Skip ahead — if multiple seconds elapsed without a trade
        # we just emit the most recent bar (the missing seconds are
        # the caller's gap-filling problem, not ours).
        self._init_bar(sec, t)
        return emitted

    def last_emitted_ts(self) -> int:
        return self._last_emitted_ts

    # ── Internals ─────────────────────────────────────────────────────

    def _init_bar(self, sec: int, t: AggTrade) -> None:
        self._bar_ts_ms = sec
        px = float(t.price)
        qty = float(t.quantity)
        self._open = px
        self._high = px
        self._low = px
        self._close = px
        self._volume = qty
        self._notional = px * qty
        self._n_trades = 1
        self._taker_buy_vol = 0.0 if t.is_buyer_maker else qty
        self._taker_sell_vol = qty if t.is_buyer_maker else 0.0

    def _update_bar(self, t: AggTrade) -> None:
        px = float(t.price)
        qty = float(t.quantity)
        if px > self._high:
            self._high = px
        if px < self._low:
            self._low = px
        self._close = px
        self._volume += qty
        self._notional += px * qty
        self._n_trades += 1
        if t.is_buyer_maker:
            self._taker_sell_vol += qty
        else:
            self._taker_buy_vol += qty

    # ── Forced emission (used on shutdown) ────────────────────────────

    def flush(self) -> Optional[Bar1s]:
        """Emit the in-flight bar (e.g. on shutdown). Returns None if empty."""
        if self._bar_ts_ms < 0 or self._n_trades == 0:
            return None
        emitted = Bar1s(
            bar_ts_ms=self._bar_ts_ms,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=self._volume,
            notional=self._notional,
            n_trades=self._n_trades,
            taker_buy_volume=self._taker_buy_vol,
            taker_sell_volume=self._taker_sell_vol,
        )
        self._last_emitted_ts = emitted.bar_ts_ms
        self._bar_ts_ms = -1
        return emitted


__all__ = ["Bar1s", "BarAggregator"]