"""Trading metrics calculation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.core.events import Side
from app.trading.signals import Trade


@dataclass
class TradingMetrics:
    """Comprehensive trading metrics."""
    # Basic counts
    total_trades: int
    winning_trades: int
    losing_trades: int
    long_trades: int
    short_trades: int

    # Win rate
    win_rate: Decimal
    long_win_rate: Decimal
    short_win_rate: Decimal

    # P&L
    gross_pnl: Decimal
    net_pnl: Decimal
    total_fees: Decimal
    total_slippage: Decimal

    # Averages
    avg_win: Decimal
    avg_loss: Decimal
    avg_trade: Decimal
    avg_hold_time_seconds: float

    # Risk metrics
    risk_reward_ratio: Decimal  # avg_win / avg_loss
    expectancy: Decimal  # (WR * avg_win) - ((1-WR) * avg_loss)
    profit_factor: Decimal  # gross_profit / gross_loss

    # Drawdown
    max_drawdown_pct: Decimal
    max_drawdown_amount: Decimal

    # Streaks
    max_win_streak: int
    max_loss_streak: int
    current_streak: int

    # Initial/final
    initial_balance: Decimal
    final_balance: Decimal
    return_pct: Decimal

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary with string values."""
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "long_trades": self.long_trades,
            "short_trades": self.short_trades,
            "win_rate": f"{self.win_rate:.2%}",
            "long_win_rate": f"{self.long_win_rate:.2%}",
            "short_win_rate": f"{self.short_win_rate:.2%}",
            "gross_pnl": f"{self.gross_pnl:.2f}",
            "net_pnl": f"{self.net_pnl:.2f}",
            "total_fees": f"{self.total_fees:.2f}",
            "total_slippage": f"{self.total_slippage:.2f}",
            "avg_win": f"{self.avg_win:.2f}",
            "avg_loss": f"{self.avg_loss:.2f}",
            "avg_trade": f"{self.avg_trade:.2f}",
            "avg_hold_time_seconds": f"{self.avg_hold_time_seconds:.1f}",
            "risk_reward_ratio": f"{self.risk_reward_ratio:.2f}",
            "expectancy": f"{self.expectancy:.2f}",
            "profit_factor": f"{self.profit_factor:.2f}",
            "max_drawdown_pct": f"{self.max_drawdown_pct:.2%}",
            "max_drawdown_amount": f"{self.max_drawdown_amount:.2f}",
            "max_win_streak": self.max_win_streak,
            "max_loss_streak": self.max_loss_streak,
            "current_streak": self.current_streak,
            "initial_balance": f"{self.initial_balance:.2f}",
            "final_balance": f"{self.final_balance:.2f}",
            "return_pct": f"{self.return_pct:.2%}",
        }


