"""SQLite WAL state store for the live engine.

This module is the only place that touches the database. All
other modules go through the small ``StateStore`` API which
keeps the SQL out of the hot path and gives us one migration
story.

Design goals
============

* **Resumability** — a process restart mid-bar must not lose
  any open state. ``FvgZone`` lifecycle, ``pending_snipers``,
  ``bos_choch_events`` deque, and every ``orders``/``trades``
  row must round-trip from disk.
* **Low latency** — every per-bar write (zone update, sniper
  trigger) is a single ``UPDATE`` or ``INSERT``. The schema
  uses integer primary keys for the hot tables.
* **Single writer** — SQLite WAL allows concurrent readers but
  only one writer. The live engine is the only writer; the
  reconciler reads through the same connection's read-only
  views. ``PRAGMA busy_timeout`` keeps transient locks cheap.
* **Crash safety** — ``PRAGMA synchronous=NORMAL`` trades a
  tiny risk of losing the last transaction on power loss for
  ~10x throughput; for a paper deployment this is fine.

Schema migrations
=================

Migrations live in :class:`Migrations` and are applied in order
at startup. The ``schema_version`` table records the current
version. A migration is a forward-only SQL script wrapped in
``BEGIN``/``COMMIT``.

API
===

The :class:`StateStore` class exposes typed methods:

* :meth:`load_live_zones` / :meth:`upsert_zone` / :meth:`kill_zone`
* :meth:`push_bos_choch` / :meth:`load_bos_choch_memory`
* :meth:`insert_sniper` / :meth:`update_sniper_status` /
  :meth:`load_pending_snipers`
* :meth:`insert_order` / :meth:`update_order` / :meth:`get_order_by_client_id`
* :meth:`insert_trade` / :meth:`update_trade_exit` /
  :meth:`todays_trades`
* :meth:`heartbeat` / :meth:`read_heartbeat`
* :meth:`daily_pnl_today`
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence


# ─────────────────────────────────────────────────────────────────────────────
# Migrations
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA_VERSION = 1


_MIGRATION_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fvg_zones (
    zone_id          INTEGER PRIMARY KEY,
    trigger_bar      INTEGER NOT NULL,
    trigger_time_ns  INTEGER NOT NULL,
    direction        INTEGER NOT NULL,
    zone_low         REAL NOT NULL,
    zone_high        REAL NOT NULL,
    mitigated_bar    INTEGER DEFAULT -1,
    pierced_bar      INTEGER DEFAULT -1,
    inverted_bar     INTEGER DEFAULT -1,
    inverted         INTEGER DEFAULT 0,
    consumed_bar     INTEGER DEFAULT -1,
    expired_bar      INTEGER DEFAULT -1,
    superseded_bar   INTEGER DEFAULT -1,
    played_out_bar   INTEGER DEFAULT -1,
    live             INTEGER DEFAULT 1,
    n_touches        INTEGER DEFAULT 0,
    structure_invalidated INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bos_choch_events (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bar   INTEGER NOT NULL,
    kind  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_snipers (
    sniper_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id        INTEGER NOT NULL,
    submit_bar       INTEGER NOT NULL,
    submit_time_ns   INTEGER NOT NULL,
    direction        INTEGER NOT NULL,
    zone_id          INTEGER NOT NULL REFERENCES fvg_zones(zone_id),
    triggered_by     TEXT NOT NULL,
    trigger_price    REAL NOT NULL,
    stop_usd         REAL NOT NULL,
    target_usd       REAL NOT NULL,
    lots             REAL NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at_ns    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snipers_status
    ON pending_snipers(status);
CREATE INDEX IF NOT EXISTS idx_snipers_zone
    ON pending_snipers(zone_id);

CREATE TABLE IF NOT EXISTS orders (
    order_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id   TEXT UNIQUE NOT NULL,
    binance_order_id  INTEGER,
    sniper_id         INTEGER REFERENCES pending_snipers(sniper_id),
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    type              TEXT NOT NULL,
    quantity          REAL NOT NULL,
    stop_price        REAL,
    working_type      TEXT,
    price_protect     INTEGER DEFAULT 0,
    reduce_only       INTEGER DEFAULT 0,
    close_position    INTEGER DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'new',
    submitted_at_ns   INTEGER,
    filled_at_ns      INTEGER,
    avg_fill_price    REAL,
    cum_filled_qty    REAL,
    raw_response      TEXT,
    last_error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_client ON orders(client_order_id);

CREATE TABLE IF NOT EXISTS trades (
    trade_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_order_id      INTEGER REFERENCES orders(order_id),
    exit_order_id       INTEGER REFERENCES orders(order_id),
    symbol              TEXT NOT NULL,
    direction           INTEGER NOT NULL,
    entry_bar           INTEGER NOT NULL,
    entry_time_ns       INTEGER NOT NULL,
    entry_price         REAL NOT NULL,
    exit_time_ns        INTEGER,
    exit_price          REAL,
    quantity            REAL NOT NULL,
    stop_usd            REAL NOT NULL,
    target_usd          REAL NOT NULL,
    exit_reason         TEXT,
    hold_secs           REAL,
    pnl_usd             REAL,
    fee_usd             REAL,
    entry_triggered_by  TEXT NOT NULL,
    rank_tier           TEXT,
    entry_alignment     TEXT,
    notes               TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_entry ON trades(entry_time_ns);
CREATE INDEX IF NOT EXISTS idx_trades_day   ON trades(entry_time_ns);

CREATE TABLE IF NOT EXISTS daily_pnl (
    day         TEXT PRIMARY KEY,
    n_trades    INTEGER DEFAULT 0,
    pnl_usd     REAL DEFAULT 0,
    fee_usd     REAL DEFAULT 0,
    realized    REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS heartbeat (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    last_bar_ns       INTEGER,
    last_feed_ns      INTEGER,
    last_order_ns     INTEGER,
    last_recon_ns     INTEGER,
    process_start_ns  INTEGER,
    uptime_secs       REAL,
    last_state        TEXT
);

CREATE TABLE IF NOT EXISTS failsafes (
    name            TEXT PRIMARY KEY,
    active_until_ns INTEGER NOT NULL,
    reason          TEXT
);

CREATE TABLE IF NOT EXISTS metrics_counters (
    name        TEXT PRIMARY KEY,
    value       REAL NOT NULL DEFAULT 0,
    updated_ns  INTEGER NOT NULL
);
"""


