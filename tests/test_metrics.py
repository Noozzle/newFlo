"""Tests for metrics calculation."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.core.events import Side
from app.reporting.metrics import MetricsCalculator
from app.trading.signals import Trade


def make_trade(
    trade_id: str,
    symbol: str = "BTCUSDT",
    side: Side = Side.BUY,
    entry_price: Decimal = Decimal("50000"),
    exit_price: Decimal = Decimal("50500"),
    size: Decimal = Decimal("0.1"),
    fees: Decimal = Decimal("6"),
    slippage: Decimal = Decimal("2"),
    exit_reason: str = "signal",
    hold_minutes: int = 60,
) -> Trade:
    """Helper to create a trade."""
    entry_time = datetime(2024, 1, 1, 12, 0, 0)
    exit_time = entry_time + timedelta(minutes=hold_minutes)

    if side == Side.BUY:
        gross_pnl = (exit_price - entry_price) * size
    else:
        gross_pnl = (entry_price - exit_price) * size

    return Trade(
        trade_id=trade_id,
        symbol=symbol,
        side=side,
        entry_time=entry_time,
        exit_time=exit_time,
        entry_price=entry_price,
        exit_price=exit_price,
        size=size,
        gross_pnl=gross_pnl,
        fees=fees,
        slippage_estimate=slippage,
        exit_reason=exit_reason,
    )


def test_empty_trades():
    """Test metrics with no trades."""
    metrics = MetricsCalculator.calculate(
        trades=[],
        initial_balance=Decimal("10000"),
        final_balance=Decimal("10000"),
    )

    assert metrics.total_trades == 0
    assert metrics.win_rate == Decimal("0")
    assert metrics.net_pnl == Decimal("0")


def test_single_winning_trade():
    """Test metrics with single winning trade."""
    trade = make_trade(
        "T001",
        entry_price=Decimal("50000"),
        exit_price=Decimal("50500"),
        size=Decimal("0.1"),
        fees=Decimal("6"),
    )

    # Net PnL: (50500 - 50000) * 0.1 - 6 = 50 - 6 = 44
    assert trade.net_pnl == Decimal("42")  # 50 - 6 - 2 slippage

    metrics = MetricsCalculator.calculate(
        trades=[trade],
        initial_balance=Decimal("10000"),
        final_balance=Decimal("10042"),
    )

    assert metrics.total_trades == 1
    assert metrics.winning_trades == 1
    assert metrics.losing_trades == 0
    assert metrics.win_rate == Decimal("1")
    assert metrics.net_pnl == Decimal("42")


def test_single_losing_trade():
    """Test metrics with single losing trade."""
    trade = make_trade(
        "T001",
        side=Side.BUY,
        entry_price=Decimal("50000"),
        exit_price=Decimal("49500"),
        size=Decimal("0.1"),
        fees=Decimal("6"),
    )

    # Gross PnL: (49500 - 50000) * 0.1 = -50
    # Net: -50 - 6 - 2 = -58
    assert trade.net_pnl == Decimal("-58")

    metrics = MetricsCalculator.calculate(
        trades=[trade],
        initial_balance=Decimal("10000"),
        final_balance=Decimal("9942"),
    )

    assert metrics.total_trades == 1
    assert metrics.winning_trades == 0
    assert metrics.losing_trades == 1
    assert metrics.win_rate == Decimal("0")


def test_mixed_trades():
    """Test metrics with mix of winning and losing trades."""
    trades = [
        make_trade("T001", entry_price=Decimal("50000"), exit_price=Decimal("50500")),  # Win
        make_trade("T002", entry_price=Decimal("50000"), exit_price=Decimal("49500")),  # Loss
        make_trade("T003", entry_price=Decimal("50000"), exit_price=Decimal("50800")),  # Win
        make_trade("T004", entry_price=Decimal("50000"), exit_price=Decimal("49700")),  # Loss
    ]

    metrics = MetricsCalculator.calculate(
        trades=trades,
        initial_balance=Decimal("10000"),
        final_balance=Decimal("10050"),
    )

    assert metrics.total_trades == 4
    assert metrics.winning_trades == 2
    assert metrics.losing_trades == 2
    assert metrics.win_rate == Decimal("0.5")


def test_long_short_split():
    """Test long/short trade counting."""
    trades = [
        make_trade("T001", side=Side.BUY),
        make_trade("T002", side=Side.BUY),
        make_trade("T003", side=Side.SELL, entry_price=Decimal("50000"), exit_price=Decimal("49500")),
    ]

    metrics = MetricsCalculator.calculate(
        trades=trades,
        initial_balance=Decimal("10000"),
        final_balance=Decimal("10100"),
    )

    assert metrics.long_trades == 2
    assert metrics.short_trades == 1


def test_risk_reward_ratio():
    """Test risk/reward ratio calculation."""
    # Create trades with known wins/losses
    trades = [
        make_trade("T001", entry_price=Decimal("50000"), exit_price=Decimal("50600"), fees=Decimal("0"), slippage=Decimal("0")),  # +60
        make_trade("T002", entry_price=Decimal("50000"), exit_price=Decimal("49800"), fees=Decimal("0"), slippage=Decimal("0")),  # -20
    ]

    metrics = MetricsCalculator.calculate(
        trades=trades,
        initial_balance=Decimal("10000"),
        final_balance=Decimal("10040"),
    )

    # Avg win: 60, Avg loss: 20
    # R:R = 60 / 20 = 3.0
    assert metrics.risk_reward_ratio == Decimal("3")


def test_profit_factor():
    """Test profit factor calculation."""
    trades = [
        make_trade("T001", entry_price=Decimal("50000"), exit_price=Decimal("51000"), fees=Decimal("0"), slippage=Decimal("0")),  # +100
        make_trade("T002", entry_price=Decimal("50000"), exit_price=Decimal("49600"), fees=Decimal("0"), slippage=Decimal("0")),  # -40
    ]

    metrics = MetricsCalculator.calculate(
        trades=trades,
        initial_balance=Decimal("10000"),
        final_balance=Decimal("10060"),
    )

    # Profit factor = gross_profit / gross_loss = 100 / 40 = 2.5
    assert metrics.profit_factor == Decimal("2.5")


def test_streaks():
    """Test win/loss streak calculation."""
    # Pattern: W W W L L W
    trades = [
        make_trade("T001", exit_price=Decimal("50100")),  # Win
        make_trade("T002", exit_price=Decimal("50100")),  # Win
        make_trade("T003", exit_price=Decimal("50100")),  # Win
        make_trade("T004", exit_price=Decimal("49900")),  # Loss
        make_trade("T005", exit_price=Decimal("49900")),  # Loss
        make_trade("T006", exit_price=Decimal("50100")),  # Win
    ]

    metrics = MetricsCalculator.calculate(
        trades=trades,
        initial_balance=Decimal("10000"),
        final_balance=Decimal("10100"),
    )

    assert metrics.max_win_streak == 3
    assert metrics.max_loss_streak == 2
    assert metrics.current_streak == 1  # Positive because last trade was a win


def test_hold_time_average():
    """Test average hold time calculation."""
    trades = [
        make_trade("T001", hold_minutes=60),
        make_trade("T002", hold_minutes=30),
        make_trade("T003", hold_minutes=90),
    ]

    metrics = MetricsCalculator.calculate(
        trades=trades,
        initial_balance=Decimal("10000"),
        final_balance=Decimal("10100"),
    )

    # Average: (60 + 30 + 90) / 3 = 60 minutes = 3600 seconds
    assert metrics.avg_hold_time_seconds == 3600.0


def test_return_percentage():
    """Test return percentage calculation."""
    metrics = MetricsCalculator.calculate(
        trades=[make_trade("T001")],
        initial_balance=Decimal("10000"),
        final_balance=Decimal("11000"),
    )

    # Return: (11000 - 10000) / 10000 = 0.1 = 10%
    assert metrics.return_pct == Decimal("0.1")


def test_expectancy():
    """Test expectancy calculation."""
    # 50% WR, avg win = 100, avg loss = 50
    trades = [
        make_trade("T001", entry_price=Decimal("50000"), exit_price=Decimal("51000"), fees=Decimal("0"), slippage=Decimal("0")),  # +100
        make_trade("T002", entry_price=Decimal("50000"), exit_price=Decimal("49500"), fees=Decimal("0"), slippage=Decimal("0")),  # -50
    ]

    metrics = MetricsCalculator.calculate(
        trades=trades,
        initial_balance=Decimal("10000"),
        final_balance=Decimal("10050"),
    )

    # Expectancy = (WR * avg_win) - ((1-WR) * avg_loss)
    # = (0.5 * 100) - (0.5 * 50) = 50 - 25 = 25
    assert metrics.expectancy == Decimal("25")
