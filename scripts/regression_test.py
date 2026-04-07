#!/usr/bin/env python
"""Regression harness: run 1-day backtest with CSV and Parquet paths, compare trade lists.

Usage:
    python scripts/regression_test.py
    python scripts/regression_test.py --date 2026-02-03
    python scripts/regression_test.py --tolerance 1e-6
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_latest_report(reports_dir: Path) -> Path:
    """Return the most-recently-created report subdirectory."""
    dirs = sorted(
        (d for d in reports_dir.iterdir() if d.is_dir()),
        key=lambda d: d.name,
        reverse=True,
    )
    if not dirs:
        raise FileNotFoundError(f"No report dirs in {reports_dir}")
    return dirs[0]


def _read_trades(trades_csv: Path) -> list[dict]:
    """Read trades.csv into list of dicts."""
    with open(trades_csv, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _read_signal_count(gate_csv: Path) -> int:
    """Count data rows in gate_signals.csv (excludes header)."""
    if not gate_csv.exists():
        return 0
    with open(gate_csv, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        return sum(1 for _ in reader)


def _run_backtest(
    project_dir: Path,
    config_path: Path,
    data_format: str,
    date: str,
    gate_log: Path,
) -> tuple[Path, int]:
    """Run a single backtest, return (report_dir, signal_count).

    Temporarily patches config to use the given format and single-day range,
    then restores the original config.
    """
    import yaml  # lazy import; pyyaml is already a project dep

    # Read + patch config
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    cfg["data"]["format"] = data_format
    cfg["backtest"]["start_date"] = date
    cfg["backtest"]["end_date"] = date

    # Point gate log to our temp path so we can count signals
    cfg["ai_gate"]["log_path"] = str(gate_log)

    # Write patched config to a temp file so we don't corrupt the original
    tmp_cfg = config_path.with_suffix(f".{data_format}.tmp.yaml")
    with open(tmp_cfg, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

    # Remove previous gate log so we get a clean count
    if gate_log.exists():
        gate_log.unlink()

    reports_dir = project_dir / "reports"
    existing = set(reports_dir.iterdir()) if reports_dir.exists() else set()

    try:
        env = os.environ.copy()
        env["FLOTRD_CONFIG"] = str(tmp_cfg)

        # Redirect backtest output to a log file (can be huge for CSV)
        log_file = config_path.with_suffix(f".{data_format}.log")
        with open(log_file, "w", encoding="utf-8") as lf:
            result = subprocess.run(
                [sys.executable, "-m", "app", "backtest", "--config", str(tmp_cfg)],
                cwd=str(project_dir),
                stdout=lf,
                stderr=subprocess.STDOUT,
                timeout=3600,  # 60 min max
                env=env,
            )

        if result.returncode != 0:
            # Show tail of log on failure
            with open(log_file, encoding="utf-8") as lf:
                lines = lf.readlines()
            print(f"=== TAIL ({data_format}) ===")
            print("".join(lines[-30:]))
            raise RuntimeError(f"Backtest ({data_format}) failed with code {result.returncode}")

        # Find the NEW report directory
        new_dirs = set(reports_dir.iterdir()) - existing
        if not new_dirs:
            raise FileNotFoundError(f"No new report dir created for {data_format} run")
        report_dir = max(new_dirs, key=lambda d: d.name)

        signal_count = _read_signal_count(gate_log)
        return report_dir, signal_count

    finally:
        tmp_cfg.unlink(missing_ok=True)
        log_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

COMPARE_FIELDS = ["symbol", "side", "entry_time", "exit_time", "exit_reason"]
NUMERIC_FIELDS = ["entry_price", "exit_price"]


def _compare_trades(
    csv_trades: list[dict],
    pq_trades: list[dict],
    tolerance: float,
) -> list[str]:
    """Compare two trade lists.  Return list of error messages (empty = pass)."""
    errors: list[str] = []

    if len(csv_trades) != len(pq_trades):
        errors.append(
            f"Trade count mismatch: CSV={len(csv_trades)}, Parquet={len(pq_trades)}"
        )
        return errors  # no point comparing row-by-row

    for i, (ct, pt) in enumerate(zip(csv_trades, pq_trades)):
        # Exact string fields
        for field in COMPARE_FIELDS:
            cv, pv = ct.get(field, ""), pt.get(field, "")
            if cv != pv:
                errors.append(
                    f"Trade {i} field '{field}' mismatch: CSV={cv!r}, Parquet={pv!r}"
                )

        # Numeric fields with tolerance
        for field in NUMERIC_FIELDS:
            try:
                cv = float(ct.get(field, 0))
                pv = float(pt.get(field, 0))
            except ValueError:
                errors.append(f"Trade {i} field '{field}' not numeric")
                continue

            if abs(cv - pv) > tolerance:
                errors.append(
                    f"Trade {i} field '{field}' differs: CSV={cv}, Parquet={pv}, "
                    f"delta={abs(cv - pv):.12f} > tol={tolerance}"
                )

    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Regression test: CSV vs Parquet backtest")
    parser.add_argument("--date", default="2026-02-03", help="Single-day backtest date")
    parser.add_argument(
        "--tolerance", type=float, default=1e-8,
        help="Max allowed difference for price fields",
    )
    parser.add_argument(
        "--project-dir", default=".",
        help="Project root directory",
    )
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    config_path = project_dir / "config.yaml"
    gate_log_csv = project_dir / "gate_signals_regression_csv.csv"
    gate_log_pq = project_dir / "gate_signals_regression_pq.csv"

    if not config_path.exists():
        print(f"FAIL: config.yaml not found in {project_dir}")
        sys.exit(1)

    print(f"Regression test: date={args.date}, tolerance={args.tolerance}")
    print("=" * 60)

    # --- Run CSV backtest ---
    print("\n[1/2] Running backtest with CSV format...")
    csv_report, csv_signals = _run_backtest(
        project_dir, config_path, "csv", args.date, gate_log_csv,
    )
    csv_trades = _read_trades(csv_report / "trades.csv")
    print(f"  Report: {csv_report.name}")
    print(f"  Trades: {len(csv_trades)}, Signals: {csv_signals}")

    # --- Run Parquet backtest ---
    print("\n[2/2] Running backtest with Parquet format...")
    pq_report, pq_signals = _run_backtest(
        project_dir, config_path, "parquet", args.date, gate_log_pq,
    )
    pq_trades = _read_trades(pq_report / "trades.csv")
    print(f"  Report: {pq_report.name}")
    print(f"  Trades: {len(pq_trades)}, Signals: {pq_signals}")

    # --- Compare ---
    print("\n" + "=" * 60)
    print("COMPARISON")
    print("=" * 60)

    errors: list[str] = []

    # Signal count
    if csv_signals != pq_signals:
        errors.append(f"Signal count mismatch: CSV={csv_signals}, Parquet={pq_signals}")
    print(f"  Signals:  CSV={csv_signals}  Parquet={pq_signals}  {'OK' if csv_signals == pq_signals else 'MISMATCH'}")

    # Trade count
    tc_ok = len(csv_trades) == len(pq_trades)
    print(f"  Trades:   CSV={len(csv_trades)}  Parquet={len(pq_trades)}  {'OK' if tc_ok else 'MISMATCH'}")

    # Trade details
    trade_errors = _compare_trades(csv_trades, pq_trades, args.tolerance)
    errors.extend(trade_errors)

    if not trade_errors and tc_ok:
        print("  Details:  All fields match within tolerance")
        # Print the trades for reference
        for i, t in enumerate(csv_trades):
            print(
                f"    T{i}: {t['symbol']} {t['side']} "
                f"entry={t['entry_price']} exit={t['exit_price']} "
                f"reason={t['exit_reason']}"
            )

    # Cleanup temp gate logs
    gate_log_csv.unlink(missing_ok=True)
    gate_log_pq.unlink(missing_ok=True)

    print("\n" + "=" * 60)
    if errors:
        print("FAIL: Regression test found mismatches:")
        for e in errors:
            print(f"  - {e}")
        print("=" * 60)
        sys.exit(1)
    else:
        print("PASS: CSV and Parquet backtest outputs are identical.")
        print("=" * 60)
        sys.exit(0)


if __name__ == "__main__":
    main()
