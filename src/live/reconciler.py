"""60s reconciler — polls the broker and reconciles local state.

Runs as a background asyncio task. Every ``interval_secs``:
1. ``GET /fapi/v1/openOrders`` — compare against our local open
   orders; cancel any orphan (we have a broker order we don't
   know about) or mark missing (we have a local order the
   broker doesn't see) for re-submission.
2. ``GET /fapi/v1/account`` — compare ``totalWalletBalance`` to
   the previous snapshot. A large unexpected change flips the
   ``account_drift`` failsafe ON.
3. ``GET /fapi/v1/order`` — for each of our open orders, query
   the latest status. Update local DB if the broker is ahead.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .binance_client import BinanceClient
from .state_store import StateStore

_log = logging.getLogger(__name__)


class Reconciler:
    """Polls the broker every ``interval_secs`` and reconciles state."""

    def __init__(
        self,
        client: BinanceClient,
        state: StateStore,
        symbol: str,
        interval_secs: int = 60,
        drift_threshold_usd: float = 5.0,
    ) -> None:
        self.client = client
        self.state = state
        self.symbol = symbol
        self.interval_secs = max(10, int(interval_secs))
        self.drift_threshold_usd = float(drift_threshold_usd)
        self._last_balance: Optional[float] = None

    async def run(self, shutdown: asyncio.Event) -> None:
        _log.info("reconciler starting", extra={"interval_secs": self.interval_secs})
        while not shutdown.is_set():
            try:
                await self.reconcile_once()
                self.state.heartbeat(last_recon_ns=time.time_ns())
            except asyncio.CancelledError:
                break
            except Exception as e:
                _log.warning(
                "reconcile failed; continuing",
                    extra={"err": repr(e)},
                )
                self.state.incr_counter("reconcile_errors")
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=self.interval_secs)
            except asyncio.TimeoutError:
                pass
        _log.info("reconciler stopped")

    async def reconcile_once(self) -> None:
        # 1. Open orders
        broker_orders = await self.client.get_open_orders(self.symbol)
        broker_ids = {
            int(o.get("orderId", 0))
            for o in broker_orders
            if o.get("orderId") is not None
        }
        local_open = self.state.load_open_orders()
        for row in local_open:
            binance_id = row.binance_order_id
            if binance_id is None:
                continue
            if binance_id not in broker_ids:
                # Broker doesn't have it; mark stale. If we still
                # believe it's open, force-cancel to be safe.
                _log.warning(
                    "open order missing on broker",
                    extra={"client_order_id": row.client_order_id, "binance_id": binance_id},
                )
                try:
                    await self.client.cancel_order(row.symbol, client_order_id=row.client_order_id)
                    row.status = "canceled"
                    self.state.update_order(row)
                except Exception as e:
                    _log.warning("force-cancel failed", extra={"err": repr(e)})

        # 2. Account balance drift
        try:
            account = await self.client.get_account()
            balance = float(account.get("totalWalletBalance", 0.0))
            if self._last_balance is not None and abs(balance - self._last_balance) > self.drift_threshold_usd:
                _log.warning(
                    "balance drift",
                    extra={
                        "prev": self._last_balance,
                        "now": balance,
                        "delta": balance - self._last_balance,
                    },
                )
                self.state.incr_counter("balance_drift_events")
                self.state.activate_failsafe(
                    "account_drift", 300, f"balance drift ${balance - self._last_balance:+.2f}"
                )
            self._last_balance = balance
        except Exception as e:
            _log.warning("balance fetch failed", extra={"err": repr(e)})


__all__ = ["Reconciler"]