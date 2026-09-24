"""Live trading engine for the v17 SNIPER strategy on Binance USDT-M.

The live engine is a thin shell around the existing detector +
sniper machinery in :mod:`src.core.ict_signals` and
:mod:`src.backtest.ict_backtest`. It does not re-implement the
strategy — it streams bars from the Binance aggTrade websocket,
mirrors the bar-loop's sniper intercept + inversion-watch, and
fires orders to the Binance Testnet via the REST API.

Architecture overview (see ``deploy/README.md`` for the full
runbook):

* :mod:`src.live.config`        — ``.env`` loader (pydantic-settings)
* :mod:`src.live.logger`        — JSON structured logs (loguru)
* :mod:`src.live.state_store`   — SQLite WAL persistence (FVG zones,
                                  snipers, orders, trades, heartbeat)
* :mod:`src.live.binance_client` — async signed REST + WebSocket client
* :mod:`src.live.feed_handler`   — aggTrade WS consumer + heartbeat
* :mod:`src.live.bar_aggregator` — rolling 1s OHLCV buffer
* :mod:`src.live.live_backtest`  — signal-only mirror of the backtest
                                    bar loop (sniper-only)
* :mod:`src.live.order_manager`  — idempotent order placement + pool
* :mod:`src.live.reconciler`     — 60s REST poll for openOrders +
                                    balance drift
* :mod:`src.live.health`         — heartbeat file + HTTP /healthz
* :mod:`src.live.shutdown`       — signal handlers + atexit
* :mod:`src.live.metrics`        — in-memory counters + Prometheus
                                    text output
* :mod:`src.live.main`           — asyncio entrypoint

The only strategy-touching code is :mod:`src.live.live_backtest`,
which imports the canonical recipe from
:mod:`src.core.optimal_config` and mirrors the existing
``run_ict_backtest`` sniper intercept + queue walk.  Every other
module is pure plumbing.

Constants
---------
* ``__version__`` — package version (matches recipe version)
* ``STRATEGY_LABEL`` — label written to every log line and DB row
"""
from __future__ import annotations

__version__ = "1.0.0"
STRATEGY_LABEL = "ict-sniper-v17-paper"

__all__ = ["__version__", "STRATEGY_LABEL"]