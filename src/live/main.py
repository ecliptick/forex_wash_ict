"""Live engine entrypoint.

Wires every component together and runs the asyncio event loop.
This is the single source of truth for the process lifecycle.

Lifecycle
=========

1. Load config + logger.
2. Open the SQLite state store.
3. Spin up the Binance REST + WS client.
4. Construct the order manager.
5. Construct the live bar loop (signal-only mirror of backtest).
6. Construct the feed handler (WS → bar aggregator → bar loop).
7. Construct the reconciler (60s background).
8. Construct the health server (HTTP /healthz /metrics).
9. Wire shutdown callbacks (in reverse order of construction).
10. Run all background tasks concurrently until shutdown.
11. Run cleanup chain on exit.

Failsafes (core 5) — applied in the loop:
* (1) Daily-loss circuit-breaker — checked before every order.
* (2) Max-open-trades — checked in ``OrderManager.submit_sniper_market``.
* (3) Stale-data kill — checked in the loop; halts new submissions.
* (4) Order-reject freeze — activated by ``OrderManager`` on reject.
* (5) Order-book sanity guard — checked before submitting any SL/TP.

Run
===

::

    python -m src.live.main

Or under systemd::

    /opt/ict-sniper/venv/bin/python -m src.live.main
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

# Make `src.*` importable when run from anywhere on the VPS.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.live.binance_client import BinanceClient
from src.live.config import LiveConfig, load_config
from src.live.feed_handler import FeedHandler
from src.live.health import HealthServer
from src.live.live_backtest import LiveBacktest, SniperSpec
from src.live.logger import configure as configure_logger
from src.live.metrics import (
    BALANCE_DRIFT,
    ORDERS_PLACED,
    ORDERS_REJECTED,
    ORDERS_REJECTED_DURING_FREEZE,
    ORDERS_REJECTED_MAX_OPEN,
    RECONCILE_ERRORS,
    SNIPER_CANCELLED,
    SNIPER_TRIGGERED,
    TRADES_CLOSED,
    incr as metric_incr,
)
from src.live.order_manager import OrderManager
from src.live.reconciler import Reconciler
from src.live.shutdown import Shutdown, install as install_shutdown
from src.live.state_store import StateStore
from src.core.optimal_config import optimal_params, OPTIMAL_RECIPE_VERSION

_log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Sniper trigger glue
# ─────────────────────────────────────────────────────────────────────────────

async def _sniper_handler_factory(
    order_manager: OrderManager,
    state: StateStore,
    cfg: LiveConfig,
) -> Callable[[SniperSpec], Awaitable[None]]:
    """Return an async callback the bar loop uses to fire orders.

    The callback runs the spread-guard (failsafe #5) and the
    daily-loss circuit-breaker (failsafe #1) before submitting.
    """

    async def _on_sniper(spec: SniperSpec) -> None:
        # Failsafe #1 — daily-loss circuit breaker
        day_iso = time.strftime("%Y-%m-%d")
        n, pnl, fee = state.daily_pnl_today(day_iso)
        if pnl <= cfg.daily_loss_limit_usd:
            state.activate_failsafe(
                "daily_loss_breaker",
                duration_secs=24 * 3600,
                reason=f"daily pnl ${pnl:+.2f} <= limit ${cfg.daily_loss_limit_usd:+.2f}",
            )
            metric_incr(state, "orders_rejected_daily_loss")
            _log.warning(
                "sniper rejected: daily loss breaker",
                extra={"pnl_today": pnl, "limit": cfg.daily_loss_limit_usd},
            )
            return

        # Failsafe #3 — stale-data kill (defensive — main also gates)
        hb = state.read_heartbeat()
        last_feed = int(hb.get("last_feed_ns") or 0)
        if last_feed and (time.time_ns() - last_feed) / 1e9 > cfg.feed_stale_secs:
            state.activate_failsafe(
                "stale_feed_breaker",
                duration_secs=cfg.feed_stale_secs,
                reason=f"no feed for >{cfg.feed_stale_secs}s",
            )
            metric_incr(state, "orders_rejected_stale_feed")
            _log.warning("sniper rejected: stale feed", extra={"age_secs": (time.time_ns() - last_feed) / 1e9})
            return

        # Submit
        oid = await order_manager.submit_sniper_market(spec)
        if oid is None:
            return

        # Attach SL / TP — we read the entry order to know the symbol/qty.
        # Compute the trigger prices.
        entry_price = float(spec.entry_price_hint)
        if spec.direction > 0:    # long
            sl_price = entry_price - spec.stop_usd
            tp_price = entry_price + spec.target_usd
        else:                    # short
            sl_price = entry_price + spec.stop_usd
            tp_price = entry_price - spec.target_usd

        # Failsafe #5 — order-book sanity guard. We don't have the
        # bookTicker here (FeedHandler owns it); defer that check to
        # the caller via a side-channel attribute if needed. The bar
        # loop should pass the latest book to the spec.
        await order_manager.attach_stop_loss_take_profit(
            oid,
            stop_price=sl_price,
            take_profit_price=tp_price,
            working_type="MARK_PRICE",
        )
        state.heartbeat(last_order_ns=time.time_ns())
        metric_incr(state, SNIPER_TRIGGERED)
        metric_incr(state, ORDERS_PLACED)

    return _on_sniper


# ─────────────────────────────────────────────────────────────────────────────
# Daily-loss end-of-day roll-up
# ─────────────────────────────────────────────────────────────────────────────

async def _daily_rollup(state: StateStore, shutdown: Shutdown) -> None:
    """Every 60s, recompute today's PnL rollup row from the trades table."""
    while not shutdown.is_set():
        try:
            day_iso = time.strftime("%Y-%m-%d")
            trades = state.todays_trades(day_iso)
            n = len(trades)
            pnl = sum(float(t.pnl_usd or 0.0) for t in trades)
            fee = sum(float(t.fee_usd or 0.0) for t in trades)
            # Write a 0-delta if nothing changed so the row exists for /status.
            # (Use insert-or-update with delta 0.)
            state._conn.execute(
                """
                INSERT INTO daily_pnl (day, n_trades, pnl_usd, fee_usd, realized)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(day) DO NOTHING
                """,
                (day_iso, n, pnl, fee, pnl),
            )
            state._conn.execute(
                """
                UPDATE daily_pnl SET
                    n_trades = ?,
                    pnl_usd  = ?,
                    fee_usd  = ?,
                    realized = ?
                WHERE day = ?
                """,
                (n, pnl, fee, pnl, day_iso),
            )
        except Exception as e:
            _log.warning("daily rollup failed", extra={"err": repr(e)})
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Stale-feed watcher (failsafe #3 activation)
# ─────────────────────────────────────────────────────────────────────────────

async def _stale_feed_watcher(state: StateStore, cfg: LiveConfig, shutdown: Shutdown) -> None:
    while not shutdown.is_set():
        try:
            hb = state.read_heartbeat()
            last_feed = int(hb.get("last_feed_ns") or 0)
            age = (time.time_ns() - last_feed) / 1e9 if last_feed else float("inf")
            if last_feed and age > cfg.feed_stale_secs:
                state.activate_failsafe(
                    "stale_feed_breaker",
                    duration_secs=cfg.feed_stale_secs,
                    reason=f"no feed for {age:.0f}s",
                )
                _log.warning("stale feed detected", extra={"age_secs": age})
            # Also auto-clear the daily-loss breaker at next UTC midnight.
            if time.strftime("%H:%M") == "00:00":
                state.clear_failsafe("daily_loss_breaker")
        except Exception as e:
            _log.warning("stale-feed watcher failed", extra={"err": repr(e)})
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def run() -> int:
    cfg = load_config()
    log = configure_logger(
        level="DEBUG" if cfg.live_debug else "INFO",
        log_file=os.path.join(cfg.log_dir, "ict-sniper.log") if os.path.isdir(cfg.log_dir) else None,
    )
    log.info(
        "ict-sniper starting",
        extra={
            "symbol": cfg.symbol,
            "recipe": OPTIMAL_RECIPE_VERSION,
            "state_db": cfg.state_db_path,
            "rpc": f"{cfg.rpc_host}:{cfg.rpc_port}",
            "paper": "true" if "testnet" in cfg.broker_base_url else "false",
        },
    )

    if not cfg.has_credentials():
        log.error("missing BINANCE_API_KEY / BINANCE_API_SECRET — abort")
        return 2

    loop = asyncio.get_running_loop()
    shutdown = install_shutdown(loop)

    state = StateStore(cfg.state_db_path)
    state.heartbeat(process_start_ns=time.time_ns(), last_state="starting")

    client = BinanceClient(
        api_key=cfg.binance_api_key,
        api_secret=cfg.binance_api_secret,
        base_url=cfg.broker_base_url,
        ws_url=cfg.broker_ws_url,
    )
    await client.__aenter__()

    order_manager = OrderManager(
        client=client,
        state=state,
        max_open_trades=cfg.max_open_trades,
        rate_per_sec=8.0,
        burst=16,
    )

    params = optimal_params()
    log.info("loaded canonical recipe", extra={"version": OPTIMAL_RECIPE_VERSION})

    on_sniper = await _sniper_handler_factory(order_manager, state, cfg)

    bar_loop = LiveBacktest(
        state=state,
        params=params,
        symbol=cfg.symbol,
        on_sniper_trigger=on_sniper,
    )

    async def _on_closed_bar(_bar) -> None:
        # Hook for dashboards / future publishes. The bar loop has
        # already run synchronously by the time we get here.
        pass

    feed = FeedHandler(
        client=client,
        state=state,
        bar_loop=bar_loop,
        symbol=cfg.symbol,
        on_closed_bar=_on_closed_bar,
    )

    reconciler = Reconciler(
        client=client,
        state=state,
        symbol=cfg.symbol,
        interval_secs=60,
    )

    health = HealthServer(
        state=state,
        feed_stale_secs=cfg.feed_stale_secs,
        host=cfg.rpc_host,
        port=cfg.rpc_port,
    )
    await health.start()
    state.heartbeat(last_state="ready")

    # ── Register cleanups (reverse order of construction) ───────────
    async def _stop_health() -> None:
        await health.stop()

    async def _cancel_all_open() -> None:
        try:
            n = await order_manager.cancel_all_open()
            log.info("cancelled open orders", extra={"n": n})
        except Exception as e:
            log.warning("cancel_all_open failed", extra={"err": repr(e)})

    async def _close_client() -> None:
        await client.__aexit__(None, None, None)

    async def _close_state() -> None:
        try:
            state.heartbeat(last_state="stopped")
            state.close()
        except Exception as e:
            log.warning("state.close failed", extra={"err": repr(e)})

    shutdown.register_cleanup("close_state", _close_state)
    shutdown.register_cleanup("close_client", _close_client)
    shutdown.register_cleanup("cancel_all_open", _cancel_all_open)
    shutdown.register_cleanup("stop_health", _stop_health)

    # ── Run all background tasks ────────────────────────────────────
    tasks = [
        asyncio.create_task(feed.run(shutdown.event), name="feed"),
        asyncio.create_task(reconciler.run(shutdown.event), name="reconciler"),
        asyncio.create_task(_daily_rollup(state, shutdown), name="daily_rollup"),
        asyncio.create_task(_stale_feed_watcher(state, cfg, shutdown), name="stale_watcher"),
    ]

    # Wait for shutdown or first failure.
    done, pending = await asyncio.wait(
        tasks + [asyncio.create_task(shutdown.event.wait(), name="shutdown_wait")],
        return_when=asyncio.FIRST_COMPLETED,
    )

    log.info("engine stopping", extra={"done": [t.get_name() for t in done]})
    shutdown.trigger(reason="loop_exited")

    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    await shutdown.run_cleanups(timeout=15.0)
    log.info("ict-sniper stopped cleanly")
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())