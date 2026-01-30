"""Trading signal types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.events import Side


@dataclass
class EntrySignal:
    """Entry signal from strategy."""
    timestamp: datetime
    symbol: str
    side: Side
    entry_price: Decimal
    sl_price: Decimal
    tp_price: Decimal
    size: Decimal | None = None  # If None, use risk manager to calculate
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_distance(self) -> Decimal:
        """Get risk distance (entry to SL)."""
        if self.side == Side.BUY:
            return self.entry_price - self.sl_price
        return self.sl_price - self.entry_price

    @property
    def reward_distance(self) -> Decimal:
        """Get reward distance (entry to TP)."""
        if self.side == Side.BUY:
            return self.tp_price - self.entry_price
        return self.entry_price - self.tp_price

    @property
    def risk_reward_ratio(self) -> Decimal:
        """Get risk/reward ratio."""
        risk = self.risk_distance
        if risk <= 0:
            return Decimal("0")
        return self.reward_distance / risk

    def is_valid(self) -> bool:
        """Validate signal prices make sense."""
        if self.side == Side.BUY:
            return self.sl_price < self.entry_price < self.tp_price
        return self.tp_price < self.entry_price < self.sl_price


@dataclass
class ExitSignal:
    """Exit signal from strategy."""
    timestamp: datetime
    symbol: str
    reason: str  # "sl", "tp", "time_exit", "signal", "manual"
    exit_price: Decimal | None = None  # If None, exit at market
    partial_pct: Decimal = Decimal("100")  # Percentage to close (100 = full close)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModifySignal:
    """Signal to modify SL/TP."""
    timestamp: datetime
    symbol: str
    new_sl: Decimal | None = None
    new_tp: Decimal | None = None
    reason: str = ""


@dataclass
class Trade:
    """Completed trade record."""
    trade_id: str
    symbol: str
    side: Side
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    size: Decimal
    gross_pnl: Decimal
    fees: Decimal
    slippage_estimate: Decimal
    exit_reason: str  # "sl", "tp", "time_exit", "signal"

    @property
    def net_pnl(self) -> Decimal:
        """Get net P&L after fees and slippage."""
        return self.gross_pnl - self.fees - self.slippage_estimate

    @property
    def net_r(self) -> Decimal:
        """Get net R (risk-adjusted return)."""
        # This requires knowing the original risk, which should be stored
        return self.net_pnl

    @property
    def hold_time_seconds(self) -> float:
        """Get hold time in seconds."""
        return (self.exit_time - self.entry_time).total_seconds()

    @property
    def is_winner(self) -> bool:
        """Check if trade was profitable (net)."""
        return self.net_pnl > 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat(),
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "size": str(self.size),
            "gross_pnl": str(self.gross_pnl),
            "fees": str(self.fees),
            "slippage_estimate": str(self.slippage_estimate),
            "net_pnl": str(self.net_pnl),
            "exit_reason": self.exit_reason,
            "hold_time_seconds": self.hold_time_seconds,
        }


@dataclass
class OpenPosition:
    """Open position tracking."""
    symbol: str
    side: Side
    entry_time: datetime
    entry_price: Decimal
    size: Decimal
    sl_price: Decimal
    tp_price: Decimal
    entry_fees: Decimal
    signal_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def initial_risk(self) -> Decimal:
        """Get initial risk amount."""
        risk_per_unit = abs(self.entry_price - self.sl_price)
        return risk_per_unit * self.size

    def unrealized_pnl(self, current_price: Decimal) -> Decimal:
        """Calculate unrealized P&L."""
        if self.side == Side.BUY:
            return (current_price - self.entry_price) * self.size
        return (self.entry_price - current_price) * self.size
