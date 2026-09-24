"""Structured JSON logger for the live engine.

Every log line is emitted as a single JSON object so journald /
log aggregators (Loki, Datadog, etc.) can parse fields without
regex. The loguru library handles formatting + sink management
in a single line.

Log levels
==========

* ``DEBUG``     — verbose per-bar / per-tick trace; off by default
* ``INFO``      — bar-loop milestones, order placements, fills,
                  zone lifecycle events
* ``WARNING``   — recoverable anomalies (reconciler drift, retried
                  requests, fallbacks)
* ``ERROR``     — broker rejects, feed disconnects that exceeded
                  retry budget
* ``CRITICAL``  — anything that requires human attention

Sinks
=====

* stderr (JSON) — primary sink; systemd captures and forwards to
  journald. Use ``journalctl -u ict-sniper -f`` to tail.
* Optional file sink — disabled by default; enable by passing
  ``log_file=/var/log/ict-sniper/ict-sniper.log`` to
  :func:`configure`.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any, Optional

import loguru

# module-level singleton (loguru's recommended pattern)
_logger_singleton: Optional["loguru.Logger"] = None


def _json_sink(message: "loguru.Message") -> None:
    """Write a single JSON line per log record."""
    rec = message.record
    payload: dict[str, Any] = {
        "ts": rec["time"].isoformat(),
        "level": rec["level"].name,
        "msg": rec["message"],
        "module": rec["name"],
        "func": rec["function"],
        "line": rec["line"],
    }
    # Surface any extra={"..."} fields the caller passed.
    extra = rec.get("extra") or {}
    if extra:
        # Coerce non-JSON-serialisable values to strings.
        for k, v in extra.items():
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
    line = json.dumps(payload, separators=(",", ":"))
    print(line, file=sys.stderr, flush=True)


def configure(
    level: str = "INFO",
    log_file: Optional[str] = None,
) -> "loguru.Logger":
    """Configure the global loguru logger with our JSON sink.

    Idempotent — calling twice re-binds the sink cleanly.
    """
    global _logger_singleton
    logger = loguru.logger
    logger.remove()
    logger.add(
        _json_sink,
        level=level,
        enqueue=True,             # async-safe (we run on asyncio)
        backtrace=False,
        diagnose=False,
        colorize=False,
    )
    if log_file:
        logger.add(
            log_file,
            level=level,
            rotation="100 MB",
            retention="14 days",
            compression="gz",
            enqueue=True,
            serialize=True,        # file sink also JSON
        )
    # Mute noisy 3rd-party loggers that aren't relevant for ops.
    for noisy in ("asyncio", "aiohttp.access", "websockets.client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _logger_singleton = logger
    return logger


def get_logger() -> "loguru.Logger":
    """Return the singleton logger (configure() must have run first)."""
    if _logger_singleton is None:
        return configure()
    return _logger_singleton


__all__ = ["configure", "get_logger"]