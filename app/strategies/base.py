"""Base strategy interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Config
    from app.core.event_bus import EventBus
    from app.core.events import KlineEvent, MarketTradeEvent, OrderBookEvent
    from app.trading.portfolio import Portfolio


class BaseStrategy(ABC):
    """
    Abstract base class for trading strategies.

    Strategies receive market events and generate trading signals.
    The same strategy code runs in both backtest and live modes.

    To create a new strategy:
    1. Inherit from BaseStrategy
    2. Implement on_kline, on_trade, on_orderbook methods
    3. Use self._emit_signal() to generate entry/exit signals
    """

    def __init__(self) -> None:
        """Initialize strategy."""
        self._event_bus: EventBus | None = None
        self._portfolio: Portfolio | None = None
        self._config: Config | None = None
        self._initialized = False

    async def initialize(
        self,
        event_bus: "EventBus",
        portfolio: "Portfolio",
        config: "Config",
    ) -> None:
        """
        Initialize strategy with runtime dependencies.

        Called by the engine before processing events.

        Args:
            event_bus: Event bus for emitting signals
            portfolio: Portfolio for position info
            config: Application configuration
        """
        self._event_bus = event_bus
        self._portfolio = portfolio
        self._config = config
        self._initialized = True
        await self._on_initialize()

    async def _on_initialize(self) -> None:
        """
        Override this for custom initialization logic.

        Called after dependencies are injected.
        """
        pass

    @abstractmethod
    async def on_kline(self, event: "KlineEvent") -> None:
        """
        Handle kline/candlestick event.

        Args:
            event: Kline event with OHLCV data
        """
        pass

    @abstractmethod
    async def on_trade(self, event: "MarketTradeEvent") -> None:
        """
        Handle market trade event.

        Args:
            event: Trade event with price, amount, side
        """
        pass

    @abstractmethod
    async def on_orderbook(self, event: "OrderBookEvent") -> None:
        """
        Handle orderbook event.

        Args:
            event: Orderbook event with bid/ask prices and sizes
        """
        pass

    async def _emit_entry_signal(
        self,
        event: "KlineEvent | MarketTradeEvent | OrderBookEvent",
        side: str,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        reason: str = "",
        **metadata,
    ) -> None:
        """
        Emit an entry signal.

        Args:
            event: Source event (for timestamp and symbol)
            side: "buy" or "sell"
            entry_price: Entry price
            sl_price: Stop loss price
            tp_price: Take profit price
            reason: Signal reason
            **metadata: Additional metadata
        """
        from decimal import Decimal
        from app.core.events import Side, SignalEvent

        if self._event_bus is None:
            return

        await self._event_bus.publish_immediate(
            SignalEvent(
                timestamp=event.timestamp,
                symbol=event.symbol,
                signal_type="entry",
                side=Side.BUY if side.lower() == "buy" else Side.SELL,
                price=Decimal(str(entry_price)),
                sl_price=Decimal(str(sl_price)),
                tp_price=Decimal(str(tp_price)),
                reason=reason,
                metadata=metadata,
            )
        )

    async def _emit_exit_signal(
        self,
        event: "KlineEvent | MarketTradeEvent | OrderBookEvent",
        reason: str,
        exit_price: float | None = None,
        **metadata,
    ) -> None:
        """
        Emit an exit signal.

        Args:
            event: Source event (for timestamp and symbol)
            reason: Exit reason (e.g., "signal", "time_exit")
            exit_price: Optional specific exit price
            **metadata: Additional metadata
        """
        from decimal import Decimal
        from app.core.events import Side, SignalEvent

        if self._event_bus is None:
            return

        await self._event_bus.publish_immediate(
            SignalEvent(
                timestamp=event.timestamp,
                symbol=event.symbol,
                signal_type="exit",
                side=Side.BUY,  # Not used for exits
                price=Decimal(str(exit_price)) if exit_price else None,
                reason=reason,
                metadata=metadata,
            )
        )

    def has_position(self, symbol: str) -> bool:
        """Check if we have an open position."""
        if self._portfolio is None:
            return False
        return self._portfolio.has_position(symbol)

    def get_position(self, symbol: str):
        """Get current position for a symbol."""
        if self._portfolio is None:
            return None
        return self._portfolio.get_position(symbol)
