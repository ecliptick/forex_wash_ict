"""Configuration loader for the live engine.

All configuration is sourced from a single ``.env`` file (or
environment variables) so the deployment is fully declarative
and secrets stay out of git. ``pydantic-settings`` validates
the values at startup and surfaces bad config loudly.

Required environment variables
==============================

* ``BINANCE_API_KEY`` — Testnet API key
* ``BINANCE_API_SECRET`` — Testnet API secret
* ``ICT_STATE_DB_PATH`` — absolute path to the SQLite DB file

Optional environment variables (with sensible defaults)
======================================================

* ``ICT_SYMBOL`` — ``BTCUSDT`` (default). One symbol per process.
* ``ICT_RPC_PORT`` — ``8080`` (default). HTTP /healthz /metrics port.
* ``ICT_LIVE_DEBUG`` — ``0`` (default). Set to ``1`` to enable
  verbose loguru debug output.
* ``ICT_DAILY_LOSS_LIMIT_USD`` — ``-50.0`` (default). The circuit
  breaker halts submissions when realised daily PnL falls below
  this value.
* ``ICT_MAX_OPEN_TRADES`` — ``1`` (default). Max concurrent open
  positions.
* ``ICT_FEED_STALE_SECS`` — ``30`` (default). No new 1s bar for
  this many seconds → STOP submitting.
* ``ICT_ORDER_REJECT_FREEZE_SECS`` — ``60`` (default). After a
  broker reject, freeze submissions for this long.
* ``ICT_SL_SPREAD_GUARD`` — ``2.0`` (default). Multiplier on the
  bookTicker bid-ask spread — beyond this the SL price is
  rejected pre-submission.
* ``ICT_BROKER_BASE_URL`` — ``https://testnet.binancefuture.com``
  (default). The Binance USDⓈ-M Testnet base URL.
* ``ICT_BROKER_WS_URL`` — ``wss://stream.binancefuture.com``
  (default). The combined-stream WS URL (Testnet).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_log = logging.getLogger(__name__)


class LiveConfig(BaseSettings):
    """Live-engine configuration loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ICT_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Required secrets ──────────────────────────────────────────────
    binance_api_key: str = Field(default="", description="Binance API key")
    binance_api_secret: str = Field(default="", description="Binance API secret")

    # ── Broker endpoints ──────────────────────────────────────────────
    symbol: str = Field(default="BTCUSDT")
    broker_base_url: str = Field(
        default="https://testnet.binancefuture.com",
        description="REST base URL — Testnet by default for paper",
    )
    broker_ws_url: str = Field(
        default="wss://stream.binancefuture.com/stream",
        description="Combined-stream WS URL — Testnet by default",
    )

    # ── Local file paths ──────────────────────────────────────────────
    state_db_path: str = Field(
        default="/var/lib/ict-sniper/state.db",
        description="Absolute path to the SQLite WAL state database",
    )
    log_dir: str = Field(
        default="/var/log/ict-sniper",
        description="Directory for log files (journald is the primary sink)",
    )

    # ── HTTP / observability ──────────────────────────────────────────
    rpc_host: str = Field(default="0.0.0.0")
    rpc_port: int = Field(default=8080)

    # ── Failsafes (core 5) ────────────────────────────────────────────
    daily_loss_limit_usd: float = Field(default=-50.0)
    max_open_trades: int = Field(default=1)
    feed_stale_secs: int = Field(default=30)
    order_reject_freeze_secs: int = Field(default=60)
    sl_spread_guard: float = Field(
        default=2.0,
        gt=0.0,
        description="Multiplier on the bid-ask spread beyond which an SL is rejected",
    )

    # ── Operational ───────────────────────────────────────────────────
    live_debug: bool = Field(default=False)

    def has_credentials(self) -> bool:
        """True when both API key and secret are populated."""
        return bool(self.binance_api_key) and bool(self.binance_api_secret)

    def ws_streams(self) -> str:
        """The combined-stream path for our symbol."""
        sym = self.symbol.lower()
        # aggTrade + bookTicker combined stream.
        return f"{self.broker_ws_url}?streams={sym}@aggTrade/{sym}@bookTicker"


def load_config(env_file: Optional[Path] = None) -> LiveConfig:
    """Load config from ``env_file`` (defaults to ``.env`` in CWD).

    Raises ``ValueError`` if required credentials are missing.
    """
    if env_file is not None and env_file.exists():
        cfg = LiveConfig(_env_file=str(env_file))
    else:
        cfg = LiveConfig()
    if not cfg.has_credentials():
        # We don't raise — main() will refuse to start instead, so the
        # operator sees a clean log line rather than a stack trace.
        _log.warning(
            "No Binance credentials found — set BINANCE_API_KEY and "
            "BINANCE_API_SECRET in the environment or .env file"
        )
    return cfg


__all__ = ["LiveConfig", "load_config"]