class Migrations:
    """Forward-only schema migrations.

    Apply in order; track current version in the
    ``schema_version`` table. Adding a new migration is a matter
    of appending a new SQL script to :data:`_ALL` and bumping
    :data:`SCHEMA_VERSION`.
    """

    _ALL: Sequence[str] = (_MIGRATION_V1,)

    @classmethod
    def apply(cls, conn: sqlite3.Connection) -> None:
        cur = conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "  version INTEGER PRIMARY KEY,"
            "  applied_at INTEGER NOT NULL"
            ")"
        )
        cur.close()
        current_row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version"
        ).fetchone()
        current = int(current_row[0]) if current_row else 0
        now_ns = time.time_ns()
        for idx in range(current, len(cls._ALL)):
            sql = cls._ALL[idx]
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (idx + 1, now_ns),
            )
        conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Public dataclasses (used by callers)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ZoneRow:
    """A persisted FVG zone."""
    zone_id: int
    trigger_bar: int
    trigger_time_ns: int
    direction: int
    zone_low: float
    zone_high: float
    mitigated_bar: int = -1
    pierced_bar: int = -1
    inverted_bar: int = -1
    inverted: bool = False
    consumed_bar: int = -1
    expired_bar: int = -1
    superseded_bar: int = -1
    played_out_bar: int = -1
    live: bool = True
    n_touches: int = 0
    structure_invalidated: bool = False

    def __repr__(self) -> str:
        return (
            f"ZoneRow(id={self.zone_id}, dir={self.direction:+d}, "
            f"z=[{self.zone_low:.2f},{self.zone_high:.2f}], "
            f"inv={self.inverted}, live={self.live})"
        )


