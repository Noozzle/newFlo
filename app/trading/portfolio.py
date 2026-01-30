"""Portfolio and account state management."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from loguru import logger

from app.core.events import BalanceEvent, FillEvent, PositionEvent, Side
from app.trading.signals import OpenPosition, Trade


@dataclass
class EquityPoint:
    """Single point on equity curve."""
    timestamp: datetime
    balance: Decimal
    equity: Decimal
    unrealized_pnl: Decimal
    drawdown_pct: Decimal


class Portfolio:
    """
    Portfolio manager - tracks balance, equity, positions, and trades.

    This is the single source of truth for account state during both
    backtest and live trading.
    """

    def __init__(
        self,
        initial_balance: Decimal = Decimal("10000"),
    ) -> None:
        """
        Initialize portfolio.

        Args:
            initial_balance: Starting account balance
        """
        self._initial_balance = initial_balance
        self._balance = initial_balance
        self._positions: dict[str, OpenPosition] = {}
        self._pending_symbols: set[str] = set()  # Symbols with pending entry orders
        self._trades: list[Trade] = []
        self._equity_curve: list[EquityPoint] = []
        self._peak_equity = initial_balance
        self._daily_pnl = Decimal("0")
        self._daily_start_balance = initial_balance
        self._current_day: datetime | None = None
        self._total_fees = Decimal("0")
        self._trade_counter = 0

    @property
    def balance(self) -> Decimal:
        """Get current balance."""
        return self._balance

    def initialize_balance(self, balance: Decimal) -> None:
        """
        Initialize balance from exchange (for live mode).

        This sets both current balance and initial balance for
        accurate P&L and drawdown calculations.

        Args:
            balance: Current exchange balance
        """
        self._balance = balance
        self._initial_balance = balance
        self._peak_equity = balance
        self._daily_start_balance = balance

    @property
    def equity(self) -> Decimal:
        """Get current equity (balance + unrealized P&L)."""
        return self._balance + self.unrealized_pnl

    @property
    def unrealized_pnl(self) -> Decimal:
        """Get total unrealized P&L."""
        total = Decimal("0")
        for pos in self._positions.values():
            # Need current price - this should be updated externally
            pass
        return total

    @property
    def positions(self) -> dict[str, OpenPosition]:
        """Get all open positions."""
        return self._positions.copy()

    @property
    def trades(self) -> list[Trade]:
        """Get all completed trades."""
        return self._trades.copy()

    @property
    def num_open_positions(self) -> int:
        """Get number of open positions."""
        return len(self._positions)

    @property
    def daily_pnl(self) -> Decimal:
        """Get current day's P&L."""
        return self._daily_pnl

    @property
    def total_pnl(self) -> Decimal:
        """Get total P&L since start."""
        return self._balance - self._initial_balance

    @property
    def total_fees(self) -> Decimal:
        """Get total fees paid."""
        return self._total_fees

    @property
    def drawdown(self) -> Decimal:
        """Get current drawdown from peak equity."""
        if self._peak_equity <= 0:
            return Decimal("0")
        return (self._peak_equity - self.equity) / self._peak_equity * 100

    def has_position(self, symbol: str) -> bool:
        """Check if we have an open position or pending entry for a symbol."""
        return symbol in self._positions or symbol in self._pending_symbols

    def mark_pending_entry(self, symbol: str) -> None:
        """Mark symbol as having a pending entry order (prevents duplicate entries)."""
        self._pending_symbols.add(symbol)
        logger.debug(f"Marked {symbol} as pending entry")

    def clear_pending_entry(self, symbol: str) -> None:
        """Clear pending entry status for symbol."""
        self._pending_symbols.discard(symbol)
        logger.debug(f"Cleared pending entry for {symbol}")

    def get_position(self, symbol: str) -> OpenPosition | None:
        """Get position for a symbol."""
        return self._positions.get(symbol)

    def open_position(
        self,
        symbol: str,
        side: Side,
        entry_price: Decimal,
        size: Decimal,
        sl_price: Decimal,
        tp_price: Decimal,
        entry_fees: Decimal,
        timestamp: datetime,
        metadata: dict[str, Any] | None = None,
    ) -> OpenPosition:
        """
        Open a new position.

        Args:
            symbol: Trading pair
            side: Position side
            entry_price: Entry price
            size: Position size
            sl_price: Stop loss price
            tp_price: Take profit price
            entry_fees: Entry fees paid
            timestamp: Entry timestamp
            metadata: Optional signal metadata

        Returns:
            The opened position
        """
        if symbol in self._positions:
            raise ValueError(f"Position already exists for {symbol}")

        position = OpenPosition(
            symbol=symbol,
            side=side,
            entry_time=timestamp,
            entry_price=entry_price,
            size=size,
            sl_price=sl_price,
            tp_price=tp_price,
            entry_fees=entry_fees,
            signal_metadata=metadata or {},
        )

        self._positions[symbol] = position
        self._balance -= entry_fees
        self._total_fees += entry_fees

        logger.info(
            f"Opened {side.value} position: {symbol} {size}@{entry_price}, "
            f"SL={sl_price}, TP={tp_price}"
        )

        return position

    def close_position(
        self,
        symbol: str,
        exit_price: Decimal,
        exit_fees: Decimal,
        timestamp: datetime,
        exit_reason: str,
        slippage_estimate: Decimal = Decimal("0"),
    ) -> Trade | None:
        """
        Close a position and record the trade.

        Args:
            symbol: Trading pair
            exit_price: Exit price
            exit_fees: Exit fees paid
            timestamp: Exit timestamp
            exit_reason: Reason for exit (sl, tp, signal, time_exit)
            slippage_estimate: Estimated slippage cost

        Returns:
            The completed trade record, or None if no position
        """
        position = self._positions.pop(symbol, None)
        if position is None:
            logger.warning(f"No position to close for {symbol}")
            return None

        # Calculate P&L
        if position.side == Side.BUY:
            gross_pnl = (exit_price - position.entry_price) * position.size
        else:
            gross_pnl = (position.entry_price - exit_price) * position.size

        total_fees = position.entry_fees + exit_fees

        # Create trade record
        self._trade_counter += 1
        trade = Trade(
            trade_id=f"T{self._trade_counter:06d}",
            symbol=symbol,
            side=position.side,
            entry_time=position.entry_time,
            exit_time=timestamp,
            entry_price=position.entry_price,
            exit_price=exit_price,
            size=position.size,
            gross_pnl=gross_pnl,
            fees=total_fees,
            slippage_estimate=slippage_estimate,
            exit_reason=exit_reason,
        )

        self._trades.append(trade)

        # Update balance
        self._balance += gross_pnl - exit_fees
        self._total_fees += exit_fees

        # Update daily P&L
        self._daily_pnl += trade.net_pnl

        logger.info(
            f"Closed {position.side.value} {symbol}: PnL={trade.net_pnl:.2f} "
            f"(gross={gross_pnl:.2f}, fees={total_fees:.2f}), reason={exit_reason}"
        )

        return trade

    def update_equity_curve(self, timestamp: datetime, current_prices: dict[str, Decimal]) -> None:
        """
        Update equity curve with current state.

        Args:
            timestamp: Current timestamp
            current_prices: Dict of symbol -> current price
        """
        # Calculate unrealized P&L
        unrealized = Decimal("0")
        for symbol, pos in self._positions.items():
            if symbol in current_prices:
                unrealized += pos.unrealized_pnl(current_prices[symbol])

        equity = self._balance + unrealized

        # Update peak equity
        if equity > self._peak_equity:
            self._peak_equity = equity

        # Calculate drawdown
        drawdown_pct = Decimal("0")
        if self._peak_equity > 0:
            drawdown_pct = (self._peak_equity - equity) / self._peak_equity * 100

        point = EquityPoint(
            timestamp=timestamp,
            balance=self._balance,
            equity=equity,
            unrealized_pnl=unrealized,
            drawdown_pct=drawdown_pct,
        )

        self._equity_curve.append(point)

    def new_day(self, date: datetime) -> None:
        """
        Start a new trading day.

        Args:
            date: The new day's date
        """
        if self._current_day is not None:
            logger.info(f"Day ended: P&L={self._daily_pnl:.2f}")

        self._current_day = date
        self._daily_pnl = Decimal("0")
        self._daily_start_balance = self._balance

    def handle_balance_event(self, event: BalanceEvent) -> None:
        """Handle balance update event (for live trading)."""
        self._balance = event.available

    def handle_fill_event(self, event: FillEvent) -> None:
        """Handle fill event to update fees."""
        self._total_fees += event.fee

    @property
    def equity_curve(self) -> list[EquityPoint]:
        """Get equity curve data."""
        return self._equity_curve.copy()

    def get_summary(self) -> dict[str, Any]:
        """Get portfolio summary."""
        return {
            "initial_balance": str(self._initial_balance),
            "current_balance": str(self._balance),
            "equity": str(self.equity),
            "total_pnl": str(self.total_pnl),
            "total_pnl_pct": str(self.total_pnl / self._initial_balance * 100),
            "total_fees": str(self._total_fees),
            "num_trades": len(self._trades),
            "num_open_positions": self.num_open_positions,
            "max_drawdown_pct": str(max((p.drawdown_pct for p in self._equity_curve), default=Decimal("0"))),
            "peak_equity": str(self._peak_equity),
        }
