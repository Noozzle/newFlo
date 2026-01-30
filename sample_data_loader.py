#!/usr/bin/env python3
"""
Sample data loader - demonstrates CSV schema auto-detection.

This script reads the live_data directory and shows:
- Detected schema for each CSV file
- Sample data parsing
- Event creation
"""

from pathlib import Path

import pandas as pd

from app.adapters.historical_data_feed import CSVSchemaDetector


def main():
    data_dir = Path("live_data")

    if not data_dir.exists():
        print(f"Data directory not found: {data_dir}")
        return

    print("=" * 60)
    print("FloTrader - CSV Schema Auto-Detection Demo")
    print("=" * 60)

    # Find all CSV files
    csv_files = list(data_dir.rglob("*.csv"))

    if not csv_files:
        print("No CSV files found in live_data/")
        return

    for csv_path in sorted(csv_files):
        print(f"\n{'-' * 60}")
        print(f"File: {csv_path}")
        print("-" * 60)

        try:
            # Read first few rows
            df = pd.read_csv(csv_path, nrows=5)

            # Detect schema
            schema = CSVSchemaDetector.detect_schema(df)

            print(f"\nColumns: {list(df.columns)}")
            print(f"Detected type: {schema.get('_type', 'unknown')}")
            print(f"\nColumn mapping:")
            for key, value in schema.items():
                if not key.startswith("_"):
                    print(f"  {key}: {value}")

            print(f"\nSample data (first 3 rows):")
            print(df.head(3).to_string(index=False))

            # Parse timestamps
            if "timestamp" in schema:
                ts_col = schema["timestamp"]
                print(f"\nTimestamp parsing:")
                for i, row in df.head(3).iterrows():
                    try:
                        ts = CSVSchemaDetector.parse_timestamp(row[ts_col])
                        print(f"  {row[ts_col]} -> {ts}")
                    except Exception as e:
                        print(f"  {row[ts_col]} -> ERROR: {e}")

        except Exception as e:
            print(f"Error reading file: {e}")

    print("\n" + "=" * 60)
    print("Schema detection complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
