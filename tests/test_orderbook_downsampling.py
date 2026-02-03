"""Test orderbook downsampling with LOCF (Last Observation Carried Forward)."""

import pytest
from decimal import Decimal
from datetime import datetime
from pathlib import Path
import tempfile

from app.config import Config, Mode, DataConfig, StrategyConfig, StrategyParams
from app.adapters.historical_data_feed import StreamingHistoricalDataFeed
from app.core.event_bus import EventBus
from app.core.events import OrderBookEvent


@pytest.fixture
def mock_event_bus():
    """Create mock event bus."""
    return EventBus()


@pytest.mark.asyncio
async def test_orderbook_downsampling_basic(mock_event_bus):
    """Test basic orderbook downsampling with 50ms buckets."""

    with tempfile.TemporaryDirectory() as tmpdir:
        symbol_dir = Path(tmpdir) / "BTCUSDT"
        symbol_dir.mkdir()

        # Create orderbook data with multiple events per 50ms bucket
        with open(symbol_dir / "orderbook.csv", "w") as f:
            f.write("timestamp,bid_price,bid_size,ask_price,ask_size,update_id\n")
            # Bucket 0 (0-49ms): 3 events at 0, 10, 40ms - should keep last (40ms)
            f.write("2024-01-01 00:00:00.000000,50000.0,1.0,50001.0,1.0,100\n")
            f.write("2024-01-01 00:00:00.010000,50000.5,1.1,50001.5,1.1,101\n")
            f.write("2024-01-01 00:00:00.040000,50001.0,1.2,50002.0,1.2,102\n")  # Last in bucket 0
            # Bucket 1 (50-99ms): 2 events at 50, 80ms - should keep last (80ms)
            f.write("2024-01-01 00:00:00.050000,50002.0,1.3,50003.0,1.3,103\n")
            f.write("2024-01-01 00:00:00.080000,50003.0,1.4,50004.0,1.4,104\n")  # Last in bucket 1
            # Bucket 2 (100-149ms): 1 event at 100ms - should keep it
            f.write("2024-01-01 00:00:00.100000,50004.0,1.5,50005.0,1.5,105\n")  # Last in bucket 2

        # Config with 50ms bucket
        config = Config(
            mode=Mode.BACKTEST,
            data=DataConfig(base_dir=tmpdir),
            strategy=StrategyConfig(
                params=StrategyParams(
                    orderbook_bucket_ms=50,
                    fast_orderbook_mode=False,  # Use Decimal for exact comparison
                )
            )
        )

        feed = StreamingHistoricalDataFeed(mock_event_bus, config)
        await feed.start()
        await feed.subscribe("BTCUSDT", ["orderbook"])

        events = list(feed.iter_events())
        await feed.stop()

        # Should have 3 events (last from each bucket)
        assert len(events) == 3, f"Expected 3 events, got {len(events)}"

        # Verify LOCF: should have last event from each bucket
        assert isinstance(events[0], OrderBookEvent)
        assert events[0].bid_price == Decimal("50001.0")  # Last from bucket 0 (40ms)
        assert events[0].update_id == 102

        assert isinstance(events[1], OrderBookEvent)
        assert events[1].bid_price == Decimal("50003.0")  # Last from bucket 1 (80ms)
        assert events[1].update_id == 104

        assert isinstance(events[2], OrderBookEvent)
        assert events[2].bid_price == Decimal("50004.0")  # Last from bucket 2 (100ms)
        assert events[2].update_id == 105


@pytest.mark.asyncio
async def test_orderbook_downsampling_disabled(mock_event_bus):
    """Test that downsampling is disabled when orderbook_bucket_ms=0."""

    with tempfile.TemporaryDirectory() as tmpdir:
        symbol_dir = Path(tmpdir) / "BTCUSDT"
        symbol_dir.mkdir()

        # Create orderbook data
        with open(symbol_dir / "orderbook.csv", "w") as f:
            f.write("timestamp,bid_price,bid_size,ask_price,ask_size,update_id\n")
            f.write("2024-01-01 00:00:00.000000,50000.0,1.0,50001.0,1.0,100\n")
            f.write("2024-01-01 00:00:00.010000,50000.5,1.1,50001.5,1.1,101\n")
            f.write("2024-01-01 00:00:00.040000,50001.0,1.2,50002.0,1.2,102\n")

        # Config with disabled downsampling
        config = Config(
            mode=Mode.BACKTEST,
            data=DataConfig(base_dir=tmpdir),
            strategy=StrategyConfig(
                params=StrategyParams(
                    orderbook_bucket_ms=0,  # Disabled
                    fast_orderbook_mode=False,
                )
            )
        )

        feed = StreamingHistoricalDataFeed(mock_event_bus, config)
        await feed.start()
        await feed.subscribe("BTCUSDT", ["orderbook"])

        events = list(feed.iter_events())
        await feed.stop()

        # Should have all 3 events (no downsampling)
        assert len(events) == 3


