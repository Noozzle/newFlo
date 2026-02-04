"""Aggregate live_data to match live recording format:
- orderbook: 1 record per second (first)
- klines: 1 record per timestamp (last - closed candle)
"""

import pandas as pd
from pathlib import Path
from loguru import logger
import sys


def aggregate_orderbook(file_path: Path) -> int:
    """Aggregate orderbook to 1 record per second (keep first)."""
    logger.info(f"Processing {file_path}")

    df = pd.read_csv(file_path, low_memory=False)
    original_count = len(df)
    logger.info(f"  Original rows: {original_count:,}")

    if original_count == 0:
        return 0

    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
    df['ts_second'] = df['timestamp'].dt.floor('s')
    df_agg = df.drop_duplicates(subset=['ts_second'], keep='first')
    df_agg = df_agg.drop(columns=['ts_second'])
    df_agg['timestamp'] = df_agg['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S.%f')

    new_count = len(df_agg)
    reduction = (1 - new_count / original_count) * 100 if original_count > 0 else 0
    logger.info(f"  Aggregated rows: {new_count:,} (reduced by {reduction:.1f}%)")

    # Backup and save
    backup_path = file_path.with_suffix('.csv.bak')
    if not backup_path.exists():
        file_path.rename(backup_path)
        logger.info(f"  Backup saved to {backup_path}")

    df_agg.to_csv(file_path, index=False)
    return new_count


def aggregate_klines(file_path: Path) -> int:
    """Aggregate klines to 1 record per timestamp (keep last - closed candle)."""
    logger.info(f"Processing {file_path}")

    df = pd.read_csv(file_path, low_memory=False)
    original_count = len(df)
    logger.info(f"  Original rows: {original_count:,}")

    if original_count == 0:
        return 0

    # Parse timestamp - handle both formats
    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')

    # For klines, floor to the minute and keep LAST entry (closed candle has final values)
    df['ts_minute'] = df['timestamp'].dt.floor('min')
    df_agg = df.drop_duplicates(subset=['ts_minute'], keep='last')
    df_agg = df_agg.drop(columns=['ts_minute'])

    # Format timestamp back (without microseconds for klines)
    df_agg['timestamp'] = df_agg['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S')

    new_count = len(df_agg)
    reduction = (1 - new_count / original_count) * 100 if original_count > 0 else 0
    logger.info(f"  Aggregated rows: {new_count:,} (reduced by {reduction:.1f}%)")

    # Backup and save
    backup_path = file_path.with_suffix('.csv.bak')
    if not backup_path.exists():
        file_path.rename(backup_path)
        logger.info(f"  Backup saved to {backup_path}")

    df_agg.to_csv(file_path, index=False)
    return new_count


def main():
    """Process all data files in live_data directory."""
    base_dir = Path("live_data")

    if not base_dir.exists():
        logger.error(f"Directory not found: {base_dir}")
        sys.exit(1)

    stats = {
        'orderbook': {'before': 0, 'after': 0},
        '1m': {'before': 0, 'after': 0},
        '15m': {'before': 0, 'after': 0},
    }

    for symbol_dir in sorted(base_dir.iterdir()):
        if not symbol_dir.is_dir():
            continue

        logger.info(f"\n{'='*50}")
        logger.info(f"Symbol: {symbol_dir.name}")
        logger.info('='*50)

        # Process orderbook
        orderbook_file = symbol_dir / "orderbook.csv"
        if orderbook_file.exists():
            before = sum(1 for _ in open(orderbook_file)) - 1  # minus header
            stats['orderbook']['before'] += before
            after = aggregate_orderbook(orderbook_file)
            stats['orderbook']['after'] += after

        # Process 1m klines
        kline_1m = symbol_dir / "1m.csv"
        if kline_1m.exists():
            before = sum(1 for _ in open(kline_1m)) - 1
            stats['1m']['before'] += before
            after = aggregate_klines(kline_1m)
            stats['1m']['after'] += after

        # Process 15m klines
        kline_15m = symbol_dir / "15m.csv"
        if kline_15m.exists():
            before = sum(1 for _ in open(kline_15m)) - 1
            stats['15m']['before'] += before
            after = aggregate_klines(kline_15m)
            stats['15m']['after'] += after

    # Summary
    logger.info(f"\n{'='*50}")
    logger.info("SUMMARY")
    logger.info('='*50)
    for file_type, counts in stats.items():
        if counts['before'] > 0:
            reduction = (1 - counts['after'] / counts['before']) * 100
            logger.info(f"{file_type}: {counts['before']:,} -> {counts['after']:,} ({reduction:.1f}% reduction)")


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>")
    main()
