"""Parameter optimization script for FloTrader."""

import asyncio
import subprocess
import json
from pathlib import Path
from datetime import datetime
import yaml


# Parameter combinations to test
PARAMS = [
    {"atr_multiplier": 2.5, "rr_ratio": 2.5},
    {"atr_multiplier": 3.0, "rr_ratio": 2.5},
    {"atr_multiplier": 2.5, "rr_ratio": 3.0},
    {"atr_multiplier": 3.0, "rr_ratio": 3.0},
    {"atr_multiplier": 3.5, "rr_ratio": 3.0},
    {"atr_multiplier": 3.5, "rr_ratio": 3.5},
]


def run_backtest(atr_mult: float, rr_ratio: float) -> dict:
    """Run single backtest with given parameters."""

    # Load config
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    # Modify parameters
    config["strategy"]["params"]["atr_multiplier"] = atr_mult
    config["strategy"]["params"]["rr_ratio"] = rr_ratio

    # Save temp config
    temp_config = f"config_atr{atr_mult}_rr{rr_ratio}.yaml"
    with open(temp_config, "w") as f:
        yaml.dump(config, f)

    # Run backtest
    print(f"\n{'='*60}")
    print(f"Running: ATR={atr_mult}, R:R={rr_ratio}")
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
                metrics["atr_multiplier"] = atr_mult
                metrics["rr_ratio"] = rr_ratio
                metrics["report_dir"] = str(latest)

                # Cleanup temp config
                Path(temp_config).unlink(missing_ok=True)

                return metrics

    # Cleanup temp config
    Path(temp_config).unlink(missing_ok=True)

    return {"atr_multiplier": atr_mult, "rr_ratio": rr_ratio, "error": "No metrics found"}


def main():
    """Run optimization."""
    print("FloTrader Parameter Optimization")
    print("=" * 60)

    results = []

    for params in PARAMS:
        metrics = run_backtest(params["atr_multiplier"], params["rr_ratio"])
        results.append(metrics)

    # Print summary table
    print("\n" + "=" * 80)
    print("OPTIMIZATION RESULTS")
    print("=" * 80)
    print(f"{'ATR':>5} {'R:R':>5} {'Trades':>7} {'WinRate':>8} {'PF':>6} {'Net PnL':>10} {'Return':>10} {'MaxDD':>8}")
    print("-" * 80)

    for r in results:
        if "error" in r:
            print(f"{r['atr_multiplier']:>5} {r['rr_ratio']:>5} ERROR: {r['error']}")
        else:
            print(
                f"{r['atr_multiplier']:>5} "
                f"{r['rr_ratio']:>5} "
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
        print(f"\nBest by Profit Factor: ATR={best['atr_multiplier']}, R:R={best['rr_ratio']}")
        print(f"  PF={best['profit_factor']}, WR={best['win_rate']}, Return={best['return_pct']}")


if __name__ == "__main__":
    main()
