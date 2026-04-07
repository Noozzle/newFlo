#!/usr/bin/env python
"""Offline converter: live_data CSV → Parquet with int64 ms timestamps.

Usage:
    python scripts/convert_to_parquet.py                     # All symbols
    python scripts/convert_to_parquet.py --symbol SOLUSDT    # One symbol
    python scripts/convert_to_parquet.py --batch-size 200000 # Custom batch
"""

from __future__ import annotations

import argparse
import csv
from calendar import timegm
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from loguru import logger

# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _parse_ts_ms(ts_str: str) -> int:
    """Parse timestamp string to int64 milliseconds (UTC)."""
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    return timegm(dt.timetuple()) * 1000 + dt.microsecond // 1000


# ---------------------------------------------------------------------------
# Parquet schemas
# ---------------------------------------------------------------------------

ORDERBOOK_SCHEMA = pa.schema([
    ("timestamp_ms", pa.int64()),
    ("bid_price", pa.float64()),
    ("bid_size", pa.float64()),
    ("ask_price", pa.float64()),
    ("ask_size", pa.float64()),
    ("update_id", pa.int64()),
    ("seq", pa.int64()),
])

TRADES_SCHEMA = pa.schema([
    ("timestamp_ms", pa.int64()),
    ("price", pa.float64()),
    ("amount", pa.float64()),
    ("side", pa.int8()),  # 1=buy, 2=sell
])

KLINE_SCHEMA = pa.schema([
    ("timestamp_ms", pa.int64()),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("volume", pa.float64()),
])

# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------

def _flush(writer, batch, schema) -> int:
    """Write batch to parquet and return row count."""
    n = len(batch[schema.names[0]])
    if n > 0:
        writer.write_table(pa.table(batch, schema=schema))
    return n


def convert_orderbook(
    csv_path: Path, parquet_path: Path, batch_size: int, *, dedup: bool = False,
) -> tuple[int, int]:
    """Convert orderbook CSV to Parquet.  Handles 6→10 column schema change.

    When *dedup* is True, consecutive rows with identical
    (bid_price, ask_price, bid_size, ask_size) are collapsed into one row
    keeping the **last** timestamp (lossless — state is unchanged).

    Returns (original_rows, kept_rows).
    """
    writer = pq.ParquetWriter(str(parquet_path), ORDERBOOK_SCHEMA, compression="snappy")
    original = 0
    kept = 0

    # Dedup state: pending row waiting to be flushed
    prev_key: tuple | None = None
    pending: dict | None = None  # single-row dict ready to append

    def _append(rec: dict, batch: dict[str, list]) -> None:
        for k in ORDERBOOK_SCHEMA.names:
            batch[k].append(rec[k])

    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        batch: dict[str, list] = {k: [] for k in ORDERBOOK_SCHEMA.names}

        for row in reader:
            ncols = len(row)
            try:
                if ncols == 6:
                    ts_ms = _parse_ts_ms(row[0])
                    rec = {
                        "timestamp_ms": ts_ms,
                        "bid_price": float(row[1]), "bid_size": float(row[2]),
                        "ask_price": float(row[3]), "ask_size": float(row[4]),
                        "update_id": -1, "seq": -1,
                    }
                elif ncols >= 10:
                    ts_ms = _parse_ts_ms(row[0])
                    rec = {
                        "timestamp_ms": ts_ms,
                        "bid_price": float(row[5]), "bid_size": float(row[6]),
                        "ask_price": float(row[7]), "ask_size": float(row[8]),
                        "update_id": int(row[3]), "seq": int(row[4]),
                    }
                else:
                    continue
            except (ValueError, IndexError):
                continue

            original += 1

            if dedup:
                key = (rec["bid_price"], rec["ask_price"], rec["bid_size"], rec["ask_size"])
                if key == prev_key:
                    # Same state — update pending with latest timestamp
                    pending["timestamp_ms"] = rec["timestamp_ms"]
                    if rec["update_id"] != -1:
                        pending["update_id"] = rec["update_id"]
                        pending["seq"] = rec["seq"]
                    continue
                # State changed — flush previous pending row
                if pending is not None:
                    _append(pending, batch)
                    kept += 1
                prev_key = key
                pending = rec
            else:
                _append(rec, batch)
                kept += 1

            if len(batch["timestamp_ms"]) >= batch_size:
                writer.write_table(pa.table(batch, schema=ORDERBOOK_SCHEMA))
                batch = {k: [] for k in ORDERBOOK_SCHEMA.names}

        # Flush last pending row (dedup mode)
        if dedup and pending is not None:
            _append(pending, batch)
            kept += 1

        if batch["timestamp_ms"]:
            writer.write_table(pa.table(batch, schema=ORDERBOOK_SCHEMA))

    writer.close()
    return original, kept


