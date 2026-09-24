"""Feed handler — bridges the Binance WS stream to the bar aggregator.

Owns the asyncio task that:
1. Subscribes to the combined aggTrade + bookTicker WS.
2. Pumps agg trades into :class:`BarAggregator`.
3. Emits closed 1s bars on rollover to the bar loop.
4. Updates the latest bookTicker snapshot (used by the SL/TP
   spread guard).

Heartbeat is updated on every WS frame so the health endpoint
can detect stalls.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .bar_aggregator import Bar1s, BarAggregator
from .binance_client import AggTrade, BinanceClient, BookTicker
from .live_backtest import InFlightBar, LiveBacktest
from .state_store import StateStore

_log = logging.getLogger(__name__)


class FeedHandler:
    """Owns the WS consumer + bookTicker snapshot."""

    def __init__(
        self,
        client: BinanceClient,
        state: StateStore,
        bar_loop: LiveBacktest,
        symbol: str,
        on_closed_bar: callable,  # async callable receiving Bar1s
    ) -> None:
        self.client = client
        self.state = state
        self.bar_loop = bar_loop
        self.symbol = symbol
        self._on_closed_bar = on_closed_bar
        self._aggregator = BarAggregator()
        self._book: Optional[BookTicker] = None
        self._bar_count = 0
        self._last_event_ns: int = time.time_ns()

    @property
    def last_book(self) -> Optional[BookTicker]:
        return self._book

    @property
    def bar_count(self) -> int:
        return self._bar_count

    async def run(self, shutdown: asyncio.Event) -> None:
        """Drive the WS consumer + bar loop until shutdown."""
        async for event in self.client.stream_aggtrade_bookticker(self.symbol):
            if shutdown.is_set():
                break
            self._last_event_ns = time.time_ns()
            if isinstance(event, AggTrade):
                closed = self._aggregator.on_trade(event)
                if closed is not None:
                    self._bar_count += 1
                    self.state.incr_counter("bars_received")
                    bar = self._to_inflight(closed)
                    # Run the bar loop synchronously (detector code is CPU-bound).
                    self.bar_loop.on_bar(bar)
                    self.state.heartbeat(last_bar_ns=self._last_event_ns)
                    # Notify the caller (e.g. main → publish to dashboard).
                    try:
                        await self._on_closed_bar(closed)
                    except Exception as e:
                        _log.exception("on_closed_bar callback failed", extra={"err": repr(e)})
            elif isinstance(event, BookTicker):
                self._book = event
                self.state.heartbeat(last_feed_ns=self._last_event_ns)

    def last_event_age_secs(self) -> float:
        return (time.time_ns() - self._last_event_ns) / 1e9

    @staticmethod
    def _to_inflight(b: Bar1s) -> InFlightBar:
        return InFlightBar(
            bar_idx=0,                # assigned by the bar loop's internal counter
            bar_ts_ms=b.bar_ts_ms,
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=b.volume,
        )


__all__ = ["FeedHandler"]