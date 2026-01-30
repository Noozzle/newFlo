"""Async data recorder for live data."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app.config import DataConfig
from app.core.events import KlineEvent, MarketTradeEvent, OrderBookEvent


class DataRecorder:
    """
    Data recorder for writing market data to files.
    Writes immediately without buffering for live data.
    """

    def __init__(self, config: DataConfig) -> None:
        """Initialize data recorder."""
        self._config = config
        self._base_dir = Path(config.base_dir)
        self._running = False
        self._write_lock = asyncio.Lock()

    async def start(self, symbols: list[str] | None = None) -> None:
        """Start the data recorder."""
        self._running = True
        self._base_dir.mkdir(parents=True, exist_ok=True)

        # Create directories and empty files with headers for symbols
        if symbols:
            self._create_initial_files(symbols)

        logger.info(f"Data recorder started, saving to: {self._base_dir}")

    def _create_initial_files(self, symbols: list[str]) -> None:
        """Create initial CSV files with headers for all symbols."""
        file_configs = [
            ("1m.csv", ["timestamp", "open", "high", "low", "close", "volume"]),
            ("15m.csv", ["timestamp", "open", "high", "low", "close", "volume"]),
            ("trades.csv", ["timestamp", "price", "amount", "side"]),
            ("orderbook.csv", ["timestamp", "bid_price", "bid_size", "ask_price", "ask_size", "mid_price"]),
        ]

        for symbol in symbols:
            symbol_dir = self._base_dir / symbol
            symbol_dir.mkdir(parents=True, exist_ok=True)

            for filename, headers in file_configs:
                file_path = symbol_dir / filename
                if not file_path.exists():
                    with open(file_path, "w") as f:
                        f.write(",".join(headers) + "\n")
                    logger.info(f"Created {file_path}")

    async def stop(self) -> None:
        """Stop the data recorder."""
        self._running = False
        logger.info("Data recorder stopped")

    async def record_kline(self, event: KlineEvent) -> None:
        """Record a kline event (only closed candles)."""
        if not event.is_closed:
            return

        logger.info(f"Recording kline: {event.symbol} {event.interval.value} closed at {event.timestamp}")

        data = {
            "timestamp": event.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "open": str(event.open),
            "high": str(event.high),
            "low": str(event.low),
            "close": str(event.close),
            "volume": str(event.volume),
        }
        filename = f"{event.interval.value}.csv"
        await self._write_row(event.symbol, filename, data)

    async def record_trade(self, event: MarketTradeEvent) -> None:
        """Record a market trade event."""
        data = {
            "timestamp": event.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "price": str(event.price),
            "amount": str(event.amount),
            "side": event.side.value,
        }
        await self._write_row(event.symbol, "trades.csv", data)

    async def record_orderbook(self, event: OrderBookEvent) -> None:
        """Record an orderbook event."""
        data = {
            "timestamp": event.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "bid_price": str(event.bid_price),
            "bid_size": str(event.bid_size),
            "ask_price": str(event.ask_price),
            "ask_size": str(event.ask_size),
            "mid_price": str(event.mid_price),
        }
        await self._write_row(event.symbol, "orderbook.csv", data)

    async def _write_row(self, symbol: str, filename: str, data: dict[str, Any]) -> None:
        """Write a single row to CSV file."""
        file_path = self._base_dir / symbol / filename

        try:
            async with self._write_lock:
                with open(file_path, "a") as f:
                    f.write(",".join(data.values()) + "\n")
        except Exception as e:
            logger.error(f"Error writing to {file_path}: {e}")