@dataclass
class SniperRow:
    sniper_id: int
    signal_id: int
    submit_bar: int
    submit_time_ns: int
    direction: int
    zone_id: int
    triggered_by: str
    trigger_price: float
    stop_usd: float
    target_usd: float
    lots: float
    status: str = "pending"


@dataclass
class OrderRow:
    order_id: int
    client_order_id: str
    binance_order_id: Optional[int]
    sniper_id: Optional[int]
    symbol: str
    side: str
    type: str
    quantity: float
    stop_price: Optional[float]
    working_type: Optional[str]
    price_protect: bool
    reduce_only: bool
    close_position: bool
    status: str
    submitted_at_ns: Optional[int]
    filled_at_ns: Optional[int]
    avg_fill_price: Optional[float]
    cum_filled_qty: Optional[float]
    raw_response: Optional[str]
    last_error: Optional[str]


@dataclass
class TradeRow:
    trade_id: int
    entry_order_id: Optional[int]
    exit_order_id: Optional[int]
    symbol: str
    direction: int
    entry_bar: int
    entry_time_ns: int
    entry_price: float
    exit_time_ns: Optional[int]
    exit_price: Optional[float]
    quantity: float
    stop_usd: float
    target_usd: float
    exit_reason: Optional[str]
    hold_secs: Optional[float]
    pnl_usd: Optional[float]
    fee_usd: Optional[float]
    entry_triggered_by: str
    rank_tier: Optional[str]
    entry_alignment: Optional[str]
    notes: Optional[str]


# ─────────────────────────────────────────────────────────────────────────────
# StateStore — main API
# ─────────────────────────────────────────────────────────────────────────────

