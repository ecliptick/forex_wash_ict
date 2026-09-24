"""HTTP /healthz + /metrics endpoint for systemd / Prometheus.

Runs as a tiny ``aiohttp`` web server on the configured RPC port.
Endpoints:

* ``GET /healthz`` — 200 OK if the engine has had a feed event
  within ``feed_stale_secs``; 503 Service Unavailable otherwise.
  Response body is JSON with the heartbeat table.
* ``GET /metrics`` — Prometheus text format. Counts every
  ``metrics_counters`` row plus the failsafe state.
* ``GET /status`` — JSON snapshot of the state DB: open
  positions, today's PnL, failsafes active.

This module deliberately avoids pulling in a Prometheus client
library; the format is simple enough to write by hand.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from aiohttp import web

from .state_store import StateStore

_log = logging.getLogger(__name__)


def _prom_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class HealthServer:
    def __init__(
        self,
        state: StateStore,
        feed_stale_secs: int,
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self.state = state
        self.feed_stale_secs = int(feed_stale_secs)
        self.host = str(host)
        self.port = int(port)
        self._app = web.Application()
        self._app.router.add_get("/healthz", self._healthz)
        self._app.router.add_get("/metrics", self._metrics)
        self._app.router.add_get("/status", self._status)
        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host=self.host, port=self.port)
        await site.start()
        _log.info("health server up", extra={"host": self.host, "port": self.port})

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _healthz(self, _request: web.Request) -> web.Response:
        hb = self.state.read_heartbeat()
        last_feed = int(hb.get("last_feed_ns") or 0)
        last_bar = int(hb.get("last_bar_ns") or 0)
        now_ns = time.time_ns()
        feed_age = (now_ns - last_feed) / 1e9 if last_feed else float("inf")
        bar_age = (now_ns - last_bar) / 1e9 if last_bar else float("inf")
        healthy = feed_age < self.feed_stale_secs
        payload = {
            "healthy": healthy,
            "feed_age_secs": round(feed_age, 3),
            "bar_age_secs": round(bar_age, 3),
            "feed_stale_threshold_secs": self.feed_stale_secs,
            "process_start_ns": hb.get("process_start_ns"),
            "uptime_secs": hb.get("uptime_secs"),
            "last_state": hb.get("last_state"),
        }
        return web.json_response(payload, status=200 if healthy else 503)

    async def _metrics(self, _request: web.Request) -> web.Response:
        lines: list[str] = []
        for name, value in self.state.get_all_counters().items():
            lines.append(f"ict_{_prom_escape(name)} {value}")
        # Adds the heartbeat-derived gauges
        hb = self.state.read_heartbeat()
        if hb:
            lines.append(f"ict_uptime_secs {hb.get('uptime_secs', 0.0)}")
            now_ns = time.time_ns()
            for field in ("last_feed_ns", "last_bar_ns", "last_order_ns", "last_recon_ns"):
                v = hb.get(field)
                if v:
                    lines.append(f"ict_{field}_age_secs {(now_ns - int(v)) / 1e9:.3f}")
        body = "\n".join(lines) + "\n"
        return web.Response(
            body=body, charset="utf-8", text=body,
            content_type="text/plain",
        )

    async def _status(self, _request: web.Request) -> web.Response:
        day_iso = time.strftime("%Y-%m-%d")
        n_trades, pnl_usd, fee_usd = self.state.daily_pnl_today(day_iso)
        counters = self.state.get_all_counters()
        return web.json_response({
            "day": day_iso,
            "n_trades": n_trades,
            "pnl_usd": pnl_usd,
            "fee_usd": fee_usd,
            "counters": counters,
            "heartbeat": self.state.read_heartbeat(),
            "open_orders": [
                {
                    "client_order_id": o.client_order_id,
                    "side": o.side,
                    "type": o.type,
                    "quantity": o.quantity,
                    "stop_price": o.stop_price,
                    "status": o.status,
                }
                for o in self.state.load_open_orders()
            ],
        })


__all__ = ["HealthServer"]