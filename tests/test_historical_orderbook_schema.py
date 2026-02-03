"""Tests for extended orderbook schema detection in HistoricalDataFeed.

Verifies that:
1. Optional orderbook columns (update_id, seq, cts, etc.) are detected
2. exchange_ts (cts) is used as primary timestamp when available
3. Dedupe mode skips consecutive rows with same update_id or bid/ask
"""

import pytest
import pandas as pd
import tempfile
from datetime import datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from app.adapters.historical_data_feed import (
    CSVSchemaDetector,
    HistoricalDataFeed,
    StreamingHistoricalDataFeed,
)
from app.core.events import OrderBookEvent
from app.config import Config, DataConfig, Mode


class TestCSVSchemaDetector:
    """Tests for CSVSchemaDetector optional orderbook columns."""

    def test_detect_update_id_column(self):
        """Should detect update_id column variants."""
        df = pd.DataFrame({
            "timestamp": ["2024-01-01 00:00:00"],
            "bid_price": [50000],
            "bid_size": [1.0],
            "ask_price": [50001],
            "ask_size": [1.0],
            "update_id": [100],
        })
        schema = CSVSchemaDetector.detect_schema(df)
        assert schema.get("update_id") == "update_id"

    def test_detect_u_as_update_id(self):
        """Should detect 'u' column as update_id."""
        df = pd.DataFrame({
            "timestamp": ["2024-01-01 00:00:00"],
            "bid_price": [50000],
            "bid_size": [1.0],
            "ask_price": [50001],
            "ask_size": [1.0],
            "u": [100],
        })
        schema = CSVSchemaDetector.detect_schema(df)
        assert schema.get("update_id") == "u"

    def test_detect_seq_column(self):
        """Should detect seq column."""
        df = pd.DataFrame({
            "timestamp": ["2024-01-01 00:00:00"],
            "bid_price": [50000],
            "bid_size": [1.0],
            "ask_price": [50001],
            "ask_size": [1.0],
            "seq": [12345],
        })
        schema = CSVSchemaDetector.detect_schema(df)
        assert schema.get("seq") == "seq"

    def test_detect_exchange_ts_variants(self):
        """Should detect exchange_ts and cts columns."""
        # Test 'exchange_ts'
        df1 = pd.DataFrame({
            "timestamp": ["2024-01-01 00:00:00"],
            "bid_price": [50000],
            "bid_size": [1.0],
            "ask_price": [50001],
            "ask_size": [1.0],
            "exchange_ts": ["2024-01-01 00:00:00.100"],
        })
        schema1 = CSVSchemaDetector.detect_schema(df1)
        assert schema1.get("exchange_ts") == "exchange_ts"

        # Test 'cts'
        df2 = pd.DataFrame({
            "timestamp": ["2024-01-01 00:00:00"],
            "bid_price": [50000],
            "bid_size": [1.0],
            "ask_price": [50001],
            "ask_size": [1.0],
            "cts": ["2024-01-01 00:00:00.100"],
        })
        schema2 = CSVSchemaDetector.detect_schema(df2)
        assert schema2.get("exchange_ts") == "cts"

    def test_detect_system_ts_variants(self):
        """Should detect system_ts and ts columns."""
        df = pd.DataFrame({
            "timestamp": ["2024-01-01 00:00:00"],
            "bid_price": [50000],
            "bid_size": [1.0],
            "ask_price": [50001],
            "ask_size": [1.0],
            "system_ts": ["2024-01-01 00:00:00.200"],
        })
        schema = CSVSchemaDetector.detect_schema(df)
        assert schema.get("system_ts") == "system_ts"

    def test_detect_local_ts(self):
        """Should detect local_ts column."""
        df = pd.DataFrame({
            "timestamp": ["2024-01-01 00:00:00"],
            "bid_price": [50000],
            "bid_size": [1.0],
            "ask_price": [50001],
            "ask_size": [1.0],
            "local_ts": ["2024-01-01 00:00:00.300"],
        })
        schema = CSVSchemaDetector.detect_schema(df)
        assert schema.get("local_ts") == "local_ts"

    def test_detect_all_optional_columns(self):
        """Should detect all optional orderbook columns together."""
        df = pd.DataFrame({
            "timestamp": ["2024-01-01 00:00:00"],
            "bid_price": [50000],
            "bid_size": [1.0],
            "ask_price": [50001],
            "ask_size": [1.0],
            "update_id": [100],
            "seq": [12345],
            "exchange_ts": ["2024-01-01 00:00:00.100"],
            "system_ts": ["2024-01-01 00:00:00.200"],
            "local_ts": ["2024-01-01 00:00:00.300"],
        })
        schema = CSVSchemaDetector.detect_schema(df)
        assert schema.get("update_id") == "update_id"
        assert schema.get("seq") == "seq"
        assert schema.get("exchange_ts") == "exchange_ts"
        assert schema.get("system_ts") == "system_ts"
        assert schema.get("local_ts") == "local_ts"
        assert schema.get("_type") == "orderbook"