@pytest.mark.asyncio
async def test_orderbook_downsampling_with_dedupe(mock_event_bus):
    """Test that downsampling works together with dedupe."""

    with tempfile.TemporaryDirectory() as tmpdir:
        symbol_dir = Path(tmpdir) / "BTCUSDT"
        symbol_dir.mkdir()

        # Create orderbook data with duplicates
        with open(symbol_dir / "orderbook.csv", "w") as f:
            f.write("timestamp,bid_price,bid_size,ask_price,ask_size,update_id\n")
            # Bucket 0: 3 events, 2 duplicates (same update_id)
            f.write("2024-01-01 00:00:00.000000,50000.0,1.0,50001.0,1.0,100\n")
            f.write("2024-01-01 00:00:00.010000,50000.5,1.1,50001.5,1.1,100\n")  # Duplicate update_id
            f.write("2024-01-01 00:00:00.040000,50001.0,1.2,50002.0,1.2,101\n")  # Last non-duplicate
            # Bucket 1: 2 events
            f.write("2024-01-01 00:00:00.050000,50002.0,1.3,50003.0,1.3,102\n")
            f.write("2024-01-01 00:00:00.080000,50003.0,1.4,50004.0,1.4,103\n")  # Last in bucket

        config = Config(
            mode=Mode.BACKTEST,
            data=DataConfig(base_dir=tmpdir),
            strategy=StrategyConfig(
                params=StrategyParams(
                    orderbook_bucket_ms=50,
                    fast_orderbook_mode=False,
                )
            )
        )

        feed = StreamingHistoricalDataFeed(mock_event_bus, config, dedupe_orderbook=True)
        await feed.start()
        await feed.subscribe("BTCUSDT", ["orderbook"])

        events = list(feed.iter_events())
        await feed.stop()

        # Should have 2 events (dedupe filters first, then LOCF)
        assert len(events) == 2
        assert events[0].update_id == 101  # Last non-duplicate from bucket 0
        assert events[1].update_id == 103  # Last from bucket 1


@pytest.mark.asyncio
async def test_orderbook_downsampling_edge_case_single_bucket(mock_event_bus):
    """Test edge case: all events in single bucket."""

    with tempfile.TemporaryDirectory() as tmpdir:
        symbol_dir = Path(tmpdir) / "BTCUSDT"
        symbol_dir.mkdir()

        # All events within 50ms
        with open(symbol_dir / "orderbook.csv", "w") as f:
            f.write("timestamp,bid_price,bid_size,ask_price,ask_size,update_id\n")
            f.write("2024-01-01 00:00:00.000000,50000.0,1.0,50001.0,1.0,100\n")
            f.write("2024-01-01 00:00:00.020000,50000.5,1.1,50001.5,1.1,101\n")
            f.write("2024-01-01 00:00:00.040000,50001.0,1.2,50002.0,1.2,102\n")  # Last

        config = Config(
            mode=Mode.BACKTEST,
            data=DataConfig(base_dir=tmpdir),
            strategy=StrategyConfig(
                params=StrategyParams(
                    orderbook_bucket_ms=50,
                    fast_orderbook_mode=False,
                )
            )
        )

        feed = StreamingHistoricalDataFeed(mock_event_bus, config)
        await feed.start()
        await feed.subscribe("BTCUSDT", ["orderbook"])

        events = list(feed.iter_events())
        await feed.stop()

        # Should have 1 event (last from single bucket)
        assert len(events) == 1
        assert events[0].update_id == 102


@pytest.mark.asyncio
async def test_kline_not_affected_by_downsampling(mock_event_bus):
    """Test that kline events are NOT affected by orderbook downsampling."""

    with tempfile.TemporaryDirectory() as tmpdir:
        symbol_dir = Path(tmpdir) / "BTCUSDT"
        symbol_dir.mkdir()

        # Create kline data
        with open(symbol_dir / "1m.csv", "w") as f:
            f.write("timestamp,open,high,low,close,volume\n")
            f.write("2024-01-01 00:00:00,50000,50100,49900,50050,100.5\n")
            f.write("2024-01-01 00:01:00,50050,50150,49950,50100,110.2\n")
            f.write("2024-01-01 00:02:00,50100,50200,50000,50150,120.8\n")

        config = Config(
            mode=Mode.BACKTEST,
            data=DataConfig(base_dir=tmpdir),
            strategy=StrategyConfig(
                params=StrategyParams(
                    orderbook_bucket_ms=50,  # Should NOT affect klines
                    fast_orderbook_mode=False,
                )
            )
        )

        feed = StreamingHistoricalDataFeed(mock_event_bus, config)
        await feed.start()
        await feed.subscribe("BTCUSDT", ["kline_1m"])

        events = list(feed.iter_events())
        await feed.stop()

        # Should have all 3 kline events (downsampling only affects orderbook)
        assert len(events) == 3
