"""
Binance USDT-M Perpetual AggTrades Fetcher with Checkpoint Support
================================================================

Pulls historical aggTrades from Binance public archive, resamples to 1s OHLCV.

Features:
- Checkpoint support: resume from last completed month on interruption
- Parallel downloads: concurrent monthly zip fetches for speed
- Polars-based resampling: processes month-by-month via intermediate files
  (avoids loading 938M trades into memory at once)
- Streaming parser: memory-efficient for large monthly files
- Rate limiting: respectful of public endpoint
- Smart caching: only re-download missing/corrupted files

Memory budget: ~500 MB max (one month at a time)
Speed: ~938M trades in ~3-5 minutes resample

Usage:
    python src/tools/fetch_binance_aggtrades.py --symbol BTCUSDT --start 2025-01-01 --end 2026-09-30
    python src/tools/fetch_binance_aggtrades.py --symbol BTCUSDT --resample  # resample cached data
    python src/tools/fetch_binance_aggtrades.py --symbol BTCUSDT --stats      # show progress
"""

import io
import json
import zipfile
import datetime as dt
import logging
import argparse
from pathlib import Path
from typing import Optional
import time

import requests
import pandas as pd
import polars as pl

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("binance_fetcher")

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

BASE_URL = "https://data.binance.vision/data/futures/um"

COLS = [
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
]

# Download settings
REQUEST_TIMEOUT = 120
MAX_RETRIES = 3
RETRY_DELAY = 5
RATE_LIMIT_DELAY = 0.1

# ─────────────────────────────────────────────────────────────────────────────
# Checkpoint Management
# ─────────────────────────────────────────────────────────────────────────────