class TestHistoricalDataFeedOrderbook:
    """Tests for HistoricalDataFeed orderbook parsing with streaming."""

    @pytest.fixture
    def mock_event_bus(self):
        bus = MagicMock()
        bus.publish = AsyncMock()
        return bus

    @pytest.mark.asyncio
    async def test_parse_orderbook_with_extended_fields(self, mock_event_bus):
        """Should parse orderbook with extended fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            symbol_dir = Path(tmpdir) / "BTCUSDT"
            symbol_dir.mkdir()

            # Create CSV with extended fields
            with open(symbol_dir / "orderbook.csv", "w") as f:
                f.write("timestamp,bid_price,bid_size,ask_price,ask_size,update_id,seq,exchange_ts,system_ts,local_ts\n")
                f.write("2024-01-01 00:00:00.000000,50000.0,1.0,50001.0,1.0,100,12345,2024-01-01 00:00:00.100000,2024-01-01 00:00:00.200000,2024-01-01 00:00:00.300000\n")

            config = Config(mode=Mode.BACKTEST, data=DataConfig(base_dir=tmpdir))
            feed = StreamingHistoricalDataFeed(mock_event_bus, config)
            await feed.start()
            await feed.subscribe("BTCUSDT", ["orderbook"])

            events = list(feed.iter_events())

            assert len(events) == 1
            event = events[0]
            assert isinstance(event, OrderBookEvent)
            assert event.update_id == 100
            assert event.seq == 12345
            assert event.exchange_ts is not None
            assert event.system_ts is not None
            assert event.local_ts is not None

            await feed.stop()

    @pytest.mark.asyncio
    async def test_exchange_ts_used_as_primary_timestamp(self, mock_event_bus):
        """exchange_ts (cts) should be used as primary timestamp."""
        with tempfile.TemporaryDirectory() as tmpdir:
            symbol_dir = Path(tmpdir) / "BTCUSDT"
            symbol_dir.mkdir()

            # Create CSV with exchange_ts different from timestamp
            with open(symbol_dir / "orderbook.csv", "w") as f:
                f.write("timestamp,bid_price,bid_size,ask_price,ask_size,exchange_ts\n")
                f.write("2024-01-01 00:00:00.000000,50000.0,1.0,50001.0,1.0,2024-01-01 00:00:01.000000\n")

            config = Config(mode=Mode.BACKTEST, data=DataConfig(base_dir=tmpdir))
            feed = StreamingHistoricalDataFeed(mock_event_bus, config)
            await feed.start()
            await feed.subscribe("BTCUSDT", ["orderbook"])

            events = list(feed.iter_events())

            assert len(events) == 1
            event = events[0]
            # Should use exchange_ts, not original timestamp
            assert event.timestamp.second == 1  # From exchange_ts
            assert event.exchange_ts.second == 1

            await feed.stop()

    @pytest.mark.asyncio
    async def test_fallback_to_original_timestamp_when_no_cts(self, mock_event_bus):
        """Should use original timestamp when exchange_ts not available."""
        with tempfile.TemporaryDirectory() as tmpdir:
            symbol_dir = Path(tmpdir) / "BTCUSDT"
            symbol_dir.mkdir()

            # Create CSV without exchange_ts
            with open(symbol_dir / "orderbook.csv", "w") as f:
                f.write("timestamp,bid_price,bid_size,ask_price,ask_size\n")
                f.write("2024-01-01 00:00:05.000000,50000.0,1.0,50001.0,1.0\n")

            config = Config(mode=Mode.BACKTEST, data=DataConfig(base_dir=tmpdir))
            feed = StreamingHistoricalDataFeed(mock_event_bus, config)
            await feed.start()
            await feed.subscribe("BTCUSDT", ["orderbook"])

            events = list(feed.iter_events())

            assert len(events) == 1
            event = events[0]
            assert event.timestamp.second == 5
            assert event.exchange_ts is None

            await feed.stop()


class TestOrderbookDedupe:
    """Tests for orderbook deduplication during streaming."""

    @pytest.fixture
    def mock_event_bus(self):
        bus = MagicMock()
        bus.publish = AsyncMock()
        return bus

    @pytest.mark.asyncio
    async def test_dedupe_by_update_id(self, mock_event_bus):
        """Should skip consecutive rows with same update_id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            symbol_dir = Path(tmpdir) / "BTCUSDT"
            symbol_dir.mkdir()

            # Create CSV with duplicate update_ids
            with open(symbol_dir / "orderbook.csv", "w") as f:
                f.write("timestamp,bid_price,bid_size,ask_price,ask_size,update_id\n")
                f.write("2024-01-01 00:00:00,50000,1.0,50001,1.0,100\n")
                f.write("2024-01-01 00:00:01,50000,1.0,50001,1.0,100\n")  # Duplicate
                f.write("2024-01-01 00:00:02,50001,1.0,50002,1.0,101\n")
                f.write("2024-01-01 00:00:03,50001,1.0,50002,1.0,101\n")  # Duplicate

            config = Config(mode=Mode.BACKTEST, data=DataConfig(base_dir=tmpdir))
            feed = StreamingHistoricalDataFeed(mock_event_bus, config, dedupe_orderbook=True)
            await feed.start()
            await feed.subscribe("BTCUSDT", ["orderbook"])

            events = list(feed.iter_events())

            # Should only get 2 events (100, 101), not 4
            assert len(events) == 2
            assert events[0].update_id == 100
            assert events[1].update_id == 101

            await feed.stop()

    @pytest.mark.asyncio
    async def test_dedupe_by_bid_ask(self, mock_event_bus):
        """Should skip consecutive rows with same bid/ask."""
        with tempfile.TemporaryDirectory() as tmpdir:
            symbol_dir = Path(tmpdir) / "BTCUSDT"
            symbol_dir.mkdir()

            # Create CSV without update_id (dedupe by bid/ask)
            with open(symbol_dir / "orderbook.csv", "w") as f:
                f.write("timestamp,bid_price,bid_size,ask_price,ask_size\n")
                f.write("2024-01-01 00:00:00,50000,1.0,50001,1.0\n")
                f.write("2024-01-01 00:00:01,50000,1.0,50001,1.0\n")  # Same bid/ask
                f.write("2024-01-01 00:00:02,50001,1.0,50002,1.0\n")  # Different

            config = Config(mode=Mode.BACKTEST, data=DataConfig(base_dir=tmpdir))
            feed = StreamingHistoricalDataFeed(mock_event_bus, config, dedupe_orderbook=True)
            await feed.start()
            await feed.subscribe("BTCUSDT", ["orderbook"])

            events = list(feed.iter_events())

            # Should only get 2 events (different bid/ask)
            assert len(events) == 2
            assert events[0].bid_price == Decimal("50000")
            assert events[1].bid_price == Decimal("50001")

            await feed.stop()

    @pytest.mark.asyncio
    async def test_no_dedupe_when_disabled(self, mock_event_bus):
        """Should not dedupe when dedupe_orderbook=False."""
        with tempfile.TemporaryDirectory() as tmpdir:
            symbol_dir = Path(tmpdir) / "BTCUSDT"
            symbol_dir.mkdir()

            # Create CSV with duplicates
            with open(symbol_dir / "orderbook.csv", "w") as f:
                f.write("timestamp,bid_price,bid_size,ask_price,ask_size,update_id\n")
                f.write("2024-01-01 00:00:00,50000,1.0,50001,1.0,100\n")
                f.write("2024-01-01 00:00:01,50000,1.0,50001,1.0,100\n")  # Same

            config = Config(mode=Mode.BACKTEST, data=DataConfig(base_dir=tmpdir))
            feed = StreamingHistoricalDataFeed(mock_event_bus, config, dedupe_orderbook=False)
            await feed.start()
            await feed.subscribe("BTCUSDT", ["orderbook"])

            events = list(feed.iter_events())

            # Should get all 2 events (no dedupe)
            assert len(events) == 2

            await feed.stop()

    @pytest.mark.asyncio
    async def test_dedupe_allows_different_updates(self, mock_event_bus):
        """Dedupe should not skip rows with different update_id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            symbol_dir = Path(tmpdir) / "BTCUSDT"
            symbol_dir.mkdir()

            # Create CSV with same bid/ask but different update_ids
            with open(symbol_dir / "orderbook.csv", "w") as f:
                f.write("timestamp,bid_price,bid_size,ask_price,ask_size,update_id\n")
                f.write("2024-01-01 00:00:00,50000,1.0,50001,1.0,100\n")
                f.write("2024-01-01 00:00:01,50000,1.0,50001,1.0,101\n")  # Different ID
                f.write("2024-01-01 00:00:02,50000,1.0,50001,1.0,102\n")  # Different ID

            config = Config(mode=Mode.BACKTEST, data=DataConfig(base_dir=tmpdir))
            feed = StreamingHistoricalDataFeed(mock_event_bus, config, dedupe_orderbook=True)
            await feed.start()
            await feed.subscribe("BTCUSDT", ["orderbook"])

            events = list(feed.iter_events())

            # Should get all 3 events (different update_ids take precedence)
            assert len(events) == 3

            await feed.stop()


class TestBackwardCompatibility:
    """Tests for backward compatibility with old CSV format."""

    @pytest.fixture
    def mock_event_bus(self):
        bus = MagicMock()
        bus.publish = AsyncMock()
        return bus

    @pytest.mark.asyncio
    async def test_old_csv_format_still_works(self, mock_event_bus):
        """Old CSV format without extended columns should still work."""
        with tempfile.TemporaryDirectory() as tmpdir:
            symbol_dir = Path(tmpdir) / "BTCUSDT"
            symbol_dir.mkdir()

            # Old format: only basic columns with mid_price
            with open(symbol_dir / "orderbook.csv", "w") as f:
                f.write("timestamp,bid_price,bid_size,ask_price,ask_size,mid_price\n")
                f.write("2024-01-01 00:00:00,50000,1.0,50001,1.0,50000.5\n")

            config = Config(mode=Mode.BACKTEST, data=DataConfig(base_dir=tmpdir))
            feed = StreamingHistoricalDataFeed(mock_event_bus, config)
            await feed.start()
            await feed.subscribe("BTCUSDT", ["orderbook"])

            events = list(feed.iter_events())

            assert len(events) == 1
            event = events[0]
            assert isinstance(event, OrderBookEvent)
            assert event.bid_price == Decimal("50000")
            assert event.update_id is None
            assert event.seq is None
            assert event.exchange_ts is None

            await feed.stop()