class StateStore:
    """Single-process SQLite WAL state store.

    One instance per process; one writer. Reads from other
    threads (e.g. the HTTP /healthz handler) must use
    :meth:`read_connection` so they share the same WAL.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # write connection (single writer)
        self._conn = sqlite3.connect(
            str(self.db_path),
            isolation_level=None,             # autocommit; we manage txns
            check_same_thread=True,
            timeout=30.0,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.row_factory = sqlite3.Row
        Migrations.apply(self._conn)

    # ── Lifecycle ──────────────────────────────────────────────────────

    def close(self) -> None:
        try:
            # Checkpoint WAL so the .db-wal / .db-shm files are
            # merged back into the main .db (Windows file locking).
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            self._conn.commit()
        finally:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Wrap a block in BEGIN/COMMIT. Yields the connection."""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def read_connection(self) -> sqlite3.Connection:
        """A second connection for read-only threads."""
        r = sqlite3.connect(
            f"file:{self.db_path}?mode=ro", uri=True, timeout=10.0
        )
        r.row_factory = sqlite3.Row
        return r

    # ── FVG zones ──────────────────────────────────────────────────────

    def upsert_zone(self, z: ZoneRow) -> None:
        self._conn.execute(
            """
            INSERT INTO fvg_zones (
                zone_id, trigger_bar, trigger_time_ns, direction,
                zone_low, zone_high,
                mitigated_bar, pierced_bar, inverted_bar, inverted,
                consumed_bar, expired_bar, superseded_bar, played_out_bar,
                live, n_touches, structure_invalidated
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(zone_id) DO UPDATE SET
                mitigated_bar=excluded.mitigated_bar,
                pierced_bar=excluded.pierced_bar,
                inverted_bar=excluded.inverted_bar,
                inverted=excluded.inverted,
                consumed_bar=excluded.consumed_bar,
                expired_bar=excluded.expired_bar,
                superseded_bar=excluded.superseded_bar,
                played_out_bar=excluded.played_out_bar,
                live=excluded.live,
                n_touches=excluded.n_touches,
                structure_invalidated=excluded.structure_invalidated
            """,
            (
                z.zone_id, z.trigger_bar, z.trigger_time_ns, z.direction,
                z.zone_low, z.zone_high,
                z.mitigated_bar, z.pierced_bar, z.inverted_bar,
                int(z.inverted),
                z.consumed_bar, z.expired_bar, z.superseded_bar,
                z.played_out_bar,
                int(z.live), z.n_touches, int(z.structure_invalidated),
            ),
        )

    def kill_zone(self, zone_id: int, live: bool = False) -> None:
        self._conn.execute(
            "UPDATE fvg_zones SET live = ? WHERE zone_id = ?",
            (int(live), zone_id),
        )

    def load_live_zones(self) -> list[ZoneRow]:
        rows = self._conn.execute(
            "SELECT * FROM fvg_zones WHERE live = 1 ORDER BY zone_id"
        ).fetchall()
        return [_row_to_zone(r) for r in rows]

    def load_zone(self, zone_id: int) -> Optional[ZoneRow]:
        r = self._conn.execute(
            "SELECT * FROM fvg_zones WHERE zone_id = ?", (zone_id,)
        ).fetchone()
        return _row_to_zone(r) if r else None

    # ── BoS/CHoCH memory ───────────────────────────────────────────────

    def push_bos_choch(self, bar: int, kind: int) -> None:
        self._conn.execute(
            "INSERT INTO bos_choch_events (bar, kind) VALUES (?, ?)",
            (bar, kind),
        )

    def load_bos_choch_memory(self, max_events: int = 5) -> list[tuple[int, int]]:
        rows = self._conn.execute(
            "SELECT bar, kind FROM bos_choch_events "
            "ORDER BY id DESC LIMIT ?",
            (max_events,),
        ).fetchall()
        return [(int(r["bar"]), int(r["kind"])) for r in rows]

    # ── Pending snipers ────────────────────────────────────────────────

    def insert_sniper(self, s: SniperRow) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO pending_snipers (
                signal_id, submit_bar, submit_time_ns, direction,
                zone_id, triggered_by, trigger_price,
                stop_usd, target_usd, lots, status, created_at_ns
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                s.signal_id, s.submit_bar, s.submit_time_ns, s.direction,
                s.zone_id, s.triggered_by, s.trigger_price,
                s.stop_usd, s.target_usd, s.lots, s.status, time.time_ns(),
            ),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def update_sniper_status(
        self,
        sniper_id: int,
        status: str,
    ) -> None:
        self._conn.execute(
            "UPDATE pending_snipers SET status = ? WHERE sniper_id = ?",
            (status, sniper_id),
        )

    def load_pending_snipers(self) -> list[SniperRow]:
        rows = self._conn.execute(
            "SELECT * FROM pending_snipers WHERE status = 'pending' "
            "ORDER BY sniper_id"
        ).fetchall()
        return [_row_to_sniper(r) for r in rows]

    # ── Orders ─────────────────────────────────────────────────────────

    def insert_order(self, o: OrderRow) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO orders (
                client_order_id, binance_order_id, sniper_id, symbol,
                side, type, quantity, stop_price, working_type,
                price_protect, reduce_only, close_position, status,
                submitted_at_ns, raw_response, last_error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                o.client_order_id, o.binance_order_id, o.sniper_id,
                o.symbol, o.side, o.type, o.quantity, o.stop_price,
                o.working_type, int(o.price_protect), int(o.reduce_only),
                int(o.close_position), o.status, o.submitted_at_ns,
                o.raw_response, o.last_error,
            ),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def update_order(self, o: OrderRow) -> None:
        self._conn.execute(
            """
            UPDATE orders SET
                binance_order_id = ?,
                status = ?,
                submitted_at_ns = ?,
                filled_at_ns = ?,
                avg_fill_price = ?,
                cum_filled_qty = ?,
                raw_response = ?,
                last_error = ?
            WHERE order_id = ?
            """,
            (
                o.binance_order_id, o.status, o.submitted_at_ns,
                o.filled_at_ns, o.avg_fill_price, o.cum_filled_qty,
                o.raw_response, o.last_error, o.order_id,
            ),
        )

    def get_order_by_client_id(self, client_order_id: str) -> Optional[OrderRow]:
        r = self._conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        return _row_to_order(r) if r else None

    def load_open_orders(self) -> list[OrderRow]:
        rows = self._conn.execute(
            "SELECT * FROM orders WHERE status IN ('new','submitted','partially_filled') "
            "ORDER BY order_id"
        ).fetchall()
        return [_row_to_order(r) for r in rows]

    # ── Trades ─────────────────────────────────────────────────────────

    def insert_trade(self, t: TradeRow) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO trades (
                entry_order_id, exit_order_id, symbol, direction,
                entry_bar, entry_time_ns, entry_price,
                exit_time_ns, exit_price,
                quantity, stop_usd, target_usd,
                exit_reason, hold_secs, pnl_usd, fee_usd,
                entry_triggered_by, rank_tier, entry_alignment, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                t.entry_order_id, t.exit_order_id, t.symbol, t.direction,
                t.entry_bar, t.entry_time_ns, t.entry_price,
                t.exit_time_ns, t.exit_price,
                t.quantity, t.stop_usd, t.target_usd,
                t.exit_reason, t.hold_secs, t.pnl_usd, t.fee_usd,
                t.entry_triggered_by, t.rank_tier, t.entry_alignment,
                t.notes,
            ),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def update_trade_exit(
        self,
        trade_id: int,
        *,
        exit_order_id: Optional[int],
        exit_time_ns: int,
        exit_price: float,
        exit_reason: str,
        hold_secs: float,
        pnl_usd: float,
        fee_usd: float,
    ) -> None:
        self._conn.execute(
            """
            UPDATE trades SET
                exit_order_id = ?,
                exit_time_ns = ?,
                exit_price = ?,
                exit_reason = ?,
                hold_secs = ?,
                pnl_usd = ?,
                fee_usd = ?
            WHERE trade_id = ?
            """,
            (
                exit_order_id, exit_time_ns, exit_price, exit_reason,
                hold_secs, pnl_usd, fee_usd, trade_id,
            ),
        )

    def todays_trades(self, day_iso: str) -> list[TradeRow]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE substr("
            "  strftime('%Y-%m-%dT%H:%M:%fZ', exit_time_ns / 1e9, 'unixepoch'),"
            "  1, 10) = ? ORDER BY trade_id",
            (day_iso,),
        ).fetchall()
        return [_row_to_trade(r) for r in rows]

    def daily_pnl_today(self, day_iso: str) -> tuple[int, float, float]:
        row = self._conn.execute(
            "SELECT n_trades, pnl_usd, fee_usd FROM daily_pnl WHERE day = ?",
            (day_iso,),
        ).fetchone()
        if not row:
            return (0, 0.0, 0.0)
        return (int(row["n_trades"]), float(row["pnl_usd"]),
                float(row["fee_usd"]))

    def upsert_daily_pnl(
        self,
        day_iso: str,
        n_trades_inc: int,
        pnl_usd: float,
        fee_usd: float,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO daily_pnl (day, n_trades, pnl_usd, fee_usd, realized)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                n_trades = n_trades + excluded.n_trades,
                pnl_usd  = pnl_usd  + excluded.pnl_usd,
                fee_usd  = fee_usd  + excluded.fee_usd,
                realized = realized + excluded.pnl_usd
            """,
            (day_iso, n_trades_inc, pnl_usd, fee_usd, pnl_usd),
        )

    # ── Heartbeat ──────────────────────────────────────────────────────

    def heartbeat(
        self,
        *,
        last_bar_ns: Optional[int] = None,
        last_feed_ns: Optional[int] = None,
        last_order_ns: Optional[int] = None,
        last_recon_ns: Optional[int] = None,
        process_start_ns: Optional[int] = None,
        last_state: Optional[str] = None,
    ) -> None:
        # Coalesce with the existing row so partial updates preserve other fields.
        cur = self._conn.execute("SELECT * FROM heartbeat WHERE id = 1").fetchone()
        existing = dict(cur) if cur else {}
        merged = {
            "last_bar_ns":       last_bar_ns if last_bar_ns is not None else existing.get("last_bar_ns"),
            "last_feed_ns":      last_feed_ns if last_feed_ns is not None else existing.get("last_feed_ns"),
            "last_order_ns":     last_order_ns if last_order_ns is not None else existing.get("last_order_ns"),
            "last_recon_ns":     last_recon_ns if last_recon_ns is not None else existing.get("last_recon_ns"),
            "process_start_ns":  process_start_ns if process_start_ns is not None else existing.get("process_start_ns"),
            "uptime_secs":       (time.time_ns() - (process_start_ns or existing.get("process_start_ns") or time.time_ns())) / 1e9,
            "last_state":        last_state if last_state is not None else existing.get("last_state"),
        }
        self._conn.execute(
            """
            INSERT INTO heartbeat (id, last_bar_ns, last_feed_ns,
                                   last_order_ns, last_recon_ns,
                                   process_start_ns, uptime_secs, last_state)
            VALUES (1, :last_bar_ns, :last_feed_ns, :last_order_ns,
                    :last_recon_ns, :process_start_ns, :uptime_secs,
                    :last_state)
            ON CONFLICT(id) DO UPDATE SET
                last_bar_ns      = excluded.last_bar_ns,
                last_feed_ns     = excluded.last_feed_ns,
                last_order_ns    = excluded.last_order_ns,
                last_recon_ns    = excluded.last_recon_ns,
                process_start_ns = excluded.process_start_ns,
                uptime_secs      = excluded.uptime_secs,
                last_state       = excluded.last_state
            """,
            merged,
        )

    def read_heartbeat(self) -> dict[str, Any]:
        r = self._conn.execute("SELECT * FROM heartbeat WHERE id = 1").fetchone()
        if not r:
            return {}
        return {k: r[k] for k in r.keys()}

    # ── Failsafes (e.g. daily loss circuit-breaker) ───────────────────

    def activate_failsafe(self, name: str, duration_secs: int, reason: str) -> None:
        self._conn.execute(
            """
            INSERT INTO failsafes (name, active_until_ns, reason)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                active_until_ns = excluded.active_until_ns,
                reason = excluded.reason
            """,
            (name, time.time_ns() + duration_secs * 1_000_000_000, reason),
        )

    def failsafe_active(self, name: str) -> bool:
        r = self._conn.execute(
            "SELECT active_until_ns FROM failsafes WHERE name = ?", (name,)
        ).fetchone()
        if not r:
            return False
        return int(r["active_until_ns"]) > time.time_ns()

    def failsafe_reason(self, name: str) -> Optional[str]:
        r = self._conn.execute(
            "SELECT reason FROM failsafes WHERE name = ?", (name,)
        ).fetchone()
        return str(r["reason"]) if r else None

    def clear_failsafe(self, name: str) -> None:
        self._conn.execute("DELETE FROM failsafes WHERE name = ?", (name,))

    # ── Metrics counters ──────────────────────────────────────────────

    def incr_counter(self, name: str, by: float = 1.0) -> None:
        self._conn.execute(
            """
            INSERT INTO metrics_counters (name, value, updated_ns)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                value = value + excluded.value,
                updated_ns = excluded.updated_ns
            """,
            (name, by, time.time_ns()),
        )

    def get_counter(self, name: str) -> float:
        r = self._conn.execute(
            "SELECT value FROM metrics_counters WHERE name = ?", (name,)
        ).fetchone()
        return float(r["value"]) if r else 0.0

    def get_all_counters(self) -> dict[str, float]:
        rows = self._conn.execute(
            "SELECT name, value FROM metrics_counters"
        ).fetchall()
        return {str(r["name"]): float(r["value"]) for r in rows}


# ─────────────────────────────────────────────────────────────────────────────
# Row converters
# ─────────────────────────────────────────────────────────────────────────────

def _row_to_zone(r: sqlite3.Row) -> ZoneRow:
    return ZoneRow(
        zone_id=int(r["zone_id"]),
        trigger_bar=int(r["trigger_bar"]),
        trigger_time_ns=int(r["trigger_time_ns"]),
        direction=int(r["direction"]),
        zone_low=float(r["zone_low"]),
        zone_high=float(r["zone_high"]),
        mitigated_bar=int(r["mitigated_bar"]),
        pierced_bar=int(r["pierced_bar"]),
        inverted_bar=int(r["inverted_bar"]),
        inverted=bool(r["inverted"]),
        consumed_bar=int(r["consumed_bar"]),
        expired_bar=int(r["expired_bar"]),
        superseded_bar=int(r["superseded_bar"]),
        played_out_bar=int(r["played_out_bar"]),
        live=bool(r["live"]),
        n_touches=int(r["n_touches"]),
        structure_invalidated=bool(r["structure_invalidated"]),
    )


def _row_to_sniper(r: sqlite3.Row) -> SniperRow:
    return SniperRow(
        sniper_id=int(r["sniper_id"]),
        signal_id=int(r["signal_id"]),
        submit_bar=int(r["submit_bar"]),
        submit_time_ns=int(r["submit_time_ns"]),
        direction=int(r["direction"]),
        zone_id=int(r["zone_id"]),
        triggered_by=str(r["triggered_by"]),
        trigger_price=float(r["trigger_price"]),
        stop_usd=float(r["stop_usd"]),
        target_usd=float(r["target_usd"]),
        lots=float(r["lots"]),
        status=str(r["status"]),
    )


def _row_to_order(r: sqlite3.Row) -> OrderRow:
    return OrderRow(
        order_id=int(r["order_id"]),
        client_order_id=str(r["client_order_id"]),
        binance_order_id=int(r["binance_order_id"]) if r["binance_order_id"] is not None else None,
        sniper_id=int(r["sniper_id"]) if r["sniper_id"] is not None else None,
        symbol=str(r["symbol"]),
        side=str(r["side"]),
        type=str(r["type"]),
        quantity=float(r["quantity"]),
        stop_price=float(r["stop_price"]) if r["stop_price"] is not None else None,
        working_type=str(r["working_type"]) if r["working_type"] is not None else None,
        price_protect=bool(r["price_protect"]),
        reduce_only=bool(r["reduce_only"]),
        close_position=bool(r["close_position"]),
        status=str(r["status"]),
        submitted_at_ns=int(r["submitted_at_ns"]) if r["submitted_at_ns"] is not None else None,
        filled_at_ns=int(r["filled_at_ns"]) if r["filled_at_ns"] is not None else None,
        avg_fill_price=float(r["avg_fill_price"]) if r["avg_fill_price"] is not None else None,
        cum_filled_qty=float(r["cum_filled_qty"]) if r["cum_filled_qty"] is not None else None,
        raw_response=str(r["raw_response"]) if r["raw_response"] is not None else None,
        last_error=str(r["last_error"]) if r["last_error"] is not None else None,
    )


def _row_to_trade(r: sqlite3.Row) -> TradeRow:
    return TradeRow(
        trade_id=int(r["trade_id"]),
        entry_order_id=int(r["entry_order_id"]) if r["entry_order_id"] is not None else None,
        exit_order_id=int(r["exit_order_id"]) if r["exit_order_id"] is not None else None,
        symbol=str(r["symbol"]),
        direction=int(r["direction"]),
        entry_bar=int(r["entry_bar"]),
        entry_time_ns=int(r["entry_time_ns"]),
        entry_price=float(r["entry_price"]),
        exit_time_ns=int(r["exit_time_ns"]) if r["exit_time_ns"] is not None else None,
        exit_price=float(r["exit_price"]) if r["exit_price"] is not None else None,
        quantity=float(r["quantity"]),
        stop_usd=float(r["stop_usd"]),
        target_usd=float(r["target_usd"]),
        exit_reason=str(r["exit_reason"]) if r["exit_reason"] is not None else None,
        hold_secs=float(r["hold_secs"]) if r["hold_secs"] is not None else None,
        pnl_usd=float(r["pnl_usd"]) if r["pnl_usd"] is not None else None,
        fee_usd=float(r["fee_usd"]) if r["fee_usd"] is not None else None,
        entry_triggered_by=str(r["entry_triggered_by"]),
        rank_tier=str(r["rank_tier"]) if r["rank_tier"] is not None else None,
        entry_alignment=str(r["entry_alignment"]) if r["entry_alignment"] is not None else None,
        notes=str(r["notes"]) if r["notes"] is not None else None,
    )


__all__ = [
    "StateStore",
    "SCHEMA_VERSION",
    "ZoneRow",
    "SniperRow",
    "OrderRow",
    "TradeRow",
]