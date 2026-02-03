"""Historical data feed for backtesting - streaming k-way merge implementation."""

from __future__ import annotations

import csv
import heapq
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterator, TextIO

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
    # Optional orderbook columns for extended Bybit data
    ORDERBOOK_OPTIONAL_COLUMNS = {
        "update_id": ["update_id", "u"],
        "seq": ["seq"],
        "exchange_ts": ["exchange_ts", "cts"],  # Matching engine time
        "system_ts": ["system_ts", "ts"],  # System timestamp
        "local_ts": ["local_ts"],  # Local receive time
    }
    TRADE_COLUMNS = {
        "price": ["price", "px", "trade_price"],
        "amount": ["amount", "qty", "size", "quantity", "volume"],
        "side": ["side", "direction", "type"],
    }

    @classmethod
    def detect_schema_from_headers(cls, headers: list[str]) -> dict[str, str]:
        """
        Detect column mappings from CSV headers.

        Returns:
            Dict mapping standard column names to actual column names
        """
        columns_lower = {c.lower(): c for c in headers}
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

        # Check for optional orderbook columns (update_id, seq, cts, etc.)
        for std_name, variants in cls.ORDERBOOK_OPTIONAL_COLUMNS.items():
            for variant in variants:
                if variant in columns_lower:
                    mapping[std_name] = columns_lower[variant]
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
    def detect_schema(cls, df: pd.DataFrame) -> dict[str, str]:
        """Detect column mappings from DataFrame (legacy compatibility)."""
        return cls.detect_schema_from_headers(list(df.columns))

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


@dataclass
class ChannelIterator:
    """Wraps a file iterator for a single channel."""
    channel_id: str
    symbol: str
    channel: str
    file_handle: TextIO | None
    csv_reader: Iterator | None
    schema: dict[str, str]
    current_event: BaseEvent | None = None
    rows_read: int = 0
    events_yielded: int = 0
    # For orderbook dedupe
    last_update_id: int | None = None
    last_bid_ask: tuple | None = None
    # For orderbook downsampling (LOCF - Last Observation Carried Forward)
    current_bucket_id: int | None = None
    bucket_pending_event: BaseEvent | None = None

    def close(self):
        """Close file handle."""
        if self.file_handle:
            self.file_handle.close()
            self.file_handle = None


