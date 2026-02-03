"""Parameter optimization script for FloTrader.

Performance optimizations applied:
- fast_orderbook_mode=True (float parsing)
- orderbook_bucket_ms=500 (aggressive downsampling)
- Single symbol by default (SOLUSDT)
- 5-day test period (2026-01-30 to 2026-02-03)

To run full optimization with all symbols, modify limit_symbols parameter.
"""

import asyncio
import subprocess
import json
from pathlib import Path
from datetime import datetime
import yaml


# Parameter combinations to test
# Round 3: Optimized for speed + comprehensive testing
PARAMS = [
    # Quick baseline test (minimal params for speed check)
    {"atr_multiplier": 2.5, "rr_ratio": 3.0},  # baseline

    # Fine-tune ATR around 2.5
    {"atr_multiplier": 2.0, "rr_ratio": 3.0},
    {"atr_multiplier": 2.25, "rr_ratio": 3.0},
    {"atr_multiplier": 2.75, "rr_ratio": 3.0},
    {"atr_multiplier": 3.0, "rr_ratio": 3.0},

    # Test higher R:R with best ATR
    {"atr_multiplier": 2.5, "rr_ratio": 2.5},
    {"atr_multiplier": 2.5, "rr_ratio": 3.5},
    {"atr_multiplier": 2.5, "rr_ratio": 4.0},

    # Test imbalance threshold (critical for entry quality)
    {"atr_multiplier": 2.5, "rr_ratio": 3.0, "imbalance_threshold": 0.15},
    {"atr_multiplier": 2.5, "rr_ratio": 3.0, "imbalance_threshold": 0.25},
    {"atr_multiplier": 2.5, "rr_ratio": 3.0, "imbalance_threshold": 0.30},

    # Test delta threshold
    {"atr_multiplier": 2.5, "rr_ratio": 3.0, "delta_threshold": 0.05},
    {"atr_multiplier": 2.5, "rr_ratio": 3.0, "delta_threshold": 0.15},
    {"atr_multiplier": 2.5, "rr_ratio": 3.0, "delta_threshold": 0.20},

    # Test trend period
    {"atr_multiplier": 2.5, "rr_ratio": 3.0, "trend_period": 10},
    {"atr_multiplier": 2.5, "rr_ratio": 3.0, "trend_period": 20},
    {"atr_multiplier": 2.5, "rr_ratio": 3.0, "trend_period": 30},

    # Test cooldown (prevent overtrading)
    {"atr_multiplier": 2.5, "rr_ratio": 3.0, "cooldown_seconds": 30},
    {"atr_multiplier": 2.5, "rr_ratio": 3.0, "cooldown_seconds": 120},
    {"atr_multiplier": 2.5, "rr_ratio": 3.0, "cooldown_seconds": 300},
]


def run_backtest(params: dict) -> dict:
    """Run single backtest with given parameters."""

    # Load config
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    # Modify parameters
    atr_mult = params.get("atr_multiplier", config["strategy"]["params"]["atr_multiplier"])
    rr_ratio = params.get("rr_ratio", config["strategy"]["params"]["rr_ratio"])

    config["strategy"]["params"]["atr_multiplier"] = atr_mult
    config["strategy"]["params"]["rr_ratio"] = rr_ratio

    # Additional optional params
    if "imbalance_threshold" in params:
        config["strategy"]["params"]["imbalance_threshold"] = params["imbalance_threshold"]
    if "delta_threshold" in params:
        config["strategy"]["params"]["delta_threshold"] = params["delta_threshold"]
    if "trend_period" in params:
        config["strategy"]["params"]["trend_period"] = params["trend_period"]
    if "cooldown_seconds" in params:
        config["strategy"]["params"]["cooldown_seconds"] = params["cooldown_seconds"]

    # Force performance optimizations for all backtests
    config["strategy"]["params"]["fast_orderbook_mode"] = True
    config["strategy"]["params"]["orderbook_bucket_ms"] = 500  # Aggressive downsampling for optimization

    # Limit symbols for faster optimization (can be customized)
    if params.get("limit_symbols", False):
        config["symbols"]["trade"] = ["SOLUSDT"]  # Single symbol for speed
        config["symbols"]["record"] = []

    # Set consistent date range for optimization (can be overridden)
    if "start_date" not in params:
        config["backtest"]["start_date"] = "2026-01-30"
        config["backtest"]["end_date"] = "2026-02-03"

    # Save temp config
    param_str = "_".join(f"{k}{v}" for k, v in params.items())
    temp_config = f"config_{param_str}.yaml"
    with open(temp_config, "w") as f:
        yaml.dump(config, f)

    # Run backtest
    print(f"\n{'='*60}")
    print(f"Running: {params}")
    print(f"{'='*60}")

    result = subprocess.run(
        ["python", "-m", "app", "backtest", "-c", temp_config, "--log-level", "WARNING"],
        capture_output=True,
        text=True,
    )

    print(result.stdout)
    if result.stderr:
        # Filter out only errors, not info logs
        for line in result.stderr.split("\n"):
            if "ERROR" in line or "Exception" in line:
                print(line)

    # Find latest report
    reports_dir = Path("reports")
    if reports_dir.exists():
        report_dirs = sorted(reports_dir.iterdir(), key=lambda x: x.name, reverse=True)
        if report_dirs:
            latest = report_dirs[0]
            metrics_file = latest / "metrics.json"
            if metrics_file.exists():
                with open(metrics_file) as f:
                    metrics = json.load(f)
                metrics["params"] = params
                metrics["report_dir"] = str(latest)

                # Cleanup temp config
                Path(temp_config).unlink(missing_ok=True)

                return metrics

    # Cleanup temp config
    Path(temp_config).unlink(missing_ok=True)

    return {"params": params, "error": "No metrics found"}


def main():
    """Run optimization."""
    print("FloTrader Parameter Optimization")
    print("=" * 60)

    results = []

    for params in PARAMS:
        metrics = run_backtest(params)
        results.append(metrics)

    # Print summary table
    print("\n" + "=" * 100)
    print("OPTIMIZATION RESULTS")
    print("=" * 100)
    print(f"{'Parameters':<45} {'Trades':>7} {'WinRate':>8} {'PF':>6} {'Net PnL':>10} {'Return':>10} {'MaxDD':>8}")
    print("-" * 100)

    for r in results:
        if "error" in r:
            print(f"{str(r['params']):<45} ERROR: {r['error']}")
        else:
            params_str = ", ".join(f"{k}={v}" for k, v in r['params'].items())
            print(
                f"{params_str:<45} "
                f"{r['total_trades']:>7} "
                f"{r['win_rate']:>8} "
                f"{float(r['profit_factor']):>6.2f} "
                f"{r['net_pnl']:>10} "
                f"{r['return_pct']:>10} "
                f"{r['max_drawdown_pct']:>8}"
            )

    # Save results
    output_file = f"optimization_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {output_file}")

    # Find best by profit factor
    valid_results = [r for r in results if "error" not in r and float(r["profit_factor"]) > 0]
    if valid_results:
        best = max(valid_results, key=lambda x: float(x["profit_factor"]))
        print(f"\nBest by Profit Factor: {best['params']}")
        print(f"  PF={best['profit_factor']}, WR={best['win_rate']}, Return={best['return_pct']}")

        # Also find best by return
        best_return = max(valid_results, key=lambda x: float(x["return_pct"].rstrip("%")))
        if best_return != best:
            print(f"\nBest by Return: {best_return['params']}")
            print(f"  PF={best_return['profit_factor']}, WR={best_return['win_rate']}, Return={best_return['return_pct']}")


if __name__ == "__main__":
    main()
