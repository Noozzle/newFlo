"""Tests for streaming k-way merge HistoricalDataFeed.

Verifies that:
1. Events are globally ordered by timestamp across channels
2. Memory usage is bounded (heap size <= num_channels)
3. Orderbook dedupe works in streaming mode
4. All channel types are properly parsed
"""

import pytest
import tempfile
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from app.adapters.historical_data_feed import (
    StreamingHistoricalDataFeed,
    CSVSchemaDetector,
    HistoricalDataFeed,
)
from app.config import Config, DataConfig, Mode
from app.core.events import KlineEvent, OrderBookEvent, MarketTradeEvent, Interval


@pytest.fixture
def temp_data_dir():
    """Create a temporary data directory with test CSV files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        symbol_dir = Path(tmpdir) / "BTCUSDT"
        symbol_dir.mkdir()

        # Create 1m.csv
        with open(symbol_dir / "1m.csv", "w") as f:
            f.write("timestamp,open,high,low,close,volume\n")
            base = datetime(2024, 1, 1, 0, 0, 0)
            for i in range(100):
                ts = base + timedelta(minutes=i)
                f.write(f"{ts.strftime('%Y-%m-%d %H:%M:%S')},100,110,90,105,1000\n")

        # Create 15m.csv
        with open(symbol_dir / "15m.csv", "w") as f:
            f.write("timestamp,open,high,low,close,volume\n")
            base = datetime(2024, 1, 1, 0, 0, 0)
            for i in range(20):
                ts = base + timedelta(minutes=i * 15)
                f.write(f"{ts.strftime('%Y-%m-%d %H:%M:%S')},100,115,85,108,5000\n")

        # Create orderbook.csv with many rows
        with open(symbol_dir / "orderbook.csv", "w") as f:
            f.write("timestamp,bid_price,bid_size,ask_price,ask_size,update_id\n")
            base = datetime(2024, 1, 1, 0, 0, 0)
            for i in range(1000):
                ts = base + timedelta(seconds=i * 6)  # Every 6 seconds
                f.write(f"{ts.strftime('%Y-%m-%d %H:%M:%S')},50000,1.0,50001,1.0,{i+1}\n")

        # Create trades.csv
        with open(symbol_dir / "trades.csv", "w") as f:
            f.write("timestamp,price,amount,side\n")
            base = datetime(2024, 1, 1, 0, 0, 0)
            for i in range(500):
                ts = base + timedelta(seconds=i * 12)  # Every 12 seconds
                side = "buy" if i % 2 == 0 else "sell"
                f.write(f"{ts.strftime('%Y-%m-%d %H:%M:%S')},50000.5,0.1,{side}\n")

        yield tmpdir


@pytest.fixture
def config(temp_data_dir):
    """Create test config pointing to temp data dir."""
    return Config(
        mode=Mode.BACKTEST,
        data=DataConfig(base_dir=temp_data_dir),
    )


@pytest.fixture
def mock_event_bus():
    """Create mock event bus."""
    bus = MagicMock()
    return bus


class TestStreamingGlobalOrdering:
    """Tests for global event ordering across channels."""

    @pytest.mark.asyncio
    async def test_events_globally_ordered(self, config, mock_event_bus):
        """All events should be yielded in timestamp order."""
        feed = StreamingHistoricalDataFeed(mock_event_bus, config)
        await feed.start()
        await feed.subscribe("BTCUSDT", ["kline_1m", "kline_15m", "orderbook", "trades"])

        events = list(feed.iter_events())

        # Verify ordering
        timestamps = [e.timestamp for e in events]
        assert timestamps == sorted(timestamps), "Events not in timestamp order!"

        # Should have events from all channels
        event_types = set(type(e).__name__ for e in events)
        assert "KlineEvent" in event_types
        assert "OrderBookEvent" in event_types
        assert "MarketTradeEvent" in event_types

        await feed.stop()

    @pytest.mark.asyncio
    async def test_interleaved_channels(self, config, mock_event_bus):
        """Events from different channels should interleave correctly."""
        feed = StreamingHistoricalDataFeed(mock_event_bus, config)
        await feed.start()
        await feed.subscribe("BTCUSDT", ["kline_1m", "orderbook"])

        events = list(feed.iter_events())

        # Should have both klines and orderbook interleaved
        last_kline_ts = None
        last_ob_ts = None

        for e in events:
            if isinstance(e, KlineEvent):
                if last_kline_ts:
                    assert e.timestamp >= last_kline_ts
                last_kline_ts = e.timestamp
            elif isinstance(e, OrderBookEvent):
                if last_ob_ts:
                    assert e.timestamp >= last_ob_ts
                last_ob_ts = e.timestamp

        await feed.stop()


class TestStreamingMemoryBound:
    """Tests that memory usage is bounded."""

    @pytest.mark.asyncio
    async def test_heap_size_bounded_by_channels(self, config, mock_event_bus):
        """Heap size should never exceed number of channels."""
        feed = StreamingHistoricalDataFeed(mock_event_bus, config)
        await feed.start()
        await feed.subscribe("BTCUSDT", ["kline_1m", "kline_15m", "orderbook", "trades"])

        num_channels = feed.num_channels
        max_heap_size_observed = 0

        for event in feed.iter_events():
            current_heap_size = feed.heap_size
            max_heap_size_observed = max(max_heap_size_observed, current_heap_size)

            # Invariant: heap_size <= num_channels
            assert current_heap_size <= num_channels, (
                f"Heap size {current_heap_size} exceeds num_channels {num_channels}"
            )

        # Max observed should be exactly num_channels (before any channel exhausts)
        # or less if some channels finished early
        assert max_heap_size_observed <= num_channels

        await feed.stop()

    @pytest.mark.asyncio
    async def test_large_file_constant_memory(self, mock_event_bus):
        """Even with large files, memory should stay constant."""
        with tempfile.TemporaryDirectory() as tmpdir:
            symbol_dir = Path(tmpdir) / "BTCUSDT"
            symbol_dir.mkdir()

            # Create a large orderbook.csv (10000 rows)
            with open(symbol_dir / "orderbook.csv", "w") as f:
                f.write("timestamp,bid_price,bid_size,ask_price,ask_size,update_id\n")
                base = datetime(2024, 1, 1, 0, 0, 0)
                for i in range(10000):
                    ts = base + timedelta(milliseconds=i * 100)
                    f.write(f"{ts.strftime('%Y-%m-%d %H:%M:%S.%f')},50000,1.0,50001,1.0,{i+1}\n")

            config = Config(mode=Mode.BACKTEST, data=DataConfig(base_dir=tmpdir))
            feed = StreamingHistoricalDataFeed(mock_event_bus, config)
            await feed.start()
            await feed.subscribe("BTCUSDT", ["orderbook"])

            events_processed = 0
            for event in feed.iter_events():
                events_processed += 1
                # Check heap size periodically
                if events_processed % 1000 == 0:
                    assert feed.heap_size <= 1, f"Heap grew to {feed.heap_size}"

            assert events_processed == 10000
            await feed.stop()


class TestStreamingDedupe:
    """Tests for orderbook deduplication in streaming mode."""

    @pytest.mark.asyncio
    async def test_orderbook_dedupe_by_update_id(self, mock_event_bus):
        """Consecutive rows with same update_id should be deduped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            symbol_dir = Path(tmpdir) / "BTCUSDT"
            symbol_dir.mkdir()

            with open(symbol_dir / "orderbook.csv", "w") as f:
                f.write("timestamp,bid_price,bid_size,ask_price,ask_size,update_id\n")
                f.write("2024-01-01 00:00:00,50000,1.0,50001,1.0,100\n")
                f.write("2024-01-01 00:00:01,50000,1.0,50001,1.0,100\n")  # Duplicate
                f.write("2024-01-01 00:00:02,50000,1.0,50001,1.0,101\n")
                f.write("2024-01-01 00:00:03,50000,1.0,50001,1.0,101\n")  # Duplicate

            config = Config(mode=Mode.BACKTEST, data=DataConfig(base_dir=tmpdir))
            feed = StreamingHistoricalDataFeed(mock_event_bus, config, dedupe_orderbook=True)
            await feed.start()
            await feed.subscribe("BTCUSDT", ["orderbook"])

            events = list(feed.iter_events())

            # Should only have 2 events (100 and 101)
            assert len(events) == 2
            assert events[0].update_id == 100
            assert events[1].update_id == 101

            await feed.stop()

    @pytest.mark.asyncio
    async def test_orderbook_dedupe_by_bid_ask(self, mock_event_bus):
        """When no update_id, dedupe by bid/ask values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            symbol_dir = Path(tmpdir) / "BTCUSDT"
            symbol_dir.mkdir()

            with open(symbol_dir / "orderbook.csv", "w") as f:
                f.write("timestamp,bid_price,bid_size,ask_price,ask_size\n")
                f.write("2024-01-01 00:00:00,50000,1.0,50001,1.0\n")
                f.write("2024-01-01 00:00:01,50000,1.0,50001,1.0\n")  # Same bid/ask
                f.write("2024-01-01 00:00:02,50002,2.0,50003,2.0\n")  # Different

            config = Config(mode=Mode.BACKTEST, data=DataConfig(base_dir=tmpdir))
            feed = StreamingHistoricalDataFeed(mock_event_bus, config, dedupe_orderbook=True)
            await feed.start()
            await feed.subscribe("BTCUSDT", ["orderbook"])

            events = list(feed.iter_events())

            # Should only have 2 events
            assert len(events) == 2
            assert events[0].bid_price == Decimal("50000")
            assert events[1].bid_price == Decimal("50002")

            await feed.stop()


class TestStreamingDateFilter:
    """Tests for date range filtering in streaming mode."""

    @pytest.mark.asyncio
    async def test_start_date_filter(self, config, mock_event_bus):
        """Events before start_date should be filtered."""
        start = datetime(2024, 1, 1, 0, 30, 0)  # 30 minutes in
        feed = StreamingHistoricalDataFeed(mock_event_bus, config, start_date=start)
        await feed.start()
        await feed.subscribe("BTCUSDT", ["kline_1m"])

        events = list(feed.iter_events())

        for e in events:
            assert e.timestamp >= start, f"Event {e.timestamp} before start_date {start}"

        await feed.stop()

    @pytest.mark.asyncio
    async def test_end_date_filter(self, config, mock_event_bus):
        """Events after end_date should be filtered."""
        end = datetime(2024, 1, 1, 0, 30, 0)
        feed = StreamingHistoricalDataFeed(mock_event_bus, config, end_date=end)
        await feed.start()
        await feed.subscribe("BTCUSDT", ["kline_1m"])

        events = list(feed.iter_events())

        for e in events:
            assert e.timestamp <= end, f"Event {e.timestamp} after end_date {end}"

        await feed.stop()


class TestStreamingMultiSymbol:
    """Tests for multiple symbols in streaming mode."""

    @pytest.mark.asyncio
    async def test_multi_symbol_ordering(self, mock_event_bus):
        """Events from multiple symbols should be globally ordered."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create data for two symbols
            for symbol in ["BTCUSDT", "ETHUSDT"]:
                symbol_dir = Path(tmpdir) / symbol
                symbol_dir.mkdir()

                with open(symbol_dir / "1m.csv", "w") as f:
                    f.write("timestamp,open,high,low,close,volume\n")
                    base = datetime(2024, 1, 1, 0, 0, 0)
                    # Offset ETHUSDT by 30 seconds
                    offset = timedelta(seconds=30) if symbol == "ETHUSDT" else timedelta(0)
                    for i in range(10):
                        ts = base + timedelta(minutes=i) + offset
                        f.write(f"{ts.strftime('%Y-%m-%d %H:%M:%S')},100,110,90,105,1000\n")

            config = Config(mode=Mode.BACKTEST, data=DataConfig(base_dir=tmpdir))
            feed = StreamingHistoricalDataFeed(mock_event_bus, config)
            await feed.start()
            await feed.subscribe("BTCUSDT", ["kline_1m"])
            await feed.subscribe("ETHUSDT", ["kline_1m"])

            events = list(feed.iter_events())

            # Should have events from both symbols
            symbols = set(e.symbol for e in events)
            assert "BTCUSDT" in symbols
            assert "ETHUSDT" in symbols

            # Should be globally ordered
            timestamps = [e.timestamp for e in events]
            assert timestamps == sorted(timestamps)

            await feed.stop()


class TestBackwardCompatibility:
    """Tests that the alias works for backward compatibility."""

    def test_alias_exists(self):
        """HistoricalDataFeed should be an alias for StreamingHistoricalDataFeed."""
        assert HistoricalDataFeed is StreamingHistoricalDataFeed

    @pytest.mark.asyncio
    async def test_api_compatibility(self, config, mock_event_bus):
        """Old API should still work."""
        feed = HistoricalDataFeed(mock_event_bus, config)
        await feed.start()
        await feed.subscribe("BTCUSDT")

        # iter_events() should work
        events = list(feed.iter_events())
        assert len(events) > 0

        # total_events property should work
        assert feed.total_events == len(events)

        await feed.stop()