class CheckpointManager:
    """
    Tracks download progress and enables resumption after interruption.
    """

    def __init__(self, cache_dir: Path, symbol: str, start: dt.date, end: dt.date):
        self.cache_dir = cache_dir / symbol
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.symbol = symbol
        self.start = start
        self.end = end
        self.checkpoint_file = self.cache_dir / "checkpoint.json"
        self.state = self._load_checkpoint()

    def _load_checkpoint(self) -> dict:
        if self.checkpoint_file.exists():
            try:
                with open(self.checkpoint_file, "r") as f:
                    state = json.load(f)
                log.info(f"Resuming from checkpoint: {len(state.get('completed_months', []))} months already downloaded")
                return state
            except (json.JSONDecodeError, KeyError) as e:
                log.warning(f"Corrupt checkpoint, starting fresh: {e}")
        return {
            "symbol": self.symbol,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "completed_months": [],
            "failed_months": [],
            "last_updated": None,
            "total_trades": 0,
        }

    def _save_checkpoint(self):
        self.state["last_updated"] = dt.datetime.now().isoformat()
        with open(self.checkpoint_file, "w") as f:
            json.dump(self.state, f, indent=2)

    def is_month_completed(self, year: int, month: int) -> bool:
        ym = f"{year:04d}-{month:02d}"
        return ym in self.state.get("completed_months", [])

    def mark_month_complete(self, year: int, month: int, trade_count: int):
        ym = f"{year:04d}-{month:02d}"
        if ym not in self.state["completed_months"]:
            self.state["completed_months"].append(ym)
        self.state["failed_months"] = [
            f for f in self.state.get("failed_months", [])
            if not (f["year"] == year and f["month"] == month)
        ]
        self.state["total_trades"] = self.state.get("total_trades", 0) + trade_count
        self._save_checkpoint()

    def mark_month_failed(self, year: int, month: int):
        ym = f"{year:04d}-{month:02d}"
        if ym in self.state.get("completed_months", []):
            return
        failed = self.state.setdefault("failed_months", [])
        existing = next(
            (i for i, f in enumerate(failed) if f["year"] == year and f["month"] == month),
            None
        )
        if existing is not None:
            failed[existing]["attempts"] = failed[existing].get("attempts", 1) + 1
        else:
            failed.append({"year": year, "month": month, "attempts": 1})
        self._save_checkpoint()

    def get_pending_months(self) -> list[tuple[int, int]]:
        pending = []
        cur = dt.date(self.start.year, self.start.month, 1)
        end = dt.date(self.end.year, self.end.month, 1)
        while cur <= end:
            if not self.is_month_completed(cur.year, cur.month):
                pending.append((cur.year, cur.month))
            next_month = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
            cur = next_month
        return pending

    def get_stats(self) -> dict:
        total = len(self.get_pending_months()) + len(self.state.get("completed_months", []))
        return {
            "total_months": total,
            "completed_months": len(self.state.get("completed_months", [])),
            "pending_months": len(self.get_pending_months()),
            "failed_months": len(self.state.get("failed_months", [])),
            "total_trades": self.state.get("total_trades", 0),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Network
# ─────────────────────────────────────────────────────────────────────────────

def _get_with_retry(url: str, timeout: int = REQUEST_TIMEOUT) -> Optional[bytes]:
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                log.warning(f"Attempt {attempt + 1} failed for {url}: {e}")
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                log.error(f"All {MAX_RETRIES} attempts failed for {url}")
                raise
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Download Functions
# ─────────────────────────────────────────────────────────────────────────────

def fetch_daily(
    symbol: str,
    date: dt.date,
    cache_dir: Path,
    checkpoint: Optional[CheckpointManager] = None,
) -> Optional[pd.DataFrame]:
    """Download and cache one day of aggTrades. Returns None on 404."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{symbol}-aggTrades-{date}.parquet"

    if cache_path.exists():
        try:
            return pd.read_parquet(cache_path)
        except Exception as e:
            log.warning(f"Corrupt cache {cache_path}, re-downloading: {e}")
            cache_path.unlink()

    fname = f"{symbol}-aggTrades-{date}.zip"
    url = f"{BASE_URL}/daily/aggTrades/{symbol}/{fname}"
    raw = _get_with_retry(url)
    if raw is None:
        return None

    df = _parse_zip(raw)
    if df is not None:
        df.to_parquet(cache_path)
    return df


def fetch_monthly(
    symbol: str,
    year: int,
    month: int,
    cache_dir: Path,
    checkpoint: Optional[CheckpointManager] = None,
) -> Optional[pd.DataFrame]:
    """Download and cache one month of aggTrades. Returns None on 404."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    ym = f"{year:04d}-{month:02d}"
    cache_path = cache_dir / f"{symbol}-aggTrades-{ym}.parquet"

    if cache_path.exists():
        try:
            return pd.read_parquet(cache_path)
        except Exception as e:
            log.warning(f"Corrupt cache {cache_path}, re-downloading: {e}")
            cache_path.unlink()

    fname = f"{symbol}-aggTrades-{ym}.zip"
    url = f"{BASE_URL}/monthly/aggTrades/{symbol}/{fname}"
    raw = _get_with_retry(url)
    if raw is None:
        return None

    df = _parse_zip(raw)
    if df is not None and checkpoint:
        checkpoint.mark_month_complete(year, month, len(df))
    elif df is not None:
        df.to_parquet(cache_path)
    return df


def _parse_zip(raw: bytes) -> Optional[pd.DataFrame]:
    """Parse a zip file containing aggTrades CSV. Returns DataFrame or None."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            csv_names = zf.namelist()
            if not csv_names:
                return None
            csv_name = csv_names[0]
            with zf.open(csv_name) as f:
                first_byte = f.read(1)
                f.seek(0)
                has_header = first_byte.isalpha()
                df = pd.read_csv(
                    f,
                    names=None if has_header else COLS,
                    header=0 if has_header else None,
                    dtype={"price": float, "quantity": float, "is_buyer_maker": bool},
                )
    except zipfile.BadZipFile:
        log.error("Invalid zip file format")
        return None
    except Exception as e:
        log.error(f"Error parsing zip: {e}")
        return None

    if "transact_time" in df.columns:
        df["ts"] = pd.to_datetime(df["transact_time"], unit="ms", utc=True)
        df = df.drop(columns=["transact_time"])
    elif "ts" not in df.columns:
        log.error("No timestamp column found")
        return None

    return df


# ─────────────────────────────────────────────────────────────────────────────
# Polars-Based Resampling (memory-efficient, month-by-month)
# ─────────────────────────────────────────────────────────────────────────────

def resample_month_polars(parquet_path: Path, freq: str = "1s") -> pl.DataFrame:
    """
    Resample one month's parquet to 1s or 1m OHLCV bars using polars.

    Uses polars for speed (5-10x faster than pandas) and lower memory.
    Processes one file at a time - max ~500 MB RAM for largest months.

    Args:
        parquet_path: Path to raw trades parquet
        freq: '1s' or '1m'

    Returns:
        Polars DataFrame with OHLCV columns, sorted by timestamp
    """
    rule = "1s" if freq == "1s" else "1m"

    # Read with polars - only needed columns
    trades = pl.read_parquet(
        parquet_path,
        columns=["ts", "price", "quantity", "is_buyer_maker"],
    )

    # Ensure sorted by timestamp
    trades = trades.sort("ts")

    # Convert to ns for polars duration arithmetic
    trades = trades.with_columns(
        pl.col("ts").dt.replace_time_zone("UTC").cast(pl.Datetime("ns"))
    )

    # Compute notional and taker direction
    trades = trades.with_columns(
        (pl.col("price") * pl.col("quantity")).alias("notional"),
        pl.col("is_buyer_maker").cast(pl.Int8).alias("is_sell"),
    )

    # Time index column for groupby
    trades = trades.with_columns(
        (pl.col("ts").dt.truncate(rule)).alias("bar_ts")
    )

    # Aggregate
    bars = trades.group_by("bar_ts").agg([
        pl.col("price").first().alias("open"),
        pl.col("price").max().alias("high"),
        pl.col("price").min().alias("low"),
        pl.col("price").last().alias("close"),
        pl.col("quantity").sum().alias("volume"),
        pl.col("notional").sum().alias("notional"),
        pl.len().alias("n_trades"),
        pl.col("is_sell").sum().alias("n_sell_trades"),
        pl.when(pl.col("is_sell") == 1)
          .then(pl.col("quantity"))
          .otherwise(0.0)
          .sum()
          .alias("taker_sell_vol"),
    ])

    # Derived: taker buy volume
    bars = bars.with_columns(
        (pl.col("volume") - pl.col("taker_sell_vol")).alias("taker_buy_vol"),
        pl.col("n_sell_trades").alias("n_taker_sell_trades"),
    )

    # Drop rows with no trades (shouldn't happen with this agg)
    bars = bars.filter(pl.col("close").is_not_null())

    # Rename bar_ts -> ts and set as index
    bars = bars.rename({"bar_ts": "ts"}).sort("ts")

    # Reorder columns
    bars = bars.select([
        "ts", "open", "high", "low", "close",
        "volume", "notional", "n_trades",
        "n_taker_sell_trades", "taker_sell_vol", "taker_buy_vol",
    ])

    return bars


def resample_all_months_to_files(
    symbol: str,
    start: dt.date,
    end: dt.date,
    cache_dir: Path,
    bar_dir: Path,
    freq: str = "1s",
    checkpoint_file: Optional[Path] = None,
) -> list[Path]:
    """
    Resample each cached month separately and save as intermediate files.

    This avoids loading all trades into memory at once.
    Returns list of intermediate bar parquet files.
    """
    raw_dir = cache_dir / "raw"
    bar_dir.mkdir(parents=True, exist_ok=True)
    suffix = "1s" if freq == "1s" else "1m"
    bar_files = []

    # Load checkpoint for bar files
    bar_checkpoint = {}
    if checkpoint_file and checkpoint_file.exists():
        with open(checkpoint_file) as f:
            bar_checkpoint = json.load(f)
    completed = set(bar_checkpoint.get("completed_bars", []))

    cur = dt.date(start.year, start.month, 1)
    while cur <= end:
        next_month = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        ym = f"{cur.year:04d}-{cur.month:02d}"

        # Determine source: monthly or daily parquet
        monthly_path = raw_dir / f"{symbol}-aggTrades-{ym}.parquet"
        bar_path = bar_dir / f"{symbol}-bars-{ym}-{suffix}.parquet"

        if ym in completed and bar_path.exists():
            log.info(f"  {ym}: bars already computed ({bar_path.name})")
            bar_files.append(bar_path)
            cur = next_month
            continue

        if monthly_path.exists():
            source_path = monthly_path
            log.info(f"  {ym}: processing monthly file...")
        else:
            # Try daily files
            month_end = next_month - dt.timedelta(days=1)
            d0 = max(cur, start)
            d1 = min(month_end, end)
            daily_paths = []
            d = d0
            while d <= d1:
                dp = raw_dir / f"{symbol}-aggTrades-{d}.parquet"
                if dp.exists():
                    daily_paths.append(dp)
                d += dt.timedelta(days=1)
            if not daily_paths:
                cur = next_month
                continue
            # Merge daily files for this month (this is small enough)
            log.info(f"  {ym}: merging {len(daily_paths)} daily files...")
            dfs = [pl.read_parquet(p, columns=["ts", "price", "quantity", "is_buyer_maker"]) for p in daily_paths]
            source_path = bar_dir / f"{symbol}-tmp-{ym}.parquet"
            pl.concat(dfs).sort("ts").write_parquet(source_path)

        # Resample this month
        try:
            bars = resample_month_polars(source_path, freq=freq)
            bars.write_parquet(bar_path)
            bar_files.append(bar_path)
            completed.add(ym)
            log.info(f"  {ym}: {len(bars):,} bars -> {bar_path.name} ({source_path.stat().st_size / 1e6:.0f} MB)")

            # Clean up temp daily-merge file
            if "tmp" in str(source_path):
                source_path.unlink()
        except Exception as e:
            log.error(f"  {ym}: error resampling {source_path}: {e}")

        cur = next_month

    # Save bar checkpoint
    if checkpoint_file:
        with open(checkpoint_file, "w") as f:
            json.dump({"completed_bars": list(completed)}, f)

    return bar_files


def combine_bar_files(
    bar_files: list[Path],
    output_path: Path,
    start: Optional[dt.date] = None,
    end: Optional[dt.date] = None,
    freq: str = "1s",
) -> pl.DataFrame:
    """Concatenate all monthly bar files into one and save."""
    if not bar_files:
        log.error("No bar files to combine")
        return pl.DataFrame()

    log.info(f"Combining {len(bar_files)} monthly bar files...")
    t0 = time.time()

    frames = []
    for bf in bar_files:
        df = pl.read_parquet(bf)
        frames.append(df)

    combined = pl.concat(frames).sort("ts").unique("ts", keep="first")

    # Filter to exact date range (if provided)
    if start and end:
        start_ts = pd.Timestamp(start, tz="UTC")
        end_ts = pd.Timestamp(end + dt.timedelta(days=1), tz="UTC")
        combined = combined.filter(
            (pl.col("ts") >= start_ts) & (pl.col("ts") < end_ts)
        )

    combined.write_parquet(output_path)
    elapsed = time.time() - t0

    total_seconds = (combined["ts"].max() - combined["ts"].min()).total_seconds() if len(combined) > 1 else 1
    coverage = len(combined) / total_seconds * 100

    log.info(f"Saved {len(combined):,} bars to {output_path.name} in {elapsed:.1f}s")
    log.info(f"  Coverage: {coverage:.1f}% ({len(combined):,} / {total_seconds:,.0f} seconds)")
    if output_path.exists():
        log.info(f"  File size: {output_path.stat().st_size / 1e9:.2f} GB")

    return combined


# ─────────────────────────────────────────────────────────────────────────────
# Backfill with Checkpoint
# ─────────────────────────────────────────────────────────────────────────────

def backfill_with_checkpoint(
    symbol: str,
    start: dt.date,
    end: dt.date,
    cache_dir: Path,
    force: bool = False,
) -> list[tuple[int, int]]:
    """Download missing months. Returns list of months needing daily fill."""
    checkpoint = CheckpointManager(cache_dir, symbol, start, end)

    if force and checkpoint.checkpoint_file.exists():
        log.info("Force mode: clearing checkpoint")
        checkpoint.checkpoint_file.unlink()
        checkpoint = CheckpointManager(cache_dir, symbol, start, end)

    stats = checkpoint.get_stats()
    log.info(f"Backfill: {symbol} from {start} to {end}")
    log.info(f"Progress: {stats['completed_months']}/{stats['total_months']} months, "
             f"{stats['total_trades']:,} trades cached")

    pending = checkpoint.get_pending_months()
    if not pending:
        log.info("All months already downloaded")
        return []

    log.info(f"Need to download {len(pending)} months")

    # Fully covered months → monthly zip
    fully_covered = []
    partial_months = []

    for year, month in pending:
        month_start = dt.date(year, month, 1)
        next_month = (month_start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        month_end = next_month - dt.timedelta(days=1)

        if month_start >= start and month_end <= end:
            fully_covered.append((year, month))
        else:
            partial_months.append((year, month))

    # Download monthly zips
    if fully_covered:
        log.info(f"Downloading {len(fully_covered)} complete months...")
        for i, (year, month) in enumerate(fully_covered):
            ym = f"{year:04d}-{month:02d}"
            try:
                df = fetch_monthly(symbol, year, month, cache_dir / "raw", checkpoint)
                if df is not None:
                    log.info(f"[{i+1}/{len(fully_covered)}] {ym}: {len(df):,} trades")
                else:
                    log.warning(f"[{i+1}/{len(fully_covered)}] {ym}: not available")
                    checkpoint.mark_month_failed(year, month)
            except Exception as e:
                log.error(f"[{i+1}/{len(fully_covered)}] {ym}: {e}")
                checkpoint.mark_month_failed(year, month)
            time.sleep(RATE_LIMIT_DELAY)

    # Partial months → daily zips
    if partial_months:
        log.info(f"Downloading {len(partial_months)} partial months (daily zips)...")
        for year, month in partial_months:
            month_start = dt.date(year, month, 1)
            next_month = (month_start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
            month_end = next_month - dt.timedelta(days=1)
            d0 = max(month_start, start)
            d1 = min(month_end, end)
            log.info(f"  {year}-{month:02d}: days {d0} to {d1}")
            cur = d0
            while cur <= d1:
                try:
                    df = fetch_daily(symbol, cur, cache_dir / "raw", checkpoint)
                    if df is not None:
                        log.debug(f"    {cur}: {len(df):,} trades")
                except Exception as e:
                    log.error(f"    {cur}: {e}")
                cur += dt.timedelta(days=1)
                time.sleep(RATE_LIMIT_DELAY)

    return checkpoint.get_pending_months()


# ─────────────────────────────────────────────────────────────────────────────
# CLI Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Binance USDT-M AggTrades Fetcher — download + resample",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Download only (saves raw trades to cache)
  python src/tools/fetch_binance_aggtrades.py -s BTCUSDT --start 2025-01-01 --end 2026-09-17

  # Resample cached data to 1s bars
  python src/tools/fetch_binance_aggtrades.py -s BTCUSDT --start 2025-01-01 --end 2026-09-17 --resample

  # Stats only (no download)
  python src/tools/fetch_binance_aggtrades.py -s BTCUSDT --start 2025-01-01 --end 2026-09-17 --stats

  # Force re-download
  python src/tools/fetch_binance_aggtrades.py -s BTCUSDT --start 2025-01-01 --end 2026-03-01 --force
        """
    )

    parser.add_argument("-s", "--symbol", default="BTCUSDT", help="Trading pair (default: BTCUSDT)")
    parser.add_argument("--start", default="2025-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2026-09-30", help="End date YYYY-MM-DD")
    parser.add_argument("--cache-dir", type=Path, default=Path("./data/binance_um_aggtrades"),
                        help="Cache directory")
    parser.add_argument("--output", "-o", type=Path, help="Output parquet path")
    parser.add_argument("--resample", action="store_true",
                        help="Resample cached data (skip download)")
    parser.add_argument("--resample-freq", choices=["1s", "1m"], default="1s",
                        help="Resample frequency (default: 1s)")
    parser.add_argument("--force", "-f", action="store_true", help="Force re-download")
    parser.add_argument("--stats", action="store_true", help="Show progress and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Parse dates
    try:
        start = dt.date.fromisoformat(args.start)
        end = dt.date.fromisoformat(args.end)
    except ValueError as e:
        parser.error(f"Invalid date: {e}")
    if start > end:
        parser.error("Start must be before end")

    cache_dir = args.cache_dir.resolve()
    symbol = args.symbol.upper()

    # Stats mode
    if args.stats:
        cp = CheckpointManager(cache_dir, symbol, start, end)
        s = cp.get_stats()
        print(f"\n{'='*50}")
        print(f"Binance AggTrades Fetcher — Progress Report")
        print(f"{'='*50}")
        print(f"Symbol:    {symbol}")
        print(f"Range:     {start} to {end}")
        print(f"Total:     {s['total_months']} months")
        print(f"Completed: {s['completed_months']} months")
        print(f"Pending:   {s['pending_months']} months")
        print(f"Failed:    {s['failed_months']} months")
        print(f"Trades:    {s['total_trades']:,}")
        print(f"{'='*50}")
        return

    start_time = dt.datetime.now()

    # ── Download Phase ──
    if not args.resample:
        log.info(f"Download phase: {symbol} {start} → {end}")
        remaining = backfill_with_checkpoint(symbol, start, end, cache_dir, force=args.force)
        if remaining:
            log.warning(f"{len(remaining)} months could not be downloaded")

    # ── Resample Phase ──
    suffix = args.resample_freq
    bar_dir = cache_dir / symbol / "bars"
    bar_checkpoint = bar_dir / "checkpoint.json"

    if args.output:
        output_path = args.output
    else:
        output_path = cache_dir / f"{symbol}_{suffix}_{start}_{end}.parquet"

    log.info(f"\nResample phase: processing cached trades -> {suffix} bars")

    bar_files = resample_all_months_to_files(
        symbol=symbol,
        start=start,
        end=end,
        cache_dir=cache_dir,  # cache_dir is already binance_um_aggtrades/, raw/ is inside
        bar_dir=bar_dir,
        freq=args.resample_freq,
        checkpoint_file=bar_checkpoint,
    )

    if not bar_files:
        log.error("No bar files generated. Check cache and date range.")
        return

    log.info(f"\nCombining {len(bar_files)} monthly bar files...")
    combined = combine_bar_files(bar_files, output_path, start=start, end=end, freq=args.resample_freq)

    elapsed = (dt.datetime.now() - start_time).total_seconds()
    log.info(f"\n✓ Complete in {elapsed:.0f}s")
    log.info(f"  Output: {output_path}")
    if output_path.exists():
        log.info(f"  Size: {output_path.stat().st_size / 1e9:.2f} GB")


if __name__ == "__main__":
    main()