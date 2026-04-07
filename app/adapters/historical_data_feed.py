"""Historical data feed for backtesting - streaming k-way merge implementation."""

from __future__ import annotations

import csv
import heapq
from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, TextIO

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from loguru import logger

if TYPE_CHECKING:
    import pandas as pd

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

    _TS_FORMATS = [
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]

    @classmethod
    def parse_timestamp(cls, value: str | int | float) -> datetime:
        """Parse timestamp from various formats (slow path, tries all formats)."""
        if isinstance(value, (int, float)):
            if value > 1e12:
                return datetime.utcfromtimestamp(value / 1000)
            return datetime.utcfromtimestamp(value)

        value = str(value)
        for fmt in cls._TS_FORMATS:
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue

        raise ValueError(f"Cannot parse timestamp: {value}")

    @classmethod
    def detect_ts_format(cls, value: str) -> str | None:
        """Detect timestamp format from a sample value (called once per file)."""
        value = str(value)
        for fmt in cls._TS_FORMATS:
            try:
                datetime.strptime(value, fmt)
                return fmt
            except ValueError:
                continue
        return None

    @classmethod
    def parse_timestamp_fast(cls, value: str, fmt: str) -> datetime:
        """Parse timestamp with known format (fast path, no trial-and-error)."""
        return datetime.strptime(value, fmt)


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
    # Cached timestamp format (detected on first row)
    ts_format: str | None = None
    # Parquet streaming
    is_parquet: bool = False
    parquet_event_iter: Iterator | None = None

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
        warmup_minutes: int = 0,
    ) -> None:
        """
        Initialize streaming historical data feed.

        Args:
            event_bus: Event bus to publish events to
            config: Application configuration
            start_date: Filter data from this date
            end_date: Filter data until this date
            dedupe_orderbook: Skip consecutive orderbook rows with same update_id or bid/ask
            warmup_minutes: Minutes of data to load before start_date for indicator warm-up
        """
        super().__init__(event_bus)
        self._config = config
        # Store the real trading start date before adjusting for warm-up
        self._trade_start_date = start_date
        # Shift start_date back by warmup_minutes for indicator warm-up (ATR, etc.)
        if start_date and warmup_minutes > 0:
            self._start_date = start_date - timedelta(minutes=warmup_minutes)
        else:
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

        # Cache config lookups for hot path
        params = getattr(getattr(config, 'strategy', None), 'params', None)
        self._fast_orderbook_mode = getattr(params, 'fast_orderbook_mode', False) if params else False
        self._orderbook_bucket_ms = getattr(params, 'orderbook_bucket_ms', 0) if params else 0

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
            channels = ["kline_1m", "kline_15m", "trades", "orderbook"]

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

        # Prefer parquet when format=parquet and file exists
        if self._config.data.format == DataFormat.PARQUET:
            parquet_path = file_path.with_suffix(".parquet")
            if parquet_path.exists():
                self._open_parquet_channel_stream(symbol, channel, parquet_path)
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

    # ------------------------------------------------------------------
    # Parquet streaming reader
    # ------------------------------------------------------------------

    def _open_parquet_channel_stream(
        self, symbol: str, channel: str, file_path: Path
    ) -> None:
        """Open a streaming Parquet channel (row-group-at-a-time)."""
        try:
            pf = pq.ParquetFile(str(file_path))

            # Choose generator based on channel type
            if channel == "orderbook":
                gen = self._pq_orderbook_iter(pf, symbol)
            elif channel == "trades":
                gen = self._pq_trades_iter(pf, symbol)
            elif channel.startswith("kline_"):
                interval_str = channel.replace("kline_", "")
                gen = self._pq_kline_iter(pf, symbol, interval_str)
            else:
                logger.warning(f"Unknown parquet channel: {channel}")
                return

            channel_id = f"{symbol}_{channel}"
            channel_iter = ChannelIterator(
                channel_id=channel_id,
                symbol=symbol,
                channel=channel,
                file_handle=None,
                csv_reader=None,
                schema={},
                is_parquet=True,
                parquet_event_iter=gen,
            )
            self._channel_iterators[channel_id] = channel_iter
            self._advance_channel(channel_id)
            logger.debug(f"Opened parquet channel: {channel_id} ({file_path})")

        except Exception as e:
            logger.error(f"Error opening parquet {file_path}: {e}")

    def _pq_date_bounds_ms(self) -> tuple[int | None, int | None]:
        """Pre-compute start/end date as int64 ms for fast filtering."""
        start_ms = None
        end_ms = None
        if self._start_date:
            start_ms = timegm(self._start_date.timetuple()) * 1000
        if self._end_date:
            end_ms = timegm(self._end_date.timetuple()) * 1000
        return start_ms, end_ms

    def _pq_orderbook_iter(
        self, pf: pq.ParquetFile, symbol: str
    ) -> Iterator[OrderBookEvent]:
        """Stream OrderBookEvents with row-group predicate pushdown + vectorized LOCF."""
        fast = self._fast_orderbook_mode
        start_ms, end_ms = self._pq_date_bounds_ms()
        bucket_ms = self._orderbook_bucket_ms

        # Find timestamp column index for row-group statistics
        ts_col_idx = pf.schema_arrow.get_field_index("timestamp_ms")

        # Cross-batch LOCF state
        pending_event: OrderBookEvent | None = None
        pending_bucket: int | None = None

        num_rg = pf.metadata.num_row_groups
        for rg_idx in range(num_rg):
            # Row-group predicate pushdown: skip entire row groups outside date range
            rg_meta = pf.metadata.row_group(rg_idx)
            ts_stats = rg_meta.column(ts_col_idx).statistics
            if ts_stats and ts_stats.has_min_max:
                if start_ms is not None and ts_stats.max < start_ms:
                    continue
                if end_ms is not None and ts_stats.min > end_ms:
                    continue

            table = pf.read_row_group(rg_idx)
            ts_np = table.column("timestamp_ms").to_numpy()
            n = len(ts_np)
            if n == 0:
                continue

            # Vectorized date filtering
            mask = np.ones(n, dtype=bool)
            if start_ms is not None:
                mask &= ts_np >= start_ms
            if end_ms is not None:
                mask &= ts_np <= end_ms

            if not mask.any():
                continue
            if not mask.all():
                table = table.filter(pa.array(mask))
                ts_np = ts_np[mask]
                n = len(ts_np)

            # Vectorized LOCF: keep only last event per bucket
            if bucket_ms > 0 and n > 1:
                bucket_ids = ts_np // bucket_ms
                keep = np.empty(n, dtype=bool)
                keep[:-1] = bucket_ids[:-1] != bucket_ids[1:]
                keep[-1] = True  # always keep last row of batch
                indices = np.where(keep)[0]
            else:
                indices = np.arange(n)
                bucket_ids = None

            # Extract columns as Python lists
            bp = table.column("bid_price").to_pylist()
            bs = table.column("bid_size").to_pylist()
            ap = table.column("ask_price").to_pylist()
            a_s = table.column("ask_size").to_pylist()
            uid = table.column("update_id").to_pylist()
            sq = table.column("seq").to_pylist()
            ts_list = ts_np.tolist()

            for idx_pos in range(len(indices)):
                i = int(indices[idx_pos])
                t = ts_list[i]
                u = uid[i] if uid[i] != -1 else None
                s = sq[i] if sq[i] != -1 else None

                if fast:
                    event = OrderBookEvent(
                        timestamp=datetime.utcfromtimestamp(t / 1000),
                        symbol=symbol,
                        bid_price=bp[i], bid_size=bs[i],
                        ask_price=ap[i], ask_size=a_s[i],
                        update_id=u, seq=s,
                    )
                else:
                    event = OrderBookEvent(
                        timestamp=datetime.utcfromtimestamp(t / 1000),
                        symbol=symbol,
                        bid_price=Decimal(str(bp[i])), bid_size=Decimal(str(bs[i])),
                        ask_price=Decimal(str(ap[i])), ask_size=Decimal(str(a_s[i])),
                        update_id=u, seq=s,
                    )

                if bucket_ids is not None:
                    bucket = int(bucket_ids[i])
                    if pending_event is not None and pending_bucket != bucket:
                        yield pending_event
                    pending_event = event
                    pending_bucket = bucket
                else:
                    if pending_event is not None:
                        yield pending_event
                        pending_event = None
                        pending_bucket = None
                    yield event

        # Yield last pending event
        if pending_event is not None:
            yield pending_event

    def _pq_trades_iter(
        self, pf: pq.ParquetFile, symbol: str
    ) -> Iterator[MarketTradeEvent]:
        """Stream MarketTradeEvents with row-group predicate pushdown + float output."""
        start_ms, end_ms = self._pq_date_bounds_ms()
        ts_col_idx = pf.schema_arrow.get_field_index("timestamp_ms")

        num_rg = pf.metadata.num_row_groups
        for rg_idx in range(num_rg):
            # Row-group predicate pushdown
            rg_meta = pf.metadata.row_group(rg_idx)
            ts_stats = rg_meta.column(ts_col_idx).statistics
            if ts_stats and ts_stats.has_min_max:
                if start_ms is not None and ts_stats.max < start_ms:
                    continue
                if end_ms is not None and ts_stats.min > end_ms:
                    continue

            table = pf.read_row_group(rg_idx)
            ts_np = table.column("timestamp_ms").to_numpy()
            n = len(ts_np)
            if n == 0:
                continue

            # Vectorized date filtering
            mask = np.ones(n, dtype=bool)
            if start_ms is not None:
                mask &= ts_np >= start_ms
            if end_ms is not None:
                mask &= ts_np <= end_ms

            if not mask.any():
                continue
            if not mask.all():
                table = table.filter(pa.array(mask))
                ts_np = ts_np[mask]
                n = len(ts_np)

            # Float output — no Decimal conversion (P1.3)
            pr = table.column("price").to_pylist()
            am = table.column("amount").to_pylist()
            sd = table.column("side").to_pylist()
            ts_list = ts_np.tolist()

            for i in range(n):
                yield MarketTradeEvent(
                    timestamp=datetime.utcfromtimestamp(ts_list[i] / 1000),
                    symbol=symbol,
                    price=pr[i],
                    amount=am[i],
                    side=Side.BUY if sd[i] == 1 else Side.SELL,
                )

    def _pq_kline_iter(
        self, pf: pq.ParquetFile, symbol: str, interval_str: str
    ) -> Iterator[KlineEvent]:
        """Stream KlineEvents from a Parquet file."""
        start_ms, end_ms = self._pq_date_bounds_ms()
        interval = Interval(interval_str) if interval_str in [iv.value for iv in Interval] else Interval.M1

        for batch in pf.iter_batches():
            ts = batch.column("timestamp_ms").to_pylist()
            op = batch.column("open").to_pylist()
            hi = batch.column("high").to_pylist()
            lo = batch.column("low").to_pylist()
            cl = batch.column("close").to_pylist()
            vo = batch.column("volume").to_pylist()

            for i in range(len(ts)):
                t = ts[i]
                if start_ms is not None and t < start_ms:
                    continue
                if end_ms is not None and t > end_ms:
                    continue

                yield KlineEvent(
                    timestamp=datetime.utcfromtimestamp(t / 1000),
                    symbol=symbol,
                    interval=interval,
                    open=op[i],
                    high=hi[i],
                    low=lo[i],
                    close=cl[i],
                    volume=vo[i],
                )

    # ------------------------------------------------------------------
    # Channel advance (k-way merge)
    # ------------------------------------------------------------------

    def _advance_channel(self, channel_id: str) -> bool:
        """
        Read next valid event from channel and push to heap.

        Returns True if an event was pushed, False if channel exhausted.
        """
        channel_iter = self._channel_iterators.get(channel_id)
        if not channel_iter:
            return False

        if channel_iter.is_parquet:
            return self._advance_parquet(channel_id, channel_iter)

        if not channel_iter.csv_reader:
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

                # Detect mid-file schema change (e.g., orderbook 6→10 columns)
                if None in row:
                    extra_values = row.pop(None)
                    all_values = [row[fn] for fn in channel_iter.csv_reader.fieldnames] + extra_values
                    if channel_iter.channel == "orderbook" and len(all_values) == 10:
                        new_fieldnames = [
                            "timestamp", "system_ts", "local_ts", "update_id", "seq",
                            "bid_price", "bid_size", "ask_price", "ask_size", "mid_price",
                        ]
                        row = dict(zip(new_fieldnames, all_values))
                        channel_iter.csv_reader.fieldnames = new_fieldnames
                        channel_iter.schema = CSVSchemaDetector.detect_schema_from_headers(new_fieldnames)
                        logger.info(
                            f"Detected schema change in {channel_iter.channel_id}, "
                            f"switched to {len(new_fieldnames)}-column format"
                        )

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
                # Parse timestamp (use cached format after first row)
                ts_raw = row[ts_col]
                if channel_iter.ts_format:
                    timestamp = CSVSchemaDetector.parse_timestamp_fast(ts_raw, channel_iter.ts_format)
                else:
                    timestamp = CSVSchemaDetector.parse_timestamp(ts_raw)
                    channel_iter.ts_format = CSVSchemaDetector.detect_ts_format(ts_raw)

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
                bucket_ms = self._orderbook_bucket_ms if is_orderbook and isinstance(event, OrderBookEvent) else 0

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
                channel_iter.parse_errors = getattr(channel_iter, "parse_errors", 0) + 1
                if channel_iter.parse_errors <= 3:
                    logger.warning(f"Error parsing row {channel_iter.rows_read} in {channel_id}: {e}")
                elif channel_iter.parse_errors == 4:
                    logger.warning(f"Suppressing further parse errors for {channel_id}...")
                continue

    def _advance_parquet(self, channel_id: str, ci: ChannelIterator) -> bool:
        """Advance a parquet-based channel.  Events come pre-constructed."""
        is_ob = ci.channel == "orderbook"

        while True:
            try:
                event = next(ci.parquet_event_iter)
            except StopIteration:
                # Flush pending bucket event
                if ci.bucket_pending_event:
                    ev = ci.bucket_pending_event
                    ci.bucket_pending_event = None
                    ci.current_event = ev
                    self._sequence += 1
                    heapq.heappush(self._event_heap, (ev.timestamp, self._sequence, channel_id))
                    return True
                ci.current_event = None
                return False

            ci.rows_read += 1

            # Dedupe orderbook
            if is_ob and self._dedupe_orderbook and isinstance(event, OrderBookEvent):
                bid_ask = (event.bid_price, event.bid_size, event.ask_price, event.ask_size)
                if event.update_id is not None:
                    if event.update_id == ci.last_update_id:
                        continue
                    ci.last_update_id = event.update_id
                    ci.last_bid_ask = bid_ask
                else:
                    if bid_ask == ci.last_bid_ask:
                        continue
                    ci.last_bid_ask = bid_ask

            # Downsample orderbook (LOCF)
            bucket_ms = self._orderbook_bucket_ms if is_ob and isinstance(event, OrderBookEvent) else 0
            if bucket_ms > 0:
                timestamp_ms = int(event.timestamp.timestamp() * 1000)
                bucket_id = timestamp_ms // bucket_ms
                if ci.current_bucket_id is None:
                    ci.current_bucket_id = bucket_id
                    ci.bucket_pending_event = event
                    continue
                elif bucket_id == ci.current_bucket_id:
                    ci.bucket_pending_event = event
                    continue
                else:
                    ev = ci.bucket_pending_event
                    ci.current_bucket_id = bucket_id
                    ci.bucket_pending_event = event
                    if ev:
                        ci.current_event = ev
                        self._sequence += 1
                        heapq.heappush(self._event_heap, (ev.timestamp, self._sequence, channel_id))
                        return True
                    continue

            # Valid event
            ci.current_event = event
            self._sequence += 1
            heapq.heappush(self._event_heap, (event.timestamp, self._sequence, channel_id))
            return True

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

        fast_mode = self._fast_orderbook_mode

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
            # Parse only fields needed for trading (skip system_ts, local_ts for speed)
            update_id = None
            seq = None

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

            return OrderBookEvent(
                timestamp=timestamp,
                symbol=symbol,
                bid_price=parse_price(get_val("bid_price")),
                bid_size=parse_price(get_val("bid_size")),
                ask_price=parse_price(get_val("ask_price")),
                ask_size=parse_price(get_val("ask_size")),
                update_id=update_id,
                seq=seq,
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

    @property
    def trade_start_date(self) -> datetime | None:
        """Get the actual trading start date (after warm-up period)."""
        return self._trade_start_date

    def peek_next_event(self) -> BaseEvent | None:
        """Peek at the next event without removing it."""
        if not self._event_heap:
            return None
        _, _, channel_id = self._event_heap[0]
        channel_iter = self._channel_iterators.get(channel_id)
        return channel_iter.current_event if channel_iter else None


# Alias for backward compatibility
HistoricalDataFeed = StreamingHistoricalDataFeed