def convert_trades(csv_path: Path, parquet_path: Path, batch_size: int) -> int:
    """Convert trades CSV to Parquet."""
    writer = pq.ParquetWriter(str(parquet_path), TRADES_SCHEMA, compression="snappy")
    total = 0

    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        batch: dict[str, list] = {k: [] for k in TRADES_SCHEMA.names}

        for row in reader:
            try:
                ts_ms = _parse_ts_ms(row["timestamp"])
                side_val = row.get("side", "buy").lower()
                side_int = 1 if side_val in ("buy", "b", "bid", "1") else 2
                batch["timestamp_ms"].append(ts_ms)
                batch["price"].append(float(row["price"]))
                batch["amount"].append(float(row["amount"]))
                batch["side"].append(side_int)
            except (ValueError, KeyError):
                continue

            if len(batch["timestamp_ms"]) >= batch_size:
                total += _flush(writer, batch, TRADES_SCHEMA)
                batch = {k: [] for k in TRADES_SCHEMA.names}

        total += _flush(writer, batch, TRADES_SCHEMA)

    writer.close()
    return total


def convert_kline(csv_path: Path, parquet_path: Path, batch_size: int) -> int:
    """Convert kline CSV to Parquet."""
    writer = pq.ParquetWriter(str(parquet_path), KLINE_SCHEMA, compression="snappy")
    total = 0

    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        batch: dict[str, list] = {k: [] for k in KLINE_SCHEMA.names}

        for row in reader:
            try:
                ts_ms = _parse_ts_ms(row["timestamp"])
                batch["timestamp_ms"].append(ts_ms)
                batch["open"].append(float(row["open"]))
                batch["high"].append(float(row["high"]))
                batch["low"].append(float(row["low"]))
                batch["close"].append(float(row["close"]))
                batch["volume"].append(float(row["volume"]))
            except (ValueError, KeyError):
                continue

            if len(batch["timestamp_ms"]) >= batch_size:
                total += _flush(writer, batch, KLINE_SCHEMA)
                batch = {k: [] for k in KLINE_SCHEMA.names}

        total += _flush(writer, batch, KLINE_SCHEMA)

    writer.close()
    return total


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def convert_symbol(symbol_dir: Path, batch_size: int, *, dedup: bool = False) -> None:
    """Convert all CSV files for one symbol."""
    symbol = symbol_dir.name

    for csv_name in ("orderbook.csv", "trades.csv", "1m.csv", "15m.csv"):
        csv_path = symbol_dir / csv_name
        if not csv_path.exists():
            continue

        parquet_path = csv_path.with_suffix(".parquet")
        logger.info(f"Converting {symbol}/{csv_name} → {parquet_path.name}")

        if csv_name == "orderbook.csv":
            original, kept = convert_orderbook(csv_path, parquet_path, batch_size, dedup=dedup)
        elif csv_name == "trades.csv":
            kept = convert_trades(csv_path, parquet_path, batch_size)
            original = kept
        else:
            kept = convert_kline(csv_path, parquet_path, batch_size)
            original = kept

        csv_mb = csv_path.stat().st_size / (1024 * 1024)
        pq_mb = parquet_path.stat().st_size / (1024 * 1024)
        ratio = pq_mb / csv_mb * 100 if csv_mb > 0 else 0

        if dedup and csv_name == "orderbook.csv" and original > 0:
            reduction = (1 - kept / original) * 100
            logger.info(
                f"  {kept:,} / {original:,} rows ({reduction:.1f}% dedup reduction) | "
                f"{csv_mb:.1f} MB → {pq_mb:.1f} MB ({ratio:.0f}%)"
            )
        else:
            logger.info(f"  {kept:,} rows | {csv_mb:.1f} MB → {pq_mb:.1f} MB ({ratio:.0f}%)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert live_data CSV → Parquet")
    parser.add_argument("--data-dir", default="live_data", help="Base data directory")
    parser.add_argument("--symbol", default=None, help="Convert only this symbol")
    parser.add_argument("--batch-size", type=int, default=100_000, help="Rows per row group")
    parser.add_argument("--dedup", action="store_true", default=False,
                        help="Drop consecutive orderbook rows with identical bid/ask (lossless)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        logger.error(f"Data directory not found: {data_dir}")
        return

    if args.dedup:
        logger.info("Orderbook dedup ENABLED (keeping last timestamp of identical consecutive rows)")

    if args.symbol:
        sd = data_dir / args.symbol
        if sd.exists():
            convert_symbol(sd, args.batch_size, dedup=args.dedup)
        else:
            logger.error(f"Symbol directory not found: {sd}")
    else:
        for sd in sorted(data_dir.iterdir()):
            if sd.is_dir():
                convert_symbol(sd, args.batch_size, dedup=args.dedup)

    logger.info("Conversion complete!")


if __name__ == "__main__":
    main()
