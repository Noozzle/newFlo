"""Event types for the trading system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import uuid4


class Side(str, Enum):
    """Order/Trade side."""
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self == Side.BUY else Side.BUY


class OrderType(str, Enum):
    """Order type."""
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    """Order status."""
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class Interval(str, Enum):
    """Kline interval."""
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


@dataclass
class BaseEvent:
    """Base event class."""
    timestamp: datetime
    symbol: str
    event_id: str = field(default_factory=lambda: str(uuid4()))

    def __lt__(self, other: BaseEvent) -> bool:
        """Compare by timestamp for priority queue."""
        return self.timestamp < other.timestamp


@dataclass(kw_only=True)
class KlineEvent(BaseEvent):
    """Kline/candlestick event."""
    interval: Interval
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_closed: bool = True


@dataclass(kw_only=True)
class MarketTradeEvent(BaseEvent):
    """Market trade (tick) event."""
    price: Decimal
    amount: Decimal
    side: Side
    trade_id: str = ""


@dataclass(kw_only=True)
class OrderBookEvent(BaseEvent):
    """Order book snapshot/update event."""
    bid_price: Decimal
    bid_size: Decimal
    ask_price: Decimal
    ask_size: Decimal

    @property
    def mid_price(self) -> Decimal:
        return (self.bid_price + self.ask_price) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask_price - self.bid_price


@dataclass(kw_only=True)
class TimerEvent(BaseEvent):
    """Timer/scheduler event."""
    timer_id: str = ""
    interval_seconds: int = 0


@dataclass(kw_only=True)
class OrderUpdateEvent(BaseEvent):
    """Order status update event."""
    order_id: str
    client_order_id: str
    status: OrderStatus
    side: Side
    order_type: OrderType
    price: Decimal
    qty: Decimal
    filled_qty: Decimal
    avg_fill_price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    fee_asset: str = "USDT"
    sl_price: Decimal | None = None
    tp_price: Decimal | None = None
    reduce_only: bool = False
    create_time: datetime | None = None
    update_time: datetime | None = None


@dataclass(kw_only=True)
class FillEvent(BaseEvent):
    """Order fill/execution event."""
    order_id: str
    client_order_id: str
    trade_id: str
    side: Side
    price: Decimal
    qty: Decimal
    fee: Decimal
    fee_asset: str = "USDT"
    is_maker: bool = False
    realized_pnl: Decimal = Decimal("0")
    stop_order_type: str = ""  # "StopLoss", "TakeProfit", or "" for regular orders


@dataclass(kw_only=True)
class BalanceEvent(BaseEvent):
    """Account balance update event."""
    asset: str
    available: Decimal
    total: Decimal
    unrealized_pnl: Decimal = Decimal("0")


@dataclass(kw_only=True)
class PositionEvent(BaseEvent):
    """Position update event."""
    side: Side | None  # None means no position
    size: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    leverage: int = 1
    liq_price: Decimal | None = None


@dataclass(kw_only=True)
class SignalEvent(BaseEvent):
    """Trading signal from strategy."""
    signal_type: str  # "entry", "exit", "sl", "tp"
    side: Side
    price: Decimal | None = None
    sl_price: Decimal | None = None
    tp_price: Decimal | None = None
    size: Decimal | None = None
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# Type alias for all events
Event = (
    KlineEvent
    | MarketTradeEvent
    | OrderBookEvent
    | TimerEvent
    | OrderUpdateEvent
    | FillEvent
    | BalanceEvent
    | PositionEvent
    | SignalEvent
)
