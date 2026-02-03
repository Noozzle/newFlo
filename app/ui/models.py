"""Pydantic models for UI data."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from pydantic import BaseModel


class ClosedPnLRecord(BaseModel):
    """Single closed position record from Bybit."""

    symbol: str
    side: str
    closed_pnl: Decimal
    entry_time: datetime  # When position was opened
    exit_time: datetime   # When position was closed
    avg_entry_price: Decimal
    avg_exit_price: Decimal
    closed_size: Decimal
    leverage: int
    order_type: str

    @classmethod
    def from_api(cls, data: dict) -> ClosedPnLRecord:
        """Create from Bybit API response."""
        # createdTime = entry time, updatedTime = exit time
        return cls(
            symbol=data["symbol"],
            side=data["side"],
            closed_pnl=Decimal(data["closedPnl"]),
            entry_time=datetime.fromtimestamp(int(data["createdTime"]) / 1000, tz=timezone.utc),
            exit_time=datetime.fromtimestamp(int(data["updatedTime"]) / 1000, tz=timezone.utc),
            avg_entry_price=Decimal(data["avgEntryPrice"]),
            avg_exit_price=Decimal(data["avgExitPrice"]),
            closed_size=Decimal(data["closedSize"]),
            leverage=int(data.get("leverage", 1)),
            order_type=data.get("orderType", ""),
        )


class OpenPosition(BaseModel):
    """Currently open position."""

    symbol: str
    side: str
    size: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    leverage: int
    created_time: datetime

    @classmethod
    def from_api(cls, data: dict) -> "OpenPosition":
        """Create from Bybit API response."""
        return cls(
            symbol=data["symbol"],
            side=data["side"],
            size=Decimal(data["size"]),
            entry_price=Decimal(data["avgPrice"]),
            mark_price=Decimal(data.get("markPrice", "0")),
            unrealized_pnl=Decimal(data.get("unrealisedPnl", "0")),
            leverage=int(data.get("leverage", 1)),
            created_time=datetime.fromtimestamp(int(data["createdTime"]) / 1000, tz=timezone.utc),
        )


class DayStats(BaseModel):
    """Aggregated stats for a single day."""

    date: date
    trade_count: int
    total_pnl: Decimal
    winning_trades: int
    losing_trades: int

    @property
    def is_profitable(self) -> bool:
        """Check if day was profitable."""
        return self.total_pnl > 0

    @property
    def win_rate(self) -> float:
        """Calculate win rate."""
        if self.trade_count == 0:
            return 0.0
        return self.winning_trades / self.trade_count * 100


class MonthData(BaseModel):
    """Calendar data for a month."""

    year: int
    month: int
    days: dict[int, DayStats]
    month_pnl: Decimal
    month_trades: int

    @property
    def month_name(self) -> str:
        """Get month name."""
        import calendar
        return calendar.month_name[self.month]
