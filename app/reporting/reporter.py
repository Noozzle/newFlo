"""Report generation for backtest results."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
from loguru import logger

from app.config import Config
from app.reporting.metrics import MetricsCalculator, TradingMetrics
from app.trading.portfolio import EquityPoint, Portfolio
from app.trading.signals import Trade

if TYPE_CHECKING:
    pass


class Reporter:
    """
    Generate backtest reports.

    Creates a report folder with:
    - config.yaml: Configuration snapshot
    - trades.csv: All trades
    - equity_curve.csv: Equity curve data
    - metrics.json: Calculated metrics
    - summary.md: Human-readable summary
    """

    def __init__(self, base_dir: str | Path = "reports") -> None:
        """
        Initialize reporter.

        Args:
            base_dir: Base directory for reports
        """
        self._base_dir = Path(base_dir)

    def generate_report(
        self,
        config: Config,
        portfolio: Portfolio,
        run_id: str | None = None,
    ) -> Path:
        """
        Generate a complete backtest report.

        Args:
            config: Configuration used for backtest
            portfolio: Portfolio with trades and equity curve
            run_id: Optional run ID (defaults to timestamp)

        Returns:
            Path to report directory
        """
        # Create report directory
        run_id = run_id or datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        report_dir = self._base_dir / run_id
        report_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Generating report in {report_dir}")

        # Calculate metrics
        equity_curve = portfolio.equity_curve
        # drawdown_pct in equity_curve is stored as percentage (17.66 = 17.66%)
        # Convert to decimal (0.1766) for consistency with other pct fields (win_rate, return_pct)
        max_dd_pct = max((p.drawdown_pct for p in equity_curve), default=Decimal("0"))
        max_dd_amount = max(
            ((p.drawdown_pct / 100) * portfolio.balance for p in equity_curve),
            default=Decimal("0"),
        )

        metrics = MetricsCalculator.calculate(
            trades=portfolio.trades,
            initial_balance=portfolio._initial_balance,
            final_balance=portfolio.balance,
            max_drawdown_pct=max_dd_pct / 100,  # Convert percentage to decimal
            max_drawdown_amount=max_dd_amount,
        )

        # Generate all files
        self._save_config(report_dir, config)
        self._save_trades(report_dir, portfolio.trades)
        self._save_equity_curve(report_dir, equity_curve)
        self._save_metrics(report_dir, metrics)
        self._save_summary(report_dir, config, metrics, portfolio)

        logger.info(f"Report generated: {report_dir}")
        return report_dir

    def _save_config(self, report_dir: Path, config: Config) -> None:
        """Save configuration snapshot."""
        config_path = report_dir / "config.yaml"
        config.to_yaml(config_path)

    def _save_trades(self, report_dir: Path, trades: list[Trade]) -> None:
        """Save trades to CSV."""
        if not trades:
            return

        trades_path = report_dir / "trades.csv"
        df = pd.DataFrame([t.to_dict() for t in trades])
        df.to_csv(trades_path, index=False)

    def _save_equity_curve(self, report_dir: Path, equity_curve: list[EquityPoint]) -> None:
        """Save equity curve to CSV."""
        if not equity_curve:
            return

        curve_path = report_dir / "equity_curve.csv"
        data = [
            {
                "timestamp": p.timestamp.isoformat(),
                "balance": str(p.balance),
                "equity": str(p.equity),
                "unrealized_pnl": str(p.unrealized_pnl),
                "drawdown_pct": str(p.drawdown_pct),
            }
            for p in equity_curve
        ]
        df = pd.DataFrame(data)
        df.to_csv(curve_path, index=False)

    def _save_metrics(self, report_dir: Path, metrics: TradingMetrics) -> None:
        """Save metrics to JSON."""
        metrics_path = report_dir / "metrics.json"
        with open(metrics_path, "w") as f:
            json.dump(metrics.to_dict(), f, indent=2)

    def _save_summary(
        self,
        report_dir: Path,
        config: Config,
        metrics: TradingMetrics,
        portfolio: Portfolio,
    ) -> None:
        """Save human-readable summary."""
        summary_path = report_dir / "summary.md"

        md = f"""# Backtest Report

**Generated:** {datetime.utcnow().isoformat()}

## Configuration

- **Strategy:** {config.strategy.name}
- **Symbols:** {', '.join(config.backtest.symbols or config.symbols.trade)}
- **Period:** {config.backtest.start_date} to {config.backtest.end_date}
- **Initial Capital:** ${config.backtest.initial_capital}
- **Fees:** {config.costs.fees_bps} bps
- **Slippage:** {config.costs.slippage_bps} bps

## Performance Summary

| Metric | Value |
|--------|-------|
| Total Trades | {metrics.total_trades} |
| Win Rate | {metrics.win_rate:.2%} |
| Profit Factor | {metrics.profit_factor:.2f} |
| Net P&L | ${metrics.net_pnl:.2f} |
| Return | {metrics.return_pct:.2%} |
| Max Drawdown | {metrics.max_drawdown_pct:.2%} |

## Detailed Metrics

### Trade Statistics

- **Winning Trades:** {metrics.winning_trades}
- **Losing Trades:** {metrics.losing_trades}
- **Long Trades:** {metrics.long_trades} (WR: {metrics.long_win_rate:.2%})
- **Short Trades:** {metrics.short_trades} (WR: {metrics.short_win_rate:.2%})

### Risk/Reward

- **Average Win:** ${metrics.avg_win:.2f}
- **Average Loss:** ${metrics.avg_loss:.2f}
- **Risk/Reward Ratio:** {metrics.risk_reward_ratio:.2f}
- **Expectancy:** ${metrics.expectancy:.2f}

### Costs

- **Total Fees:** ${metrics.total_fees:.2f}
- **Total Slippage:** ${metrics.total_slippage:.2f}
- **Gross P&L:** ${metrics.gross_pnl:.2f}
- **Net P&L:** ${metrics.net_pnl:.2f}

### Streaks

- **Max Win Streak:** {metrics.max_win_streak}
- **Max Loss Streak:** {metrics.max_loss_streak}
- **Current Streak:** {metrics.current_streak}

### Hold Times

- **Average Hold Time:** {metrics.avg_hold_time_seconds / 60:.1f} minutes

## Final State

- **Initial Balance:** ${metrics.initial_balance:.2f}
- **Final Balance:** ${metrics.final_balance:.2f}
- **Total Return:** {metrics.return_pct:.2%}
"""

        with open(summary_path, "w") as f:
            f.write(md)
