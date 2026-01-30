"""Historical data feed for backtesting - reads CSV/Parquet files."""

from __future__ import annotations

import heapq
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterator

import pandas as pd
from loguru import logger

from app.adapters.data_feed import DataFeed
from app.config import Config, DataFormat
from app.core.event_bus import EventBus
from app.core.events import (
    BaseEvent,
    Interval,
    KlineEvent,
    MarketTradeEvent,
    OrderBookEvent,
    Side,
)


class CSVSchemaDetector:
    """Auto-detect CSV schema and map columns."""

    TIMESTAMP_COLUMNS = ["timestamp", "time", "datetime", "date", "ts", "t"]
    OHLCV_COLUMNS = {
        "open": ["open", "o", "open_price"],
        "high": ["high", "h", "high_price"],
        "low": ["low", "l", "low_price"],
        "close": ["close", "c", "close_price"],
        "volume": ["volume", "vol", "v"],
    }
    ORDERBOOK_COLUMNS = {
        "bid_price": ["bid_price", "bid", "best_bid", "bid_px"],
        "bid_size": ["bid_size", "bid_qty", "bid_volume", "bid_sz"],
        "ask_price": ["ask_price", "ask", "best_ask", "ask_px"],
        "ask_size": ["ask_size", "ask_qty", "ask_volume", "ask_sz"],
    }
    TRADE_COLUMNS = {
        "price": ["price", "px", "trade_price"],
        "amount": ["amount", "qty", "size", "quantity", "volume"],
        "side": ["side", "direction", "type"],
    }

    @classmethod
    def detect_schema(cls, df: pd.DataFrame) -> dict[str, str]:
        """
        Detect column mappings from DataFrame.

        Returns:
            Dict mapping standard column names to actual column names
        """
        columns_lower = {c.lower(): c for c in df.columns}
        mapping = {}

        # Find timestamp column
        for ts_col in cls.TIMESTAMP_COLUMNS:
            if ts_col in columns_lower:
                mapping["timestamp"] = columns_lower[ts_col]
                break

        # Try to detect file type and map columns
        # Check if it's OHLCV data
        ohlcv_found = 0
        for std_name, variants in cls.OHLCV_COLUMNS.items():
            for variant in variants:
                if variant in columns_lower:
                    mapping[std_name] = columns_lower[variant]
                    ohlcv_found += 1
                    break

        # Check if it's orderbook data
        orderbook_found = 0
        for std_name, variants in cls.ORDERBOOK_COLUMNS.items():
            for variant in variants:
                if variant in columns_lower:
                    mapping[std_name] = columns_lower[variant]
                    orderbook_found += 1
                    break

        # Check if it's trade data
        trade_found = 0
        for std_name, variants in cls.TRADE_COLUMNS.items():
            for variant in variants:
                if variant in columns_lower:
                    mapping[std_name] = columns_lower[variant]
                    trade_found += 1
                    break

        # Determine data type
        if ohlcv_found >= 4:
            mapping["_type"] = "kline"
        elif orderbook_found >= 4:
            mapping["_type"] = "orderbook"
        elif trade_found >= 2:
            mapping["_type"] = "trade"
        else:
            mapping["_type"] = "unknown"

        return mapping

    @classmethod
    def parse_timestamp(cls, value: str | int | float) -> datetime:
        """Parse timestamp from various formats."""
        if isinstance(value, (int, float)):
            # Unix timestamp (seconds or milliseconds)
            if value > 1e12:  # Milliseconds
                return datetime.utcfromtimestamp(value / 1000)
            return datetime.utcfromtimestamp(value)

        # String timestamp
        value = str(value)
        formats = [
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue

        raise ValueError(f"Cannot parse timestamp: {value}")


class HistoricalDataFeed(DataFeed):
    """
    Historical data feed that reads CSV/Parquet files.

    Merges multiple data streams by timestamp and emits events
    in chronological order for accurate backtesting.
    """

    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> None:
        """
        Initialize historical data feed.

        Args:
            event_bus: Event bus to publish events to
            config: Application configuration
            start_date: Filter data from this date
            end_date: Filter data until this date
        """
        super().__init__(event_bus)
        self._config = config
        self._start_date = start_date
        self._end_date = end_date
        self._data_dir = Path(config.data.base_dir)
        self._event_heap: list[tuple[datetime, int, BaseEvent]] = []
        self._sequence = 0

    async def start(self) -> None:
        """Start the data feed - load all data into memory."""
        self._running = True
        logger.info(
            f"Starting historical data feed from {self._data_dir}, "
            f"date range: {self._start_date} to {self._end_date}"
        )

    async def stop(self) -> None:
        """Stop the data feed."""
        self._running = False
        self._event_heap.clear()
        logger.info("Historical data feed stopped")

    async def subscribe(self, symbol: str, channels: list[str] | None = None) -> None:
        """
        Subscribe to historical data for a symbol.

        Loads CSV files for the symbol and adds events to the merge heap.
        """
        self._subscribed_symbols.add(symbol)
        symbol_dir = self._data_dir / symbol

        if not symbol_dir.exists():
            logger.warning(f"No data directory found for {symbol}")
            return

        # Default channels - trades excluded by default due to large file sizes (GB)
        # Use channels=["trades"] explicitly if needed for orderflow strategies
        if channels is None:
            channels = ["kline_1m", "kline_15m", "orderbook"]

        for channel in channels:
            logger.info(f"Loading {channel} for {symbol}...")
            await self._load_channel_data(symbol, channel)

        logger.info(f"Subscribed to {symbol}, total events in queue: {len(self._event_heap)}")

    async def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from a symbol."""
        self._subscribed_symbols.discard(symbol)
        # Remove events for this symbol from heap
        self._event_heap = [
            (ts, seq, e) for ts, seq, e in self._event_heap if e.symbol != symbol
        ]
        heapq.heapify(self._event_heap)

    async def _load_channel_data(self, symbol: str, channel: str) -> None:
        """Load data for a specific channel."""
        symbol_dir = self._data_dir / symbol

        # Map channel to filename
        filename_map = {
            "kline_1m": "1m.csv",
            "kline_15m": "15m.csv",
            "trades": "trades.csv",
            "orderbook": "orderbook.csv",
        }

        filename = filename_map.get(channel)
        if not filename:
            logger.warning(f"Unknown channel: {channel}")
            return

        file_path = symbol_dir / filename
        if not file_path.exists():
            logger.debug(f"File not found: {file_path}")
            return

        # Determine format
        if self._config.data.format == DataFormat.PARQUET:
            parquet_path = file_path.with_suffix(".parquet")
            if parquet_path.exists():
                file_path = parquet_path

        # Load and parse
        try:
            if file_path.suffix == ".parquet":
                df = pd.read_parquet(file_path)
            else:
                df = pd.read_csv(file_path)

            schema = CSVSchemaDetector.detect_schema(df)
            events = self._parse_dataframe(df, schema, symbol, channel)

            for event in events:
                self._sequence += 1
                heapq.heappush(
                    self._event_heap,
                    (event.timestamp, self._sequence, event),
                )

            logger.debug(f"Loaded {len(events)} events from {file_path}")

        except Exception as e:
            logger.error(f"Error loading {file_path}: {e}")

    def _parse_dataframe(
        self,
        df: pd.DataFrame,
        schema: dict[str, str],
        symbol: str,
        channel: str,
    ) -> list[BaseEvent]:
        """Parse DataFrame into events based on detected schema."""
        events: list[BaseEvent] = []

        if "timestamp" not in schema:
            logger.error(f"No timestamp column found in schema for {channel}")
            return events

        ts_col = schema["timestamp"]
        total_rows = len(df)
        filtered_count = 0

        for idx, row in df.iterrows():
            try:
                timestamp = CSVSchemaDetector.parse_timestamp(row[ts_col])

                # Filter by date range
                if self._start_date and timestamp < self._start_date:
                    filtered_count += 1
                    continue
                if self._end_date and timestamp > self._end_date:
                    filtered_count += 1
                    continue

                event = self._row_to_event(row, schema, symbol, channel, timestamp)
                if event:
                    events.append(event)

            except Exception as e:
                logger.trace(f"Error parsing row: {e}")
                continue

        logger.debug(f"Parsed {len(events)} events from {total_rows} rows (filtered: {filtered_count})")
        return events

    def _row_to_event(
        self,
        row: pd.Series,
        schema: dict[str, str],
        symbol: str,
        channel: str,
        timestamp: datetime,
    ) -> BaseEvent | None:
        """Convert a row to an event based on channel type."""
        data_type = schema.get("_type", "unknown")

        if channel.startswith("kline_") or data_type == "kline":
            interval_str = channel.replace("kline_", "") if channel.startswith("kline_") else "1m"
            interval = Interval(interval_str) if interval_str in [i.value for i in Interval] else Interval.M1

            return KlineEvent(
                timestamp=timestamp,
                symbol=symbol,
                interval=interval,
                open=Decimal(str(row.get(schema.get("open", "open"), 0))),
                high=Decimal(str(row.get(schema.get("high", "high"), 0))),
                low=Decimal(str(row.get(schema.get("low", "low"), 0))),
                close=Decimal(str(row.get(schema.get("close", "close"), 0))),
                volume=Decimal(str(row.get(schema.get("volume", "volume"), 0))),
            )

        elif channel == "orderbook" or data_type == "orderbook":
            return OrderBookEvent(
                timestamp=timestamp,
                symbol=symbol,
                bid_price=Decimal(str(row.get(schema.get("bid_price", "bid_price"), 0))),
                bid_size=Decimal(str(row.get(schema.get("bid_size", "bid_size"), 0))),
                ask_price=Decimal(str(row.get(schema.get("ask_price", "ask_price"), 0))),
                ask_size=Decimal(str(row.get(schema.get("ask_size", "ask_size"), 0))),
            )

        elif channel == "trades" or data_type == "trade":
            side_value = str(row.get(schema.get("side", "side"), "buy")).lower()
            side = Side.BUY if side_value in ("buy", "b", "bid", "1") else Side.SELL

            return MarketTradeEvent(
                timestamp=timestamp,
                symbol=symbol,
                price=Decimal(str(row.get(schema.get("price", "price"), 0))),
                amount=Decimal(str(row.get(schema.get("amount", "amount"), 0))),
                side=side,
            )

        return None

    async def emit_events(self) -> int:
        """
        Emit all events to the event bus in chronological order.

        Returns:
            Number of events emitted
        """
        count = 0
        while self._event_heap:
            _, _, event = heapq.heappop(self._event_heap)
            await self._event_bus.publish(event)
            count += 1

        logger.info(f"Emitted {count} historical events")
        return count

    def iter_events(self) -> Iterator[BaseEvent]:
        """
        Iterate over events in chronological order.

        Yields events one by one without loading all into event bus.
        """
        while self._event_heap:
            _, _, event = heapq.heappop(self._event_heap)
            yield event

    @property
    def total_events(self) -> int:
        """Get total number of events loaded."""
        return len(self._event_heap)

    def peek_next_event(self) -> BaseEvent | None:
        """Peek at the next event without removing it."""
        if not self._event_heap:
            return None
        return self._event_heap[0][2]
