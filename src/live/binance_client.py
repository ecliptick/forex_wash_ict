"""Async Binance USDⓈ-M client (REST + WebSocket).

Only the surface area we actually need:

* :meth:`BinanceClient.place_order` — ``POST /fapi/v1/order``
* :meth:`BinanceClient.cancel_order` — ``DELETE /fapi/v1/order``
* :meth:`BinanceClient.get_open_orders` — ``GET /fapi/v1/openOrders``
* :meth:`BinanceClient.get_account` — ``GET /fapi/v1/account``
* :meth:`BinanceClient.get_klines` — ``GET /fapi/v1/klines``
* :meth:`BinanceClient.stream_aggtrade_bookticker` — combined WS
  consumer that yields typed :class:`AggTrade` /
  :class:`BookTicker` records

Auth
====

Signed endpoints (order placement, account) require HMAC-SHA256
over the query string using the API secret. ``aiohttp`` is used
for HTTP; ``websockets`` is used for the WS layer (lighter than
``aiohttp``'s built-in WS client).  All endpoints default to the
Testnet base URL — production deployment overrides via the
``ICT_BROKER_BASE_URL`` env var.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

import aiohttp
import websockets

_log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Public dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AggTrade:
    """A single Binance aggTrade event."""
    event_time_ms: int       # "E" — when the aggTrade was pushed
    symbol: str              # "s"
    agg_trade_id: int        # "a"
    price: float             # "p"
    quantity: float          # "q"
    first_trade_id: int      # "f"
    last_trade_id: int       # "l"
    transact_time_ms: int    # "T"
    is_buyer_maker: bool     # "m"


@dataclass
class BookTicker:
    """Best bid/ask snapshot from the bookTicker stream."""
    event_time_ms: int
    symbol: str
    bid_price: float
    bid_qty: float
    ask_price: float
    ask_qty: float


@dataclass
class OrderAck:
    """Response from POST /fapi/v1/order."""
    order_id: int
    client_order_id: str
    symbol: str
    side: str
    type: str
    quantity: float
    stop_price: Optional[float]
    working_type: Optional[str]
    status: str
    raw: dict[str, Any]


# ─────────────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────────────

class BinanceAPIError(Exception):
    """A signed REST call failed (HTTP or business error)."""

    def __init__(self, status: int, code: int, message: str, payload: Any = None) -> None:
        super().__init__(f"HTTP {status}, code={code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.payload = payload


# ─────────────────────────────────────────────────────────────────────────────
# Signing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sign(query: str, secret: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


# ─────────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────────

class BinanceClient:
    """Async Binance USDⓈ-M Testnet client."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = "https://testnet.binancefuture.com",
        ws_url: str = "wss://stream.binancefuture.com/stream",
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("api_key and api_secret are required")
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.ws_url = ws_url.rstrip("/")
        self._session = session
        self._owned_session = session is None

    async def __aenter__(self) -> "BinanceClient":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owned_session and self._session is not None:
            await self._session.close()
            self._session = None

    # ── REST: signed & unsigned ───────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        signed: bool = False,
    ) -> Any:
        assert self._session is not None, "use `async with BinanceClient(...)`"
        params = dict(params or {})
        headers = {"X-MBX-APIKEY": self.api_key}
        if signed:
            params["timestamp"] = _now_ms()
            params.setdefault("recvWindow", 5000)
            query = urllib.parse.urlencode(params)
            params["signature"] = _sign(query, self.api_secret)
        url = f"{self.base_url}{path}"
        async with self._session.request(method, url, params=params, headers=headers) as resp:
            text = await resp.text()
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError:
                data = {"raw": text}
            if resp.status >= 400:
                code = data.get("code", -1) if isinstance(data, dict) else -1
                msg = data.get("msg", text) if isinstance(data, dict) else text
                raise BinanceAPIError(resp.status, int(code), str(msg), data)
            return data

    # ── Market data (unsigned) ─────────────────────────────────────────

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time_ms: Optional[int] = None,
        end_time_ms: Optional[int] = None,
        limit: int = 1000,
    ) -> list[list[Any]]:
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": min(int(limit), 1500),
        }
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        if end_time_ms is not None:
            params["endTime"] = int(end_time_ms)
        data = await self._request("GET", "/fapi/v1/klines", params=params)
        return data if isinstance(data, list) else []

    # ── Trading (signed) ──────────────────────────────────────────────

    async def place_order(
        self,
        symbol: str,
        side: str,
        type: str,                              # MARKET | STOP_MARKET | TAKE_PROFIT_MARKET
        *,
        quantity: Optional[float] = None,
        stop_price: Optional[float] = None,
        working_type: Optional[str] = None,     # MARK_PRICE | CONTRACT_PRICE
        price_protect: bool = False,
        reduce_only: bool = False,
        close_position: bool = False,
        client_order_id: Optional[str] = None,
        new_order_resp_type: str = "RESULT",
        time_in_force: str = "GTC",
    ) -> OrderAck:
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": type.upper(),
            "newOrderRespType": new_order_resp_type,
        }
        if quantity is not None:
            params["quantity"] = float(quantity)
        if stop_price is not None:
            params["stopPrice"] = float(stop_price)
        if working_type is not None:
            params["workingType"] = working_type
        if price_protect:
            params["priceProtect"] = "true"
        if reduce_only:
            params["reduceOnly"] = "true"
        if close_position:
            params["closePosition"] = "true"
        if client_order_id is not None:
            params["newClientOrderId"] = client_order_id
        if type.upper() != "MARKET":
            params["timeInForce"] = time_in_force
        data = await self._request("POST", "/fapi/v1/order", params=params, signed=True)
        return _to_order_ack(data)

    async def cancel_order(
        self,
        symbol: str,
        *,
        order_id: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if order_id is not None:
            params["orderId"] = int(order_id)
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        if "orderId" not in params and "origClientOrderId" not in params:
            raise ValueError("cancel_order requires order_id or client_order_id")
        return await self._request("DELETE", "/fapi/v1/order", params=params, signed=True)

    async def cancel_all_orders(self, symbol: str) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            "/fapi/v1/allOpenOrders",
            params={"symbol": symbol.upper()},
            signed=True,
        )

    async def get_order(
        self,
        symbol: str,
        *,
        order_id: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if order_id is not None:
            params["orderId"] = int(order_id)
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        return await self._request("GET", "/fapi/v1/order", params=params, signed=True)

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if symbol is not None:
            params["symbol"] = symbol.upper()
        data = await self._request("GET", "/fapi/v1/openOrders", params=params, signed=True)
        return data if isinstance(data, list) else []

    async def get_account(self) -> dict[str, Any]:
        return await self._request("GET", "/fapi/v1/account", signed=True)

    # ── WebSocket: combined aggTrade + bookTicker ──────────────────────

    async def stream_aggtrade_bookticker(
        self,
        symbol: str,
    ) -> AsyncIterator[AggTrade | BookTicker]:
        """Yield typed AggTrade / BookTicker events from the combined stream.

        Reconnects with exponential backoff on disconnect; the
        consumer sees a continuous stream across reconnects.
        """
        url = (
            f"{self.ws_url}?streams="
            f"{symbol.lower()}@aggTrade/{symbol.lower()}@bookTicker"
        )
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    backoff = 1.0
                    _log.info("ws connected", extra={"url": url})
                    async for raw in ws:
                        msg = json.loads(raw)
                        stream = msg.get("stream", "")
                        data = msg.get("data", {})
                        if stream.endswith("@aggTrade"):
                            yield _to_agg_trade(data)
                        elif stream.endswith("@bookTicker"):
                            yield _to_book_ticker(data)
            except (websockets.WebSocketException, asyncio.IncompleteReadError, ConnectionError) as e:
                _log.warning(
                    "ws disconnected; reconnecting",
                    extra={"err": repr(e), "backoff_secs": backoff},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)


# ─────────────────────────────────────────────────────────────────────────────
# Converter helpers
# ─────────────────────────────────────────────────────────────────────────────

def _to_agg_trade(d: dict[str, Any]) -> AggTrade:
    return AggTrade(
        event_time_ms=int(d.get("E", 0)),
        symbol=str(d.get("s", "")),
        agg_trade_id=int(d.get("a", 0)),
        price=float(d.get("p", 0.0)),
        quantity=float(d.get("q", 0.0)),
        first_trade_id=int(d.get("f", 0)),
        last_trade_id=int(d.get("l", 0)),
        transact_time_ms=int(d.get("T", 0)),
        is_buyer_maker=bool(d.get("m", False)),
    )


def _to_book_ticker(d: dict[str, Any]) -> BookTicker:
    return BookTicker(
        event_time_ms=int(d.get("E", 0)),
        symbol=str(d.get("s", "")),
        bid_price=float(d.get("b", 0.0)),
        bid_qty=float(d.get("B", 0.0)),
        ask_price=float(d.get("a", 0.0)),
        ask_qty=float(d.get("A", 0.0)),
    )


def _to_order_ack(d: dict[str, Any]) -> OrderAck:
    return OrderAck(
        order_id=int(d.get("orderId", 0)),
        client_order_id=str(d.get("clientOrderId", "")),
        symbol=str(d.get("symbol", "")),
        side=str(d.get("side", "")),
        type=str(d.get("type", "")),
        quantity=float(d.get("origQty", 0.0) or d.get("quantity", 0.0)),
        stop_price=float(d["stopPrice"]) if d.get("stopPrice") not in (None, "", "0", 0, 0.0) else None,
        working_type=str(d.get("workingType")) if d.get("workingType") else None,
        status=str(d.get("status", "")),
        raw=d,
    )


__all__ = [
    "AggTrade",
    "BookTicker",
    "OrderAck",
    "BinanceAPIError",
    "BinanceClient",
]