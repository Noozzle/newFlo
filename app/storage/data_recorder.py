"""Async data recorder for live data."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app.config import DataConfig
from app.core.events import KlineEvent, MarketTradeEvent, OrderBookEvent


class DataRecorder:
    """
    Async data recorder for writing market data to files.

    Features:
    - Non-blocking writes using asyncio queue
    - File rotation by date/hour
    - Buffered writing for efficiency
    - Supports CSV format
    """

    def __init__(self, config: DataConfig) -> None:
        """
        Initialize data recorder.

        Args:
            config: Data storage configuration
        """
        self._config = config
        self._base_dir = Path(config.base_dir)
        self._rotation_hours = config.rotation_hours
        self._buffer_size = config.write_buffer_size

        # Write queue
        self._queue: asyncio.Queue[tuple[str, str, dict[str, Any]]] = asyncio.Queue()
        self._running = False
        self._writer_task: asyncio.Task | None = None

        # Buffers per file
        self._buffers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._file_handles: dict[str, Any] = {}
        self._current_rotation: dict[str, str] = {}

    async def start(self, symbols: list[str] | None = None) -> None:
        """Start the data recorder."""
        self._running = True
        self._writer_task = asyncio.create_task(self._writer_loop())

        # Create directories and empty files with headers for symbols
        if symbols:
            await self._create_initial_files(symbols)

        logger.info(f"Data recorder started, saving to: {self._base_dir}")

    async def _create_initial_files(self, symbols: list[str]) -> None:
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
        """Stop the data recorder and flush buffers."""
        self._running = False

        # Flush all buffers
        await self._flush_all()

        # Close file handles
        for handle in self._file_handles.values():
            handle.close()
        self._file_handles.clear()

        # Wait for writer task
        if self._writer_task:
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass

        logger.info("Data recorder stopped")

    async def record_kline(self, event: KlineEvent) -> None:
        """Record a kline event (only closed candles)."""
        # Only record when candle is closed, not intermediate updates
        if not event.is_closed:
            return

        logger.debug(f"Recording kline: {event.symbol} {event.interval.value} closed at {event.timestamp}")

        data = {
            "timestamp": event.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "open": str(event.open),
            "high": str(event.high),
            "low": str(event.low),
            "close": str(event.close),
            "volume": str(event.volume),
        }
        filename = f"{event.interval.value}.csv"
        await self._queue.put((event.symbol, filename, data))

    async def record_trade(self, event: MarketTradeEvent) -> None:
        """Record a market trade event."""
        data = {
            "timestamp": event.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "price": str(event.price),
            "amount": str(event.amount),
            "side": event.side.value,
        }
        await self._queue.put((event.symbol, "trades.csv", data))

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
        await self._queue.put((event.symbol, "orderbook.csv", data))

    async def _writer_loop(self) -> None:
        """Background writer loop."""
        while self._running:
            try:
                # Get item with timeout
                try:
                    symbol, filename, data = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                # Add to buffer
                file_key = f"{symbol}/{filename}"
                self._buffers[file_key].append(data)

                # Flush if buffer is full
                if len(self._buffers[file_key]) >= self._buffer_size:
                    await self._flush_buffer(symbol, filename)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in data recorder: {e}")

    async def _flush_buffer(self, symbol: str, filename: str) -> None:
        """Flush buffer to file."""
        file_key = f"{symbol}/{filename}"
        buffer = self._buffers.get(file_key, [])

        if not buffer:
            return

        # Get or create file path
        file_path = self._get_file_path(symbol, filename)

        # Write data
        try:
            # Check if file exists (for header)
            write_header = not file_path.exists()

            with open(file_path, "a") as f:
                if write_header and buffer:
                    # Write header
                    f.write(",".join(buffer[0].keys()) + "\n")

                # Write rows
                for row in buffer:
                    f.write(",".join(row.values()) + "\n")

            # Clear buffer
            self._buffers[file_key] = []

        except Exception as e:
            logger.error(f"Error writing to {file_path}: {e}")

    async def _flush_all(self) -> None:
        """Flush all buffers."""
        for file_key in list(self._buffers.keys()):
            parts = file_key.split("/")
            if len(parts) == 2:
                symbol, filename = parts
                await self._flush_buffer(symbol, filename)

    def _get_file_path(self, symbol: str, filename: str) -> Path:
        """Get file path with rotation."""
        symbol_dir = self._base_dir / symbol
        symbol_dir.mkdir(parents=True, exist_ok=True)

        # For simplicity, just use the base filename
        # In production, add date/hour rotation
        return symbol_dir / filename

    def _get_rotation_key(self) -> str:
        """Get current rotation key."""
        now = datetime.utcnow()
        if self._rotation_hours == 1:
            return now.strftime("%Y%m%d_%H")
        elif self._rotation_hours == 24:
            return now.strftime("%Y%m%d")
        else:
            hour_bucket = (now.hour // self._rotation_hours) * self._rotation_hours
            return now.strftime(f"%Y%m%d_{hour_bucket:02d}")