class MetricsCalculator:
    """Calculate trading metrics from trade list."""

    @classmethod
    def calculate(
        cls,
        trades: list[Trade],
        initial_balance: Decimal,
        final_balance: Decimal,
        max_drawdown_pct: Decimal = Decimal("0"),
        max_drawdown_amount: Decimal = Decimal("0"),
    ) -> TradingMetrics:
        """
        Calculate comprehensive metrics from trades.

        Args:
            trades: List of completed trades
            initial_balance: Starting balance
            final_balance: Ending balance
            max_drawdown_pct: Maximum drawdown percentage
            max_drawdown_amount: Maximum drawdown amount

        Returns:
            TradingMetrics with all calculated metrics
        """
        if not trades:
            return cls._empty_metrics(initial_balance, final_balance)

        # Basic counts
        total_trades = len(trades)
        winners = [t for t in trades if t.is_winner]
        losers = [t for t in trades if not t.is_winner]
        longs = [t for t in trades if t.side == Side.BUY]
        shorts = [t for t in trades if t.side == Side.SELL]
        long_winners = [t for t in longs if t.is_winner]
        short_winners = [t for t in shorts if t.is_winner]

        winning_trades = len(winners)
        losing_trades = len(losers)
        long_trades = len(longs)
        short_trades = len(shorts)

        # Win rates
        win_rate = Decimal(winning_trades) / Decimal(total_trades)
        long_win_rate = Decimal(len(long_winners)) / Decimal(long_trades) if long_trades else Decimal("0")
        short_win_rate = Decimal(len(short_winners)) / Decimal(short_trades) if short_trades else Decimal("0")

        # P&L sums
        gross_pnl = sum(t.gross_pnl for t in trades)
        net_pnl = sum(t.net_pnl for t in trades)
        total_fees = sum(t.fees for t in trades)
        total_slippage = sum(t.slippage_estimate for t in trades)

        # Calculate gross profit and loss
        gross_profit = sum(t.net_pnl for t in winners)
        gross_loss = abs(sum(t.net_pnl for t in losers))

        # Averages
        avg_win = gross_profit / Decimal(winning_trades) if winning_trades else Decimal("0")
        avg_loss = gross_loss / Decimal(losing_trades) if losing_trades else Decimal("0")
        avg_trade = net_pnl / Decimal(total_trades)
        avg_hold_time = sum(t.hold_time_seconds for t in trades) / total_trades

        # Risk metrics
        risk_reward_ratio = avg_win / avg_loss if avg_loss > 0 else Decimal("0")
        expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else Decimal("0")

        # Streaks
        max_win_streak, max_loss_streak, current_streak = cls._calculate_streaks(trades)

        # Return
        return_pct = (final_balance - initial_balance) / initial_balance if initial_balance else Decimal("0")

        return TradingMetrics(
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            long_trades=long_trades,
            short_trades=short_trades,
            win_rate=win_rate,
            long_win_rate=long_win_rate,
            short_win_rate=short_win_rate,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            total_fees=total_fees,
            total_slippage=total_slippage,
            avg_win=avg_win,
            avg_loss=avg_loss,
            avg_trade=avg_trade,
            avg_hold_time_seconds=avg_hold_time,
            risk_reward_ratio=risk_reward_ratio,
            expectancy=expectancy,
            profit_factor=profit_factor,
            max_drawdown_pct=max_drawdown_pct,
            max_drawdown_amount=max_drawdown_amount,
            max_win_streak=max_win_streak,
            max_loss_streak=max_loss_streak,
            current_streak=current_streak,
            initial_balance=initial_balance,
            final_balance=final_balance,
            return_pct=return_pct,
        )

    @classmethod
    def _calculate_streaks(cls, trades: list[Trade]) -> tuple[int, int, int]:
        """Calculate winning and losing streaks."""
        if not trades:
            return 0, 0, 0

        max_win_streak = 0
        max_loss_streak = 0
        current_win_streak = 0
        current_loss_streak = 0

        for trade in trades:
            if trade.is_winner:
                current_win_streak += 1
                current_loss_streak = 0
                max_win_streak = max(max_win_streak, current_win_streak)
            else:
                current_loss_streak += 1
                current_win_streak = 0
                max_loss_streak = max(max_loss_streak, current_loss_streak)

        current_streak = current_win_streak if trades[-1].is_winner else -current_loss_streak

        return max_win_streak, max_loss_streak, current_streak

    @classmethod
    def _empty_metrics(cls, initial_balance: Decimal, final_balance: Decimal) -> TradingMetrics:
        """Return empty metrics when no trades."""
        return TradingMetrics(
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            long_trades=0,
            short_trades=0,
            win_rate=Decimal("0"),
            long_win_rate=Decimal("0"),
            short_win_rate=Decimal("0"),
            gross_pnl=Decimal("0"),
            net_pnl=Decimal("0"),
            total_fees=Decimal("0"),
            total_slippage=Decimal("0"),
            avg_win=Decimal("0"),
            avg_loss=Decimal("0"),
            avg_trade=Decimal("0"),
            avg_hold_time_seconds=0.0,
            risk_reward_ratio=Decimal("0"),
            expectancy=Decimal("0"),
            profit_factor=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            max_drawdown_amount=Decimal("0"),
            max_win_streak=0,
            max_loss_streak=0,
            current_streak=0,
            initial_balance=initial_balance,
            final_balance=final_balance,
            return_pct=Decimal("0"),
        )
