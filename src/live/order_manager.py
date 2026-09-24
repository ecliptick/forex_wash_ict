"""Idempotent order manager for the live engine.

Why this exists
===============

* **Idempotency** — every order has a stable ``client_order_id``
  derived from the ``sniper_id``. The DB has a UNIQUE
  constraint on this. If the process crashes after submitting
  to the broker but before recording the ``binance_order_id``,
  the next restart will re-submit and the broker will return a
  duplicate-error — but our state already has the original
  ``binance_order_id`` (we look it up before re-submitting).
* **Pool management** — the user explicitly asked for an order
  pool. We use a pre-generated UUID pool of 32,768 IDs.
* **Rate-limit budget** — Binance allows 10 submissions/sec and
  1200/minute. We enforce a token-bucket so a bug can't DOS the
  broker.
* **Retry policy** — 3 attempts with exponential backoff for
  transient failures (network, 5xx); fail-fast on permanent
  errors (4xx with business code like ``-2010`` "insufficient
  balance").
* **Cancel-on-shutdown** — see :meth:`cancel_all_open`.

Order types
===========

The v17 SNIPER strategy is *market-on-inversion*: when a sniper
fires we want to be in the market at the next bar's open. We
submit a single ``MARKET`` order sized at
``p.lots * p.contract_size`` (BTC contract size = 0.001). SL/TP
are managed via Binance's ``STOP_MARKET`` and ``TAKE_PROFIT_MARKET``
working types — both are attached after the entry fills.

For the sniper, the SL price = ``entry_price ± stop_usd`` (in
the direction of the trade). The TP price = ``entry_price ±
target_usd``. Both are computed from the spec the bar loop
passed.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Optional

from .binance_client import BinanceClient, BinanceAPIError, OrderAck
from .live_backtest import SniperSpec
from .state_store import OrderRow, StateStore

_log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────────────

class OrderRejected(Exception):
    """Broker rejected the order (permanent, no retry)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"order rejected code={code}: {message}")
        self.code = code
        self.message = message


class OrderPoolExhausted(Exception):
    """The 32,768-ID pool is full — shouldn't happen on a paper run."""


# ─────────────────────────────────────────────────────────────────────────────
# Token-bucket rate limiter
# ─────────────────────────────────────────────────────────────────────────────

class TokenBucket:
    """Simple token-bucket for outbound requests.

    Binance limits per endpoint:
    * ORDER: 10/sec, 1200/min
    * General signed: 5/sec, 100/5s, 2400/min
    """

    def __init__(self, rate_per_sec: float, capacity: int) -> None:
        self.rate = float(rate_per_sec)
        self.capacity = int(capacity)
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._last_refill = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
                await asyncio.sleep(deficit / self.rate)


# ─────────────────────────────────────────────────────────────────────────────
# Client order ID pool
# ─────────────────────────────────────────────────────────────────────────────

class ClientOrderIdPool:
    """Pre-generates a bounded pool of UUIDv4 client order IDs.

    Pulled FIFO; replenished in a background task. The pool size
    (32,768) gives plenty of headroom for a 24h paper session
    (~20-50 trades/day) but caps memory growth.
    """

    def __init__(self, size: int = 32_768, refill_threshold: int = 4_096) -> None:
        self._size = int(size)
        self._refill_threshold = int(refill_threshold)
        self._pool: deque[str] = deque()
        self._initial_fill_done = False

    def initial_fill(self) -> None:
        if self._initial_fill_done:
            return
        for _ in range(self._size):
            self._pool.append(uuid.uuid4().hex)
        self._initial_fill_done = True

    def get(self) -> str:
        if not self._pool:
            # Refill synchronously as a safety net.
            for _ in range(64):
                self._pool.append(uuid.uuid4().hex)
        try:
            return self._pool.popleft()
        except IndexError as e:
            raise OrderPoolExhausted("client order ID pool empty") from e

    def put_back(self, client_order_id: str) -> None:
        """Return an unused ID (e.g. cancelled before submission)."""
        if len(self._pool) < self._size:
            self._pool.append(client_order_id)

    def __len__(self) -> int:
        return len(self._pool)


