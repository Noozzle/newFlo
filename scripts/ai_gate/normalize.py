"""Normalize live CSVs to Parquet with unified int64-ms timestamps.

Reads:
    live_data/<SYMBOL>/{1m.csv, 15m.csv, orderbook.csv, trades.csv}
    live_trades/trades_*.csv

Writes:
    <out>/<SYMBOL>/candles_1m.parquet
    <out>/<SYMBOL>/candles_15m.parquet
    <out>/<SYMBOL>/l1.parquet
    <out>/<SYMBOL>/exchange_trades.parquet
    <out>/my_trades.parquet   (all symbols combined)

Timestamp convention:
    All `ts` columns are int64 milliseconds since Unix epoch (UTC).
    Source CSVs have ISO datetimes without timezone — assumed UTC.

Usage:
    python -m scripts.ai_gate.normalize --symbol DOGEUSDT --out data/normalized
    python -m scripts.ai_gate.normalize --all --out data/normalized
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

import pandas as pd
from loguru import logger

# Chunk size for large files (orderbook ~24M rows, trades ~12M rows)
CHUNK_SIZE = 500_000


def _iso_to_ms(series: pd.Series) -> pd.Series:
    """Convert ISO datetime strings to int64 ms since epoch (UTC).

    Assumes UTC if no timezone info present.
    """
    dt = pd.to_datetime(series, utc=True)
    return (dt.astype("int64") // 10**6).astype("int64")


def _normalize_candles(csv_path: Path, out_path: Path) -> None:
    """Normalize 1m or 15m candle CSV → Parquet."""
    if not csv_path.exists():
        logger.warning(f"Not found: {csv_path}")
        return

    df = pd.read_csv(csv_path)
    df["ts"] = _iso_to_ms(df["timestamp"])
    df = df[["ts", "open", "high", "low", "close", "volume"]]

    # Ensure numeric types
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df.sort_values("ts", inplace=True)
    df.drop_duplicates(subset=["ts"], keep="last", inplace=True)
    df.reset_index(drop=True, inplace=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    logger.info(f"{csv_path.name} → {out_path} ({len(df)} rows)")


def _normalize_large_csv_chunked(
    csv_path: Path,
    out_path: Path,
    columns_map: dict[str, str],
    derived_cols: dict[str, str] | None = None,
) -> None:
    """Normalize large CSV (orderbook/trades) in chunks → Parquet.

    Args:
        csv_path: Source CSV.
        out_path: Destination Parquet.
        columns_map: {source_col: dest_col} mapping. 'timestamp' always → 'ts'.
        derived_cols: Optional {col_name: expression} for new columns (eval'd on chunk).
    """
    if not csv_path.exists():
        logger.warning(f"Not found: {csv_path}")
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)

    chunks_written = 0
    total_rows = 0
    parts: list[pd.DataFrame] = []

    for chunk in pd.read_csv(csv_path, chunksize=CHUNK_SIZE, on_bad_lines="skip"):
        chunk["ts"] = _iso_to_ms(chunk["timestamp"])

        # Rename columns
        rename = {k: v for k, v in columns_map.items() if k != "timestamp" and k in chunk.columns}
        chunk.rename(columns=rename, inplace=True)

        # Derived columns
        if derived_cols:
            for col_name, expr in derived_cols.items():
                chunk[col_name] = eval(expr, {"__builtins__": {}}, {"chunk": chunk, "pd": pd})  # noqa: S307

        # Select only needed output columns
        out_cols = ["ts"] + [v for k, v in columns_map.items() if k != "timestamp"]
        if derived_cols:
            out_cols += list(derived_cols.keys())
        out_cols = [c for c in out_cols if c in chunk.columns]

        parts.append(chunk[out_cols])
        total_rows += len(chunk)
        chunks_written += 1

        if chunks_written % 10 == 0:
            logger.debug(f"  {csv_path.name}: {total_rows:,} rows processed...")

    if not parts:
        logger.warning(f"No data in {csv_path}")
        return

    df = pd.concat(parts, ignore_index=True)
    df.sort_values("ts", inplace=True)
    df.reset_index(drop=True, inplace=True)
    df.to_parquet(out_path, index=False)
    logger.info(f"{csv_path.name} → {out_path} ({total_rows:,} rows)")


def normalize_symbol(symbol: str, live_data_dir: Path, out_dir: Path) -> None:
    """Normalize all market data for one symbol."""
    src = live_data_dir / symbol
    dst = out_dir / symbol

    if not src.exists():
        logger.error(f"Source dir not found: {src}")
        return

    logger.info(f"Normalizing {symbol}...")

    # Candles (small, fit in memory)
    _normalize_candles(src / "1m.csv", dst / "candles_1m.parquet")
    _normalize_candles(src / "15m.csv", dst / "candles_15m.parquet")

    # Orderbook (large, chunked)
    _normalize_large_csv_chunked(
        csv_path=src / "orderbook.csv",
        out_path=dst / "l1.parquet",
        columns_map={
            "timestamp": "ts",
            "bid_price": "bid_price",
            "bid_size": "bid_size",
            "ask_price": "ask_price",
            "ask_size": "ask_size",
            "mid_price": "mid_price",
        },
        derived_cols={
            "spread": "chunk['ask_price'] - chunk['bid_price']",
        },
    )

    # Exchange trades (large, chunked)
    _normalize_large_csv_chunked(
        csv_path=src / "trades.csv",
        out_path=dst / "exchange_trades.parquet",
        columns_map={
            "timestamp": "ts",
            "price": "price",
            "amount": "amount",
            "side": "side",
        },
    )


def normalize_my_trades(live_trades_dir: Path, out_path: Path) -> None:
    """Normalize live_trades/trades_*.csv → single Parquet."""
    pattern = str(live_trades_dir / "trades_*.csv")
    files = sorted(glob.glob(pattern))

    if not files:
        logger.warning(f"No trade files matching {pattern}")
        return

    dfs = []
    for f in files:
        df = pd.read_csv(f)
        if df.empty:
            continue
        dfs.append(df)

    if not dfs:
        logger.warning("All trade files empty")
        return

    df = pd.concat(dfs, ignore_index=True)

    # Convert timestamps
    df["ts_entry"] = _iso_to_ms(df["entry_time"])
    df["ts_exit"] = _iso_to_ms(df["exit_time"])

    # Numeric columns
    num_cols = ["entry_price", "exit_price", "size", "gross_pnl", "fees",
                "slippage_estimate", "net_pnl", "hold_time_seconds"]
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    out_cols = [
        "ts_entry", "ts_exit", "trade_id", "symbol", "side",
        "entry_price", "exit_price", "size",
        "gross_pnl", "fees", "slippage_estimate", "net_pnl",
        "exit_reason", "hold_time_seconds",
    ]
    df = df[[c for c in out_cols if c in df.columns]]
    df.sort_values("ts_entry", inplace=True)
    df.drop_duplicates(subset=["trade_id"], keep="last", inplace=True)
    df.reset_index(drop=True, inplace=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    logger.info(f"my_trades → {out_path} ({len(df)} rows from {len(files)} files)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize live CSVs to Parquet with int64-ms timestamps"
    )
    parser.add_argument("--symbol", type=str, help="Single symbol to normalize")
    parser.add_argument("--all", action="store_true", help="Normalize all symbols in live_data/")
    parser.add_argument("--out", type=str, default="data/normalized", help="Output directory")
    parser.add_argument("--live-data", type=str, default="live_data", help="Live data directory")
    parser.add_argument("--live-trades", type=str, default="live_trades", help="Live trades directory")
    args = parser.parse_args()

    live_data_dir = Path(args.live_data)
    live_trades_dir = Path(args.live_trades)
    out_dir = Path(args.out)

    if args.all:
        symbols = sorted(p.name for p in live_data_dir.iterdir() if p.is_dir())
    elif args.symbol:
        symbols = [args.symbol]
    else:
        parser.error("Specify --symbol DOGEUSDT or --all")
        return

    for symbol in symbols:
        normalize_symbol(symbol, live_data_dir, out_dir)

    normalize_my_trades(live_trades_dir, out_dir / "my_trades.parquet")
    logger.info("Done.")


if __name__ == "__main__":
    main()
