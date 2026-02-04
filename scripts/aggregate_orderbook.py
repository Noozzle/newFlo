"""Aggregate orderbook data to 1 record per second (like live mode)."""

import pandas as pd
from pathlib import Path
from loguru import logger
import sys


def aggregate_orderbook(file_path: Path) -> int:
    """
    Aggregate orderbook CSV to 1 record per second.

    Args:
        file_path: Path to orderbook.csv

    Returns:
        Number of rows after aggregation
    """
    logger.info(f"Processing {file_path}")

    # Read CSV
    df = pd.read_csv(file_path, low_memory=False)
    original_count = len(df)
    logger.info(f"  Original rows: {original_count:,}")

    # Parse timestamps
    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')

    # Floor to second
    df['ts_second'] = df['timestamp'].dt.floor('s')

    # Keep first record per second
    df_agg = df.drop_duplicates(subset=['ts_second'], keep='first')

    # Drop helper column and restore original timestamp column
    df_agg = df_agg.drop(columns=['ts_second'])

    # Format timestamp back to original format
    df_agg['timestamp'] = df_agg['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S.%f')

    new_count = len(df_agg)
    reduction = (1 - new_count / original_count) * 100
    logger.info(f"  Aggregated rows: {new_count:,} (reduced by {reduction:.1f}%)")

    # Backup original
    backup_path = file_path.with_suffix('.csv.bak')
    if not backup_path.exists():
        file_path.rename(backup_path)
        logger.info(f"  Backup saved to {backup_path}")
    else:
        logger.info(f"  Backup already exists, overwriting original")

    # Save aggregated
    df_agg.to_csv(file_path, index=False)
    logger.info(f"  Saved to {file_path}")

    return new_count


def main():
    """Process all orderbook files in live_data directory."""
    base_dir = Path("live_data")

    if not base_dir.exists():
        logger.error(f"Directory not found: {base_dir}")
        sys.exit(1)

    total_before = 0
    total_after = 0

    for symbol_dir in sorted(base_dir.iterdir()):
        if not symbol_dir.is_dir():
            continue

        orderbook_file = symbol_dir / "orderbook.csv"
        if not orderbook_file.exists():
            logger.warning(f"No orderbook.csv in {symbol_dir}")
            continue

        # Get original size
        original_size = orderbook_file.stat().st_size
        total_before += pd.read_csv(orderbook_file, usecols=[0]).shape[0]

        # Aggregate
        new_count = aggregate_orderbook(orderbook_file)
        total_after += new_count

    logger.info("=" * 50)
    logger.info(f"Total: {total_before:,} -> {total_after:,} rows")
    logger.info(f"Overall reduction: {(1 - total_after / total_before) * 100:.1f}%")


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>")
    main()