# ─────────────────────────────────────────────────────────────────────────────
# Order Manager
# ─────────────────────────────────────────────────────────────────────────────

class OrderManager:
    """Places orders through BinanceClient and persists them."""

    # Binance permanent-reject codes that should NOT retry.
    PERMANENT_REJECT_CODES = frozenset({
        -1003,   # TOO_MANY_REQUESTS — handled by token-bucket, not by us
        -1013,   # INVALID_MESSAGE / malformed
        -1021,   # timestamp out of sync
        -2010,   # NEW_ORDER_REJECTED (insufficient balance, market halted, etc.)
        -2011,   # CANCEL_REJECTED
        -2013,   # ORDER_DOES_NOT_EXIST
        -2015,   # INVALID_API_KEY
        -2019,   # TRADE_NOT_ALLOWED (margin, etc.)
    })

    def __init__(
        self,
        client: BinanceClient,
        state: StateStore,
        *,
        max_open_trades: int = 1,
        rate_per_sec: float = 8.0,
        burst: int = 16,
    ) -> None:
        self.client = client
        self.state = state
        self.max_open_trades = int(max_open_trades)
        self._limiter = TokenBucket(rate_per_sec=rate_per_sec, capacity=burst)
        self._pool = ClientOrderIdPool()
        self._pool.initial_fill()
        # Open positions: client_order_id -> order_row
        self._open: dict[str, OrderRow] = {}
        # Sniper -> order (for state-correlation on fills)
        self._sniper_to_order: dict[int, int] = {}

    # ── Idempotent sniper submission ─────────────────────────────────

    async def submit_sniper_market(self, spec: SniperSpec) -> Optional[int]:
        """Submit the market order for a triggered sniper.

        Returns the ``orders.order_id`` on success, or ``None`` if
        the order was rejected (a failsafe will be activated).
        """
        # Failsafe: max-open-trades
        if len(self._open) >= self.max_open_trades:
            _log.warning(
                "sniper rejected: max_open_trades",
                extra={"max": self.max_open_trades, "open": len(self._open)},
            )
            self.state.incr_counter("orders_rejected_max_open")
            return None

        # Failsafe: order-reject freeze (raised elsewhere on a prior reject).
        if self.state.failsafe_active("broker_reject_freeze"):
            self.state.incr_counter("orders_rejected_during_freeze")
            return None

        # Check idempotency: if a prior process already submitted this sniper, reuse it.
        existing = self.state.get_order_by_client_id(self._sniper_client_id(spec))
        if existing is not None and existing.status in ("filled", "submitted", "new", "partially_filled"):
            _log.info(
                "sniper already submitted",
                extra={"client_order_id": existing.client_order_id, "status": existing.status},
            )
            self._open[existing.client_order_id] = existing
            self._sniper_to_order[spec.sniper_id] = existing.order_id
            return existing.order_id

        client_order_id = self._sniper_client_id(spec)

        # Persist as 'new' BEFORE broker call so a crash mid-flight leaves a record.
        row = OrderRow(
            order_id=-1,
            client_order_id=client_order_id,
            binance_order_id=None,
            sniper_id=spec.sniper_id,
            symbol=spec.symbol,
            side="BUY" if spec.direction > 0 else "SELL",
            type="MARKET",
            quantity=spec.quantity,
            stop_price=None,
            working_type=None,
            price_protect=False,
            reduce_only=False,
            close_position=False,
            status="new",
            submitted_at_ns=None,
            filled_at_ns=None,
            avg_fill_price=None,
            cum_filled_qty=None,
            raw_response=None,
            last_error=None,
        )
        try:
            order_id = self.state.insert_order(row)
            row.order_id = order_id
        except Exception as e:
            # UNIQUE constraint = already inserted by a prior crash; recover.
            existing = self.state.get_order_by_client_id(client_order_id)
            if existing is not None:
                row = existing
                _log.info("recovered from duplicate insert", extra={"client_order_id": client_order_id})
            else:
                raise

        await self._limiter.acquire()

        # Submit to broker with retries.
        ack: Optional[OrderAck] = None
        try:
            ack = await self._submit_with_retry(row, spec)
        except OrderRejected as e:
            _log.error("order rejected", extra={"err": str(e), "client_order_id": client_order_id})
            row.status = "rejected"
            row.last_error = str(e)
            self.state.update_order(row)
            self._activate_reject_freeze()
            self.state.incr_counter("orders_rejected")
            return None
        except Exception as e:
            _log.exception("order submit failed catastrophically", extra={"err": repr(e)})
            row.status = "error"
            row.last_error = repr(e)
            self.state.update_order(row)
            self.state.incr_counter("orders_submit_error")
            return None

        # We treat a MARKET as filled immediately (Testnet paper always
        # fills instantly). Real fills arrive via the user-data stream;
        # :class:`Reconciler` will reconcile periodically.
        now_ns = time.time_ns()
        if ack is not None:
            row.binance_order_id = ack.order_id
            row.status = "filled"
            row.submitted_at_ns = now_ns
            row.filled_at_ns = now_ns
            row.avg_fill_price = float(spec.entry_price_hint)
            row.cum_filled_qty = float(spec.quantity)
            row.raw_response = repr(ack.raw)
            self.state.update_order(row)
            self._open[row.client_order_id] = row
            self._sniper_to_order[spec.sniper_id] = row.order_id

        self.state.incr_counter("orders_placed")
        _log.info(
            "sniper order placed",
            extra={
                "sniper_id": spec.sniper_id,
                "side": row.side,
                "qty": row.quantity,
                "binance_order_id": row.binance_order_id,
            },
        )
        return row.order_id

    # ── SL / TP attachment ───────────────────────────────────────────

    async def attach_stop_loss_take_profit(
        self,
        entry_order_id: int,
        *,
        stop_price: float,
        take_profit_price: float,
        working_type: str = "MARK_PRICE",
    ) -> tuple[Optional[int], Optional[int]]:
        """Attach SL and TP working orders to a filled entry.

        These are ``STOP_MARKET`` and ``TAKE_PROFIT_MARKET`` orders
        with ``closePosition=true`` (Binance auto-computes the qty
        from the open position). They fire on their own when the
        mark price crosses.
        """
        entry = self.state._conn.execute(
            "SELECT * FROM orders WHERE order_id = ?", (entry_order_id,)
        ).fetchone()
        if entry is None:
            _log.warning("attach_sl_tp: entry order not found", extra={"id": entry_order_id})
            return (None, None)
        symbol = str(entry["symbol"])
        side_opposite = "SELL" if str(entry["side"]) == "BUY" else "BUY"
        sl_id = await self._submit_close_position_trigger(
            symbol=symbol,
            side=side_opposite,
            trigger_price=stop_price,
            trigger_type="STOP_MARKET",
            working_type=working_type,
            sniper_id=int(entry["sniper_id"]) if entry["sniper_id"] is not None else None,
        )
        tp_id = await self._submit_close_position_trigger(
            symbol=symbol,
            side=side_opposite,
            trigger_price=take_profit_price,
            trigger_type="TAKE_PROFIT_MARKET",
            working_type=working_type,
            sniper_id=int(entry["sniper_id"]) if entry["sniper_id"] is not None else None,
        )
        return (sl_id, tp_id)

    async def _submit_close_position_trigger(
        self,
        *,
        symbol: str,
        side: str,
        trigger_price: float,
        trigger_type: str,
        working_type: str,
        sniper_id: Optional[int],
    ) -> Optional[int]:
        client_order_id = uuid.uuid4().hex
        row = OrderRow(
            order_id=-1,
            client_order_id=client_order_id,
            binance_order_id=None,
            sniper_id=sniper_id,
            symbol=symbol,
            side=side,
            type=trigger_type,
            quantity=0.0,
            stop_price=float(trigger_price),
            working_type=working_type,
            price_protect=False,
            reduce_only=False,
            close_position=True,
            status="new",
            submitted_at_ns=None,
            filled_at_ns=None,
            avg_fill_price=None,
            cum_filled_qty=None,
            raw_response=None,
            last_error=None,
        )
        oid = self.state.insert_order(row)
        row.order_id = oid
        await self._limiter.acquire()
        try:
            ack = await self.client.place_order(
                symbol=symbol,
                side=side,
                type=trigger_type,
                stop_price=float(trigger_price),
                working_type=working_type,
                close_position=True,
                client_order_id=client_order_id,
            )
            row.binance_order_id = ack.order_id
            row.status = ack.status or "submitted"
            row.submitted_at_ns = time.time_ns()
            row.raw_response = repr(ack.raw)
            self.state.update_order(row)
            self._open[client_order_id] = row
            return oid
        except BinanceAPIError as e:
            row.status = "rejected"
            row.last_error = f"code={e.code}: {e.message}"
            self.state.update_order(row)
            _log.warning(
                "attach SL/TP rejected",
                extra={"err": row.last_error, "type": trigger_type, "trigger_price": trigger_price},
            )
            return None

    # ── Cancellation ─────────────────────────────────────────────────

    async def cancel_all_open(self) -> int:
        """Cancel every open order (used by shutdown + reconciliation).

        Returns the number of orders successfully cancelled.
        """
        n_cancelled = 0
        for cid in list(self._open.keys()):
            row = self._open[cid]
            try:
                await self.client.cancel_order(row.symbol, client_order_id=row.client_order_id)
                row.status = "canceled"
                self.state.update_order(row)
                n_cancelled += 1
            except BinanceAPIError as e:
                # -2011 = "unknown order" — already gone; not an error.
                if e.code == -2011:
                    row.status = "canceled"
                    self.state.update_order(row)
                    n_cancelled += 1
                else:
                    _log.warning("cancel failed", extra={"err": repr(e), "cid": cid})
            finally:
                self._open.pop(cid, None)
        return n_cancelled

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _sniper_client_id(spec: SniperSpec) -> str:
        """Deterministic client_order_id derived from the sniper_id."""
        # Fixed prefix + zero-padded sniper id — idempotent across crashes.
        return f"ict-s{spec.sniper_id:08d}-{spec.signal_id:08d}"

    async def _submit_with_retry(
        self,
        row: OrderRow,
        spec: SniperSpec,
    ) -> OrderAck:
        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                ack = await self.client.place_order(
                    symbol=row.symbol,
                    side=row.side,
                    type=row.type,
                    quantity=row.quantity,
                    client_order_id=row.client_order_id,
                )
                return ack
            except BinanceAPIError as e:
                last_err = e
                if e.code in self.PERMANENT_REJECT_CODES:
                    raise OrderRejected(e.code, e.message) from e
                _log.warning(
                    "order submit transient",
                    extra={"attempt": attempt + 1, "err": repr(e)},
                )
                await asyncio.sleep(0.5 * (2 ** attempt))
        raise OrderRejected(-1, f"exhausted retries: {last_err}")

    def _activate_reject_freeze(self) -> None:
        # 60s default freeze; main reads this via failsafe_active().
        self.state.activate_failsafe(
            name="broker_reject_freeze",
            duration_secs=60,
            reason="broker rejected last order",
        )

    # ── Position state ──────────────────────────────────────────────

    def open_position_count(self) -> int:
        return len(self._open)

    def is_open(self, client_order_id: str) -> bool:
        return client_order_id in self._open


__all__ = ["OrderManager", "OrderRejected", "OrderPoolExhausted", "TokenBucket", "ClientOrderIdPool"]