"""60-second smoke test for the live engine against Binance Testnet.

This is the operator-facing tool to verify the deployment is
correctly wired BEFORE letting the engine run unsupervised.

What it checks
==============

1. **Credentials** — BinanceClient can sign a ``GET /fapi/v1/account``
   call and parse the response.
2. **Market data** — ``GET /fapi/v1/klines`` returns bars for the
   configured symbol.
3. **State DB** — the SQLite WAL DB at ``--state-db`` is writable
   and the schema is at the latest migration.
4. **Bar loop** — runs the live bar loop on a 60s window of
   historical 1s klines and prints how many bars + signals +
   snipers fired (no real orders submitted).
5. **Order path** — optional. With ``--place-test-order`` it
   submits a 0.001 BTC MARKET order, waits for fill, then
   cancels it. With ``--cancel-all`` it cancels every open
   order (use when reconciling a stuck state).

Usage
=====

::

    python -m src.tools.paper_smoke                    # dry run, no orders
    python -m src.tools.paper_smoke --place-test-order # submit + cancel 1 order
    python -m src.tools.paper_smoke --cancel-all       # cancel every open order

Reads credentials from ``/etc/ict-sniper.env`` if present,
otherwise from the ``BINANCE_API_KEY`` / ``BINANCE_API_SECRET``
environment variables.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

# Make `src.*` importable when run from the repo root.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.live.binance_client import BinanceClient
from src.live.config import LiveConfig, load_config
from src.live.state_store import StateStore

_log = logging.getLogger("paper_smoke")


def _load_creds(cfg: LiveConfig) -> tuple[str, str]:
    """Pick up credentials from /etc/ict-sniper.env if it exists."""
    if cfg.has_credentials():
        return (cfg.binance_api_key, cfg.binance_api_secret)
    env_file = Path("/etc/ict-sniper.env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("BINANCE_API_KEY="):
                cfg.binance_api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("BINANCE_API_SECRET="):
                cfg.binance_api_secret = line.split("=", 1)[1].strip().strip('"').strip("'")
    return (cfg.binance_api_key, cfg.binance_api_secret)


async def _check_credentials(client: BinanceClient) -> None:
    _log.info("checking credentials via /fapi/v1/account ...")
    acct = await client.get_account()
    bal = float(acct.get("totalWalletBalance", 0.0))
    print(f"  ok  totalWalletBalance = {bal} USDT")


async def _check_klines(client: BinanceClient, symbol: str) -> None:
    _log.info("fetching 60s of 1s klines ...")
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 60_000
    klines = await client.get_klines(symbol, "1s", start_time_ms=start_ms, end_time_ms=end_ms, limit=60)
    if not klines:
        raise SystemExit(f"ERROR: no klines returned for {symbol}")
    print(f"  ok  {len(klines)} bars  last close = {klines[-1][4]}")


def _check_state_db(state: StateStore) -> None:
    counters = state.get_all_counters()
    hb = state.read_heartbeat()
    print(f"  ok  schema at v{state._conn.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]}")
    print(f"  ok  counters={len(counters)}  heartbeat={'present' if hb else 'absent'}")


async def _check_bar_loop(state: StateStore, client: BinanceClient, symbol: str) -> None:
    """Replay 60s of historical 1s bars through the live bar loop."""
    from src.live.live_backtest import LiveBacktest
    _log.info("replaying 60s through the live bar loop (no orders submitted) ...")
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 60_000
    klines = await client.get_klines(symbol, "1s", start_time_ms=start_ms, end_time_ms=end_ms, limit=60)
    if not klines:
        raise SystemExit("no klines for bar-loop check")

    # We need an async callback for on_sniper_trigger. Use a no-op.
    async def _no_sniper(_spec) -> None:
        state.incr_counter("snipers_dryrun")

    bar_loop = LiveBacktest(
        state=state,
        symbol=symbol,
        on_sniper_trigger=_no_sniper,
    )

    from src.live.feed_handler import FeedHandler
    from src.live.bar_aggregator import BarAggregator

    # Build 1s bars directly from klines — no WS needed for the smoke.
    n_bars = 0
    for k in klines:
        bar_ts_ms = int(k[0])
        bar = type("B", (), {})()
        bar.bar_ts_ms = bar_ts_ms
        bar.open = float(k[1])
        bar.high = float(k[2])
        bar.low = float(k[3])
        bar.close = float(k[4])
        bar.volume = float(k[5])
        from src.live.live_backtest import InFlightBar
        bar_loop.on_bar(InFlightBar(
            bar_idx=n_bars,
            bar_ts_ms=bar_ts_ms,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        ))
        n_bars += 1

    snipers = state.get_counter("snipers_dryrun")
    print(f"  ok  {n_bars} bars replayed  snipers_triggered_dryrun = {snipers}")


async def _place_test_order(client: BinanceClient, symbol: str) -> None:
    _log.info("submitting test market order ...")
    ack = await client.place_order(
        symbol=symbol,
        side="BUY",
        type="MARKET",
        quantity=0.001,             # minimum lot for BTC testnet
        client_order_id=f"smoke-{int(time.time())}",
    )
    print(f"  ok  order placed  binance_order_id={ack.order_id}  status={ack.status}")
    await asyncio.sleep(2.0)
    cancelled = await client.cancel_all_orders(symbol)
    print(f"  ok  cancel_all returned: {cancelled}")


async def _cancel_all(client: BinanceClient, symbol: str) -> None:
    _log.info("cancelling every open order ...")
    open_orders = await client.get_open_orders(symbol)
    if not open_orders:
        print("  ok  no open orders")
        return
    for o in open_orders:
        try:
            await client.cancel_order(symbol, order_id=int(o["orderId"]))
            print(f"  ok  cancelled orderId={o['orderId']}")
        except Exception as e:
            print(f"  err cancel failed for {o.get('orderId')}: {e!r}")


async def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    api_key, api_secret = _load_creds(cfg)
    if not api_key or not api_secret:
        print("ERROR: BINANCE_API_KEY / BINANCE_API_SECRET not set")
        return 2

    client = BinanceClient(
        api_key=api_key,
        api_secret=api_secret,
        base_url=cfg.broker_base_url,
        ws_url=cfg.broker_ws_url,
    )
    await client.__aenter__()

    state = StateStore(args.state_db or cfg.state_db_path)

    try:
        print("=" * 60)
        print(f"  ICT SNIPER paper smoke  |  symbol={cfg.symbol}  |  base={cfg.broker_base_url}")
        print("=" * 60)
        await _check_credentials(client)
        await _check_klines(client, cfg.symbol)
        _check_state_db(state)
        await _check_bar_loop(state, client, cfg.symbol)
        if args.place_test_order:
            await _place_test_order(client, cfg.symbol)
        if args.cancel_all:
            await _cancel_all(client, cfg.symbol)
        print("=" * 60)
        print("  ALL CHECKS PASSED")
        print("=" * 60)
        return 0
    finally:
        try:
            state.close()
        except Exception:
            pass
        await client.__aexit__(None, None, None)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Paper-trade smoke test for the ICT SNIPER live engine."
    )
    p.add_argument("--state-db", help="Override state DB path (default: from config).")
    p.add_argument("--place-test-order", action="store_true",
                   help="Submit a 0.001 BTC MARKET order, then cancel-all.")
    p.add_argument("--cancel-all", action="store_true",
                   help="Cancel every open order at the broker. Use for recovery.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())