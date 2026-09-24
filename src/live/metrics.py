"""In-memory metric counters (lifted to the StateStore for durability).

Most counters are in :class:`src.live.state_store.StateStore` —
the SQLite ``metrics_counters`` table — so they survive a process
restart. This module is a thin facade for the common-case
increments and exposes the Prometheus text-format dump.

The "5 failsafes" counters tracked by the engine are:

* ``bars_received`` — total 1s bars received from the feed
* ``signals_emitted`` — total PendingSignals emitted by the detector
* ``sniper_submitted`` — total snipers pushed to ``pending_snipers``
* ``sniper_triggered`` — total snipers that fired (inversion happened)
* ``sniper_cancelled`` — total snipers cancelled before triggering
* ``orders_placed`` — total orders submitted to the broker
* ``orders_rejected`` — total orders rejected by the broker
* ``orders_rejected_max_open`` — submissions refused by max-open cap
* ``orders_rejected_during_freeze`` — submissions refused by the reject-freeze
* ``trades_closed`` — total trades that exited (TP / SL / inv / EOD)
* ``reconcile_errors`` — reconciler poll failures
* ``balance_drift_events`` — wallet-balance drift > threshold
"""
from __future__ import annotations

import logging
from typing import Iterable

from .state_store import StateStore

_log = logging.getLogger(__name__)


# Names referenced by main / order_manager / reconciler etc.
BAR_RECEIVED = "bars_received"
SIGNALS_EMITTED = "signals_emitted"
SNIPER_SUBMITTED = "sniper_submitted"
SNIPER_TRIGGERED = "sniper_triggered"
SNIPER_CANCELLED = "sniper_cancelled"
ORDERS_PLACED = "orders_placed"
ORDERS_REJECTED = "orders_rejected"
ORDERS_REJECTED_MAX_OPEN = "orders_rejected_max_open"
ORDERS_REJECTED_DURING_FREEZE = "orders_rejected_during_freeze"
TRADES_CLOSED = "trades_closed"
RECONCILE_ERRORS = "reconcile_errors"
BALANCE_DRIFT = "balance_drift_events"


def incr(state: StateStore, name: str, by: float = 1.0) -> None:
    """Bump a counter (forwarded to the StateStore)."""
    try:
        state.incr_counter(name, by)
    except Exception as e:
        _log.warning("metric incr failed", extra={"err": repr(e), "name": name})


def reset_all(state: StateStore, names: Iterable[str]) -> None:
    """Reset a subset of counters to 0 (test-only)."""
    for n in names:
        try:
            state._conn.execute(
                "UPDATE metrics_counters SET value = 0, updated_ns = ? WHERE name = ?",
                (__import__("time").time_ns(), n),
            )
        except Exception as e:
            _log.warning("metric reset failed", extra={"err": repr(e), "name": n})


__all__ = [
    "BAR_RECEIVED",
    "SIGNALS_EMITTED",
    "SNIPER_SUBMITTED",
    "SNIPER_TRIGGERED",
    "SNIPER_CANCELLED",
    "ORDERS_PLACED",
    "ORDERS_REJECTED",
    "ORDERS_REJECTED_MAX_OPEN",
    "ORDERS_REJECTED_DURING_FREEZE",
    "TRADES_CLOSED",
    "RECONCILE_ERRORS",
    "BALANCE_DRIFT",
    "incr",
    "reset_all",
]