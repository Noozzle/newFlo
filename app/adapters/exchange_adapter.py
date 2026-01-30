"""Exchange adapter interface for order management."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from app.core.events import OrderStatus, OrderType, Side

if TYPE_CHECKING:
    from app.core.event_bus import EventBus


@dataclass
class OrderRequest:
    """Order request parameters."""
    symbol: str
    side: Side
    order_type: OrderType
    qty: Decimal
    price: Decimal | None = None  # Required for limit orders
    sl_price: Decimal | None = None
    tp_price: Decimal | None = None
    reduce_only: bool = False
    client_order_id: str | None = None
    time_in_force: str = "GTC"  # GTC, IOC, FOK


@dataclass
class OrderResponse:
    """Order response from exchange."""
    success: bool
    order_id: str = ""
    client_order_id: str = ""
    message: str = ""
    status: OrderStatus = OrderStatus.NEW


@dataclass
class Position:
    """Position info."""
    symbol: str
    side: Side | None
    size: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    leverage: int
    liq_price: Decimal | None = None


@dataclass
class Balance:
    """Account balance."""
    asset: str
    available: Decimal
    total: Decimal
    unrealized_pnl: Decimal = Decimal("0")


@dataclass
class Order:
    """Order info."""
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    status: OrderStatus
    price: Decimal
    qty: Decimal
    filled_qty: Decimal
    avg_fill_price: Decimal
    sl_price: Decimal | None = None
    tp_price: Decimal | None = None
    reduce_only: bool = False


class ExchangeAdapter(ABC):
    """
    Abstract base class for exchange adapters.

    Implementations:
    - SimulatedExchangeAdapter: Simulated fills for backtest
    - BybitAdapter: Live Bybit API
    """

    def __init__(self, event_bus: EventBus) -> None:
        """
        Initialize exchange adapter.

        Args:
            event_bus: Event bus for publishing order/fill events
        """
        self._event_bus = event_bus
        self._running = False

    @abstractmethod
    async def start(self) -> None:
        """Start the exchange adapter."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the exchange adapter."""
        pass

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderResponse:
        """
        Place an order.

        Args:
            request: Order parameters

        Returns:
            Order response with order ID and status
        """
        pass

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> OrderResponse:
        """
        Cancel an order.

        Args:
            symbol: Trading pair
            order_id: Order ID to cancel

        Returns:
            Cancel response
        """
        pass

    @abstractmethod
    async def cancel_all_orders(self, symbol: str | None = None) -> OrderResponse:
        """
        Cancel all orders for a symbol or all symbols.

        Args:
            symbol: Optional symbol to cancel orders for

        Returns:
            Cancel response
        """
        pass

    @abstractmethod
    async def modify_order(
        self,
        symbol: str,
        order_id: str,
        qty: Decimal | None = None,
        price: Decimal | None = None,
    ) -> OrderResponse:
        """Modify an existing order."""
        pass

    @abstractmethod
    async def set_sl_tp(
        self,
        symbol: str,
        sl_price: Decimal | None = None,
        tp_price: Decimal | None = None,
    ) -> OrderResponse:
        """
        Set stop loss and take profit for a position.

        Args:
            symbol: Trading pair
            sl_price: Stop loss price
            tp_price: Take profit price

        Returns:
            Response indicating success
        """
        pass

    @abstractmethod
    async def get_position(self, symbol: str) -> Position | None:
        """Get current position for a symbol."""
        pass

    @abstractmethod
    async def get_all_positions(self) -> list[Position]:
        """Get all open positions."""
        pass

    @abstractmethod
    async def get_balance(self, asset: str = "USDT") -> Balance:
        """Get account balance."""
        pass

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """Get open orders."""
        pass

    @abstractmethod
    async def get_order(self, symbol: str, order_id: str) -> Order | None:
        """Get specific order by ID."""
        pass

    @property
    def is_running(self) -> bool:
        """Check if adapter is running."""
        return self._running