class StreamingHistoricalDataFeed(DataFeed):
    """
    Streaming historical data feed using k-way merge.

    Instead of loading all data into memory, this implementation:
    - Opens each CSV file as a streaming iterator
    - Maintains a min-heap with one event per channel
    - Reads next event from channel only after its current event is consumed

    Memory usage: O(num_channels) instead of O(total_events)
    """

    def __init__(
        self,
        event_bus: EventBus,
        config: Config,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        dedupe_orderbook: bool = True,
    ) -> None:
        """
        Initialize streaming historical data feed.

        Args:
            event_bus: Event bus to publish events to
            config: Application configuration
            start_date: Filter data from this date
            end_date: Filter data until this date
            dedupe_orderbook: Skip consecutive orderbook rows with same update_id or bid/ask
        """
        super().__init__(event_bus)
        self._config = config
        self._start_date = start_date
        self._end_date = end_date
        self._dedupe_orderbook = dedupe_orderbook
        self._data_dir = Path(config.data.base_dir)

        # K-way merge state
        self._channel_iterators: dict[str, ChannelIterator] = {}
        self._event_heap: list[tuple[datetime, int, str]] = []  # (timestamp, seq, channel_id)
        self._sequence = 0

        # Stats
        self._total_events_yielded = 0

    async def start(self) -> None:
        """Start the data feed."""
        self._running = True
        logger.info(
            f"Starting streaming historical data feed from {self._data_dir}, "
            f"date range: {self._start_date} to {self._end_date}"
        )

    async def stop(self) -> None:
        """Stop the data feed and close all file handles."""
        self._running = False

        # Close all file handles
        for channel_iter in self._channel_iterators.values():
            channel_iter.close()
        self._channel_iterators.clear()
        self._event_heap.clear()

        logger.info(f"Streaming historical data feed stopped. Total events: {self._total_events_yielded}")

    async def subscribe(self, symbol: str, channels: list[str] | None = None) -> None:
        """
        Subscribe to historical data for a symbol.

        Opens streaming iterators for each channel file.
        """
        self._subscribed_symbols.add(symbol)
        symbol_dir = self._data_dir / symbol

        if not symbol_dir.exists():
            logger.warning(f"No data directory found for {symbol}")
            return

        # Default channels
        if channels is None:
            channels = ["kline_1m", "kline_15m", "orderbook"]

        for channel in channels:
            await self._open_channel_stream(symbol, channel)

        logger.info(f"Subscribed to {symbol} with {len(channels)} channels (streaming mode)")

    async def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from a symbol."""
        self._subscribed_symbols.discard(symbol)

        # Close and remove channel iterators for this symbol
        to_remove = [cid for cid, ci in self._channel_iterators.items() if ci.symbol == symbol]
        for channel_id in to_remove:
            self._channel_iterators[channel_id].close()
            del self._channel_iterators[channel_id]

        # Rebuild heap without this symbol's events
        self._event_heap = [
            (ts, seq, cid) for ts, seq, cid in self._event_heap
            if cid not in to_remove
        ]
        heapq.heapify(self._event_heap)

    async def _open_channel_stream(self, symbol: str, channel: str) -> None:
        """Open a streaming iterator for a channel."""
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

        # For parquet files, fall back to pandas (streaming not supported)
        if self._config.data.format == DataFormat.PARQUET:
            parquet_path = file_path.with_suffix(".parquet")
            if parquet_path.exists():
                logger.warning(f"Parquet streaming not supported, loading {parquet_path} into memory")
                await self._load_parquet_channel(symbol, channel, parquet_path)
                return

        try:
            # Open CSV file for streaming
            file_handle = open(file_path, "r", newline="", encoding="utf-8")
            csv_reader = csv.DictReader(file_handle)

            # Detect schema from headers
            if csv_reader.fieldnames:
                schema = CSVSchemaDetector.detect_schema_from_headers(list(csv_reader.fieldnames))
            else:
                logger.error(f"No headers in {file_path}")
                file_handle.close()
                return

            channel_id = f"{symbol}_{channel}"
            channel_iter = ChannelIterator(
                channel_id=channel_id,
                symbol=symbol,
                channel=channel,
                file_handle=file_handle,
                csv_reader=csv_reader,
                schema=schema,
            )

            self._channel_iterators[channel_id] = channel_iter

            # Read first event and push to heap
            self._advance_channel(channel_id)

            logger.debug(f"Opened streaming channel: {channel_id}")

        except Exception as e:
            logger.error(f"Error opening {file_path}: {e}")

    async def _load_parquet_channel(self, symbol: str, channel: str, file_path: Path) -> None:
        """Load parquet file (fallback - loads into memory)."""
        try:
            df = pd.read_parquet(file_path)
            schema = CSVSchemaDetector.detect_schema(df)

            # Create a generator from dataframe rows
            def row_generator():
                for _, row in df.iterrows():
                    yield dict(row)

            channel_id = f"{symbol}_{channel}"
            channel_iter = ChannelIterator(
                channel_id=channel_id,
                symbol=symbol,
                channel=channel,
                file_handle=None,  # No file handle for parquet
                csv_reader=row_generator(),
                schema=schema,
            )

            self._channel_iterators[channel_id] = channel_iter
            self._advance_channel(channel_id)

        except Exception as e:
            logger.error(f"Error loading parquet {file_path}: {e}")

    def _advance_channel(self, channel_id: str) -> bool:
        """
        Read next valid event from channel and push to heap.

        Returns True if an event was pushed, False if channel exhausted.
        """
        channel_iter = self._channel_iterators.get(channel_id)
        if not channel_iter or not channel_iter.csv_reader:
            return False

        schema = channel_iter.schema
        ts_col = schema.get("timestamp")
        if not ts_col:
            return False

        is_orderbook = channel_iter.channel == "orderbook" or schema.get("_type") == "orderbook"

        while True:
            try:
                row = next(channel_iter.csv_reader)
                channel_iter.rows_read += 1
            except StopIteration:
                # Channel exhausted - yield pending event if exists (for downsampling)
                if channel_iter.bucket_pending_event:
                    event_to_yield = channel_iter.bucket_pending_event
                    channel_iter.bucket_pending_event = None
                    channel_iter.current_event = event_to_yield
                    self._sequence += 1
                    heapq.heappush(self._event_heap, (event_to_yield.timestamp, self._sequence, channel_id))
                    return True
                else:
                    channel_iter.current_event = None
                    return False

            try:
                # Parse timestamp
                timestamp = CSVSchemaDetector.parse_timestamp(row[ts_col])

                # Filter by date range
                if self._start_date and timestamp < self._start_date:
                    continue
                if self._end_date and timestamp > self._end_date:
                    continue

                # Convert row to event
                event = self._row_to_event(row, schema, channel_iter.symbol, channel_iter.channel, timestamp)
                if not event:
                    continue

                # Dedupe orderbook
                if is_orderbook and self._dedupe_orderbook and isinstance(event, OrderBookEvent):
                    current_bid_ask = (event.bid_price, event.bid_size, event.ask_price, event.ask_size)

                    if event.update_id is not None:
                        if event.update_id == channel_iter.last_update_id:
                            continue
                        channel_iter.last_update_id = event.update_id
                        channel_iter.last_bid_ask = current_bid_ask
                    else:
                        if current_bid_ask == channel_iter.last_bid_ask:
                            continue
                        channel_iter.last_bid_ask = current_bid_ask

                # Downsample orderbook (LOCF - Last Observation Carried Forward)
                bucket_ms = 0
                if is_orderbook and isinstance(event, OrderBookEvent):
                    if hasattr(self._config, 'strategy') and hasattr(self._config.strategy, 'params'):
                        bucket_ms = getattr(self._config.strategy.params, 'orderbook_bucket_ms', 0)

                if bucket_ms > 0 and is_orderbook:
                    # Calculate bucket ID from timestamp
                    timestamp_ms = int(timestamp.timestamp() * 1000)
                    bucket_id = timestamp_ms // bucket_ms

                    # Check if we moved to a new bucket
                    if channel_iter.current_bucket_id is None:
                        # First event - initialize bucket
                        channel_iter.current_bucket_id = bucket_id
                        channel_iter.bucket_pending_event = event
                        continue  # Keep reading to fill this bucket
                    elif bucket_id == channel_iter.current_bucket_id:
                        # Same bucket - update pending event (LOCF: keep last)
                        channel_iter.bucket_pending_event = event
                        continue  # Keep reading
                    else:
                        # New bucket - yield pending event from previous bucket
                        event_to_yield = channel_iter.bucket_pending_event
                        # Store current event as pending for this new bucket
                        channel_iter.current_bucket_id = bucket_id
                        channel_iter.bucket_pending_event = event
                        # Yield the pending event from previous bucket
                        if event_to_yield:
                            channel_iter.current_event = event_to_yield
                            self._sequence += 1
                            heapq.heappush(self._event_heap, (event_to_yield.timestamp, self._sequence, channel_id))
                            return True
                        else:
                            continue

                # Valid event found (no downsampling or non-orderbook)
                channel_iter.current_event = event
                self._sequence += 1
                heapq.heappush(self._event_heap, (timestamp, self._sequence, channel_id))
                return True

            except Exception as e:
                logger.trace(f"Error parsing row in {channel_id}: {e}")
                continue

    def _row_to_event(
        self,
        row: dict,
        schema: dict[str, str],
        symbol: str,
        channel: str,
        timestamp: datetime,
    ) -> BaseEvent | None:
        """Convert a row dict to an event based on channel type."""
        data_type = schema.get("_type", "unknown")

        # Determine if we should use fast mode for orderbook parsing
        fast_mode = (
            hasattr(self._config, 'strategy') and
            hasattr(self._config.strategy, 'params') and
            getattr(self._config.strategy.params, 'fast_orderbook_mode', False)
        )

        def get_val(key: str, default: str = "0") -> str:
            actual_col = schema.get(key, key)
            return str(row.get(actual_col, default))

        def get_val_or_none(key: str) -> str | None:
            actual_col = schema.get(key)
            if not actual_col:
                return None
            val = row.get(actual_col)
            if val is None or str(val).strip() == "":
                return None
            return str(val)

        def parse_price(val_str: str) -> Decimal | float:
            """Parse price as float in fast mode, Decimal otherwise."""
            if fast_mode:
                return float(val_str)
            else:
                return Decimal(val_str)

        if channel.startswith("kline_") or data_type == "kline":
            interval_str = channel.replace("kline_", "") if channel.startswith("kline_") else "1m"
            interval = Interval(interval_str) if interval_str in [i.value for i in Interval] else Interval.M1

            return KlineEvent(
                timestamp=timestamp,
                symbol=symbol,
                interval=interval,
                open=Decimal(get_val("open")),
                high=Decimal(get_val("high")),
                low=Decimal(get_val("low")),
                close=Decimal(get_val("close")),
                volume=Decimal(get_val("volume")),
            )

        elif channel == "orderbook" or data_type == "orderbook":
            # Parse optional extended fields
            update_id = None
            seq = None
            exchange_ts = None
            system_ts = None
            local_ts = None

            val = get_val_or_none("update_id")
            if val:
                try:
                    update_id = int(val)
                except (ValueError, TypeError):
                    pass

            val = get_val_or_none("seq")
            if val:
                try:
                    seq = int(val)
                except (ValueError, TypeError):
                    pass

            val = get_val_or_none("exchange_ts")
            if val:
                try:
                    exchange_ts = CSVSchemaDetector.parse_timestamp(val)
                except (ValueError, TypeError):
                    pass

            val = get_val_or_none("system_ts")
            if val:
                try:
                    system_ts = CSVSchemaDetector.parse_timestamp(val)
                except (ValueError, TypeError):
                    pass

            val = get_val_or_none("local_ts")
            if val:
                try:
                    local_ts = CSVSchemaDetector.parse_timestamp(val)
                except (ValueError, TypeError):
                    pass

            # Use exchange_ts (cts) as primary timestamp if available
            effective_timestamp = exchange_ts if exchange_ts else timestamp

            return OrderBookEvent(
                timestamp=effective_timestamp,
                symbol=symbol,
                bid_price=parse_price(get_val("bid_price")),
                bid_size=parse_price(get_val("bid_size")),
                ask_price=parse_price(get_val("ask_price")),
                ask_size=parse_price(get_val("ask_size")),
                update_id=update_id,
                seq=seq,
                exchange_ts=exchange_ts,
                system_ts=system_ts,
                local_ts=local_ts,
            )

        elif channel == "trades" or data_type == "trade":
            side_value = get_val("side", "buy").lower()
            side = Side.BUY if side_value in ("buy", "b", "bid", "1") else Side.SELL

            return MarketTradeEvent(
                timestamp=timestamp,
                symbol=symbol,
                price=Decimal(get_val("price")),
                amount=Decimal(get_val("amount")),
                side=side,
            )

        return None

    def iter_events(self) -> Iterator[BaseEvent]:
        """
        Iterate over events in chronological order (streaming).

        Yields events one by one, reading from files on demand.
        Memory usage stays constant regardless of file size.
        """
        while self._event_heap:
            # Pop next event
            _, _, channel_id = heapq.heappop(self._event_heap)

            channel_iter = self._channel_iterators.get(channel_id)
            if not channel_iter or not channel_iter.current_event:
                continue

            event = channel_iter.current_event
            channel_iter.events_yielded += 1
            self._total_events_yielded += 1

            # Advance this channel to get next event
            self._advance_channel(channel_id)

            yield event

    async def emit_events(self) -> int:
        """
        Emit all events to the event bus in chronological order.

        Returns:
            Number of events emitted
        """
        count = 0
        for event in self.iter_events():
            await self._event_bus.publish(event)
            count += 1

        logger.info(f"Emitted {count} historical events")
        return count

    @property
    def total_events(self) -> int:
        """Get number of events yielded so far (streaming - not total in file)."""
        return self._total_events_yielded

    @property
    def heap_size(self) -> int:
        """Get current heap size (should be <= num_channels)."""
        return len(self._event_heap)

    @property
    def num_channels(self) -> int:
        """Get number of active channels."""
        return len(self._channel_iterators)

    def peek_next_event(self) -> BaseEvent | None:
        """Peek at the next event without removing it."""
        if not self._event_heap:
            return None
        _, _, channel_id = self._event_heap[0]
        channel_iter = self._channel_iterators.get(channel_id)
        return channel_iter.current_event if channel_iter else None


# Alias for backward compatibility
HistoricalDataFeed = StreamingHistoricalDataFeed
