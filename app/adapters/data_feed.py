"""DataFeed interface for market data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.event_bus import EventBus


class DataFeed(ABC):
    """
    Abstract base class for data feeds.

    Implementations:
    - HistoricalDataFeed: Reads CSV/Parquet files for backtest
    - LiveDataFeed: Bybit WebSocket for live trading
    """

    def __init__(self, event_bus: EventBus) -> None:
        """
        Initialize data feed.

        Args:
            event_bus: Event bus to publish events to
        """
        self._event_bus = event_bus
        self._running = False
        self._subscribed_symbols: set[str] = set()

    @abstractmethod
    async def start(self) -> None:
        """Start the data feed."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the data feed."""
        pass

    @abstractmethod
    async def subscribe(self, symbol: str, channels: list[str] | None = None) -> None:
        """
        Subscribe to market data for a symbol.

        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")
            channels: Optional list of channels ["kline_1m", "kline_15m", "trades", "orderbook"]
                     If None, subscribe to all available channels
        """
        pass

    @abstractmethod
    async def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from a symbol."""
        pass

    @property
    def is_running(self) -> bool:
        """Check if data feed is running."""
        return self._running

    @property
    def subscribed_symbols(self) -> set[str]:
        """Get set of subscribed symbols."""
        return self._subscribed_symbols.copy()
