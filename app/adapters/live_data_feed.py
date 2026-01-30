"""Live data feed using Bybit WebSocket."""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from loguru import logger

from app.adapters.data_feed import DataFeed
from app.config import BybitConfig
from app.core.events import Interval, KlineEvent, MarketTradeEvent, OrderBookEvent, Side

if TYPE_CHECKING:
    from app.core.event_bus import EventBus

try:
    from pybit.unified_trading import WebSocket
    PYBIT_AVAILABLE = True
except ImportError:
    PYBIT_AVAILABLE = False
    WebSocket = None


class LiveDataFeed(DataFeed):
    """
    Live data feed using Bybit WebSocket.

    Subscribes to:
    - Public trades (publicTrade)
    - Order book (orderbook.1 for top-of-book)
    - Klines (kline.1, kline.15)
    """

    def __init__(
        self,
        event_bus: "EventBus",
        config: BybitConfig,
    ) -> None:
        """
        Initialize live data feed.

        Args:
            event_bus: Event bus for publishing events
            config: Bybit configuration
        """
        super().__init__(event_bus)
        self._config = config
        self._ws: Any = None
        self._reconnect_task: asyncio.Task | None = None
        self._handlers_registered = False
        self._loop: asyncio.AbstractEventLoop | None = None  # Store loop for thread-safe publish

        if not PYBIT_AVAILABLE:
            raise ImportError("pybit is required for live data feed. Install with: pip install pybit")

    async def start(self) -> None:
        """Start the data feed and connect to WebSocket."""
        self._running = True
        self._loop = asyncio.get_running_loop()  # Store loop for thread-safe callbacks
        await self._connect()
        logger.info("Live data feed started")

    async def stop(self) -> None:
        """Stop the data feed."""
        self._running = False

        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            self._ws.exit()

        logger.info("Live data feed stopped")

    async def _connect(self) -> None:
        """Connect to Bybit WebSocket."""
        try:
            self._ws = WebSocket(
                testnet=self._config.testnet,
                channel_type="linear",
            )
            logger.info(f"Connected to Bybit WebSocket (testnet={self._config.testnet})")
        except Exception as e:
            logger.error(f"Failed to connect to Bybit WebSocket: {e}")
            raise

    async def subscribe(self, symbol: str, channels: list[str] | None = None) -> None:
        """
        Subscribe to market data for a symbol.

        Args:
            symbol: Trading pair (e.g., "BTCUSDT")
            channels: Optional list of channels
        """
        if not self._ws:
            raise RuntimeError("WebSocket not connected")

        self._subscribed_symbols.add(symbol)

        # Default channels
        if channels is None:
            channels = ["kline_1m", "kline_15m", "trades", "orderbook"]

        for channel in channels:
            await self._subscribe_channel(symbol, channel)

        logger.info(f"Subscribed to {symbol}: {channels}")

    async def _subscribe_channel(self, symbol: str, channel: str) -> None:
        """Subscribe to a specific channel."""
        try:
            if channel == "trades":
                self._ws.trade_stream(
                    symbol=symbol,
                    callback=lambda msg: self._handle_trade(symbol, msg),
                )
            elif channel == "orderbook":
                self._ws.orderbook_stream(
                    depth=1,
                    symbol=symbol,
                    callback=lambda msg: self._handle_orderbook(symbol, msg),
                )
            elif channel == "kline_1m":
                self._ws.kline_stream(
                    interval=1,
                    symbol=symbol,
                    callback=lambda msg: self._handle_kline(symbol, "1m", msg),
                )
            elif channel == "kline_15m":
                self._ws.kline_stream(
                    interval=15,
                    symbol=symbol,
                    callback=lambda msg: self._handle_kline(symbol, "15m", msg),
                )
        except Exception as e:
            logger.error(f"Error subscribing to {channel} for {symbol}: {e}")

    async def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from a symbol."""
        self._subscribed_symbols.discard(symbol)
        # Note: pybit doesn't have a direct unsubscribe method
        # Would need to reconnect without that symbol

    def _handle_trade(self, symbol: str, message: dict[str, Any]) -> None:
        """Handle trade message from WebSocket."""
        try:
            data = message.get("data", [])
            for trade in data:
                timestamp = datetime.utcfromtimestamp(int(trade.get("T", 0)) / 1000)
                side = Side.BUY if trade.get("S") == "Buy" else Side.SELL

                event = MarketTradeEvent(
                    timestamp=timestamp,
                    symbol=symbol,
                    price=Decimal(str(trade.get("p", 0))),
                    amount=Decimal(str(trade.get("v", 0))),
                    side=side,
                    trade_id=trade.get("i", ""),
                )

                # Publish to event bus (thread-safe from WS callback)
                if self._loop:
                    asyncio.run_coroutine_threadsafe(self._event_bus.publish(event), self._loop)

        except Exception as e:
            logger.error(f"Error handling trade message: {e}")

    def _handle_orderbook(self, symbol: str, message: dict[str, Any]) -> None:
        """Handle orderbook message from WebSocket."""
        try:
            data = message.get("data", {})
            timestamp = datetime.utcfromtimestamp(int(message.get("ts", 0)) / 1000)

            bids = data.get("b", [])
            asks = data.get("a", [])

            if not bids or not asks:
                return

            # Top of book
            bid_price, bid_size = Decimal(str(bids[0][0])), Decimal(str(bids[0][1]))
            ask_price, ask_size = Decimal(str(asks[0][0])), Decimal(str(asks[0][1]))

            event = OrderBookEvent(
                timestamp=timestamp,
                symbol=symbol,
                bid_price=bid_price,
                bid_size=bid_size,
                ask_price=ask_price,
                ask_size=ask_size,
            )

            # Publish to event bus (thread-safe from WS callback)
            if self._loop:
                asyncio.run_coroutine_threadsafe(self._event_bus.publish(event), self._loop)

        except Exception as e:
            logger.error(f"Error handling orderbook message: {e}")

    def _handle_kline(self, symbol: str, interval: str, message: dict[str, Any]) -> None:
        """Handle kline message from WebSocket."""
        try:
            data = message.get("data", [])

            for kline in data:
                timestamp = datetime.utcfromtimestamp(int(kline.get("start", 0)) / 1000)
                is_closed = kline.get("confirm", False)

                # Log closed candles
                if is_closed:
                    logger.info(f"Kline CLOSED: {symbol} {interval} at {timestamp}")

                interval_enum = Interval.M1 if interval == "1m" else Interval.M15

                event = KlineEvent(
                    timestamp=timestamp,
                    symbol=symbol,
                    interval=interval_enum,
                    open=Decimal(str(kline.get("open", 0))),
                    high=Decimal(str(kline.get("high", 0))),
                    low=Decimal(str(kline.get("low", 0))),
                    close=Decimal(str(kline.get("close", 0))),
                    volume=Decimal(str(kline.get("volume", 0))),
                    is_closed=is_closed,
                )

                # Publish to event bus (thread-safe from WS callback)
                if self._loop:
                    asyncio.run_coroutine_threadsafe(self._event_bus.publish(event), self._loop)

        except Exception as e:
            logger.error(f"Error handling kline message: {e}")
