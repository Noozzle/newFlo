"""Merge CSV data files - simple text concatenation preserving exact format."""

import os
from pathlib import Path


def merge_csv_files(live_file: Path, merge_file: Path, output_file: Path) -> dict:
    """Merge two CSV files preserving exact format, no deduplication."""
    stats = {"live_rows": 0, "merge_rows": 0, "final_rows": 0}

    live_lines = []
    merge_lines = []
    header = None

    # Read live file
    if live_file.exists():
        with open(live_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            if lines:
                header = lines[0].strip()
                live_lines = [line.strip() for line in lines[1:] if line.strip()]
                stats["live_rows"] = len(live_lines)

    # Read merge file
    if merge_file.exists():
        with open(merge_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            if lines:
                if header is None:
                    header = lines[0].strip()
                # Skip header from merge file
                merge_lines = [line.strip() for line in lines[1:] if line.strip()]
                stats["merge_rows"] = len(merge_lines)

    if not live_lines and not merge_lines:
        return stats

    # Combine all lines
    all_lines = live_lines + merge_lines

    # Sort by timestamp (first column)
    def get_timestamp(line):
        return line.split(",")[0]

    all_lines.sort(key=get_timestamp)
    stats["final_rows"] = len(all_lines)

    # Write output
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(header + "\n")
        for line in all_lines:
            f.write(line + "\n")

    return stats


def main():
    base_path = Path("D:/Trading/newFlo")
    live_data = base_path / "live_data"
    merge_data = base_path / "merge_data"

    # Output to live_data (overwrite)
    output_data = live_data

    # Get all symbols from merge_data
    symbols = [d.name for d in merge_data.iterdir() if d.is_dir()]

    print(f"Found {len(symbols)} symbols to merge: {symbols}\n")

    file_types = ["1m.csv", "15m.csv", "trades.csv", "orderbook.csv"]

    for symbol in symbols:
        print(f"\n{'='*50}")
        print(f"Processing {symbol}")
        print("=" * 50)

        for file_type in file_types:
            live_file = live_data / symbol / file_type
            merge_file = merge_data / symbol / file_type
            output_file = output_data / symbol / file_type

            if not merge_file.exists():
                print(f"  {file_type}: No merge file, skipping")
                continue

            print(f"\n  {file_type}:")

            stats = merge_csv_files(live_file, merge_file, output_file)

            print(f"    Live rows: {stats['live_rows']:,}")
            print(f"    Merge rows: {stats['merge_rows']:,}")
            print(f"    Final rows: {stats['final_rows']:,}")

    print("\n" + "=" * 50)
    print("MERGE COMPLETE!")
    print("=" * 50)


if __name__ == "__main__":
    main()
