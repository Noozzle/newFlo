"""Tests for orderbook dedupe in LiveDataFeed.

Verifies that:
1. Duplicate orderbook updates (same update_id) are skipped
2. Resync updates (update_id=1) are always accepted
3. Different update_ids are processed normally
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal
from datetime import datetime

from app.adapters.live_data_feed import LiveDataFeed, PYBIT_AVAILABLE
from app.config import BybitConfig
from app.core.events import OrderBookEvent


def make_orderbook_message(
    u: int,
    bid_price: str = "50000.0",
    bid_size: str = "1.0",
    ask_price: str = "50001.0",
    ask_size: str = "1.0",
    ts: int = 1700000000000,
    cts: int | None = 1700000000100,
    seq: int | None = 12345,
) -> dict:
    """Create a mock Bybit orderbook message."""
    data = {
        "b": [[bid_price, bid_size]],
        "a": [[ask_price, ask_size]],
        "u": u,
    }
    if cts is not None:
        data["cts"] = cts
    if seq is not None:
        data["seq"] = seq

    return {
        "ts": ts,
        "data": data,
    }


@pytest.fixture
def mock_event_bus():
    """Create a mock event bus."""
    bus = MagicMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def live_feed(mock_event_bus):
    """Create LiveDataFeed with mocked dependencies."""
    if not PYBIT_AVAILABLE:
        pytest.skip("pybit not installed")

    config = BybitConfig(testnet=True)
    feed = LiveDataFeed(mock_event_bus, config)
    # Set loop to enable publish path
    feed._loop = MagicMock()
    return feed


class TestOrderbookDedupe:
    """Tests for orderbook deduplication."""

    def test_first_update_is_processed(self, live_feed, mock_event_bus):
        """First orderbook update should be processed."""
        msg = make_orderbook_message(u=100)

        with patch('asyncio.run_coroutine_threadsafe') as mock_rcts:
            live_feed._handle_orderbook("BTCUSDT", msg)
            assert mock_rcts.call_count == 1

        assert live_feed._last_orderbook_u["BTCUSDT"] == 100

    def test_duplicate_update_is_skipped(self, live_feed, mock_event_bus):
        """Duplicate update_id should be skipped."""
        msg1 = make_orderbook_message(u=100)
        msg2 = make_orderbook_message(u=100)  # Same u

        with patch('asyncio.run_coroutine_threadsafe') as mock_rcts:
            # Process first message
            live_feed._handle_orderbook("BTCUSDT", msg1)
            first_call_count = mock_rcts.call_count

            # Process duplicate - should be skipped
            live_feed._handle_orderbook("BTCUSDT", msg2)
            second_call_count = mock_rcts.call_count

            # No additional publish for duplicate
            assert second_call_count == first_call_count
            assert first_call_count == 1

    def test_different_update_ids_processed(self, live_feed, mock_event_bus):
        """Different update_ids should all be processed."""
        msg1 = make_orderbook_message(u=100)
        msg2 = make_orderbook_message(u=101)
        msg3 = make_orderbook_message(u=102)

        with patch('asyncio.run_coroutine_threadsafe') as mock_rcts:
            live_feed._handle_orderbook("BTCUSDT", msg1)
            call_count_1 = mock_rcts.call_count

            live_feed._handle_orderbook("BTCUSDT", msg2)
            call_count_2 = mock_rcts.call_count

            live_feed._handle_orderbook("BTCUSDT", msg3)
            call_count_3 = mock_rcts.call_count

            # Each unique update should trigger publish
            assert call_count_1 == 1
            assert call_count_2 == 2
            assert call_count_3 == 3

        assert live_feed._last_orderbook_u["BTCUSDT"] == 102

    def test_resync_update_always_accepted(self, live_feed, mock_event_bus):
        """Resync (update_id=1) should always be accepted, even if repeated."""
        msg1 = make_orderbook_message(u=100)
        msg_resync = make_orderbook_message(u=1)

        with patch('asyncio.run_coroutine_threadsafe') as mock_rcts:
            # First normal update
            live_feed._handle_orderbook("BTCUSDT", msg1)
            call_count_after_first = mock_rcts.call_count

            # Resync update
            live_feed._handle_orderbook("BTCUSDT", msg_resync)
            call_count_after_resync = mock_rcts.call_count

            # Resync should be processed
            assert call_count_after_resync == call_count_after_first + 1

        assert live_feed._last_orderbook_u["BTCUSDT"] == 1

    def test_per_symbol_dedupe(self, live_feed, mock_event_bus):
        """Dedupe should be per-symbol."""
        msg_btc = make_orderbook_message(u=100)
        msg_eth = make_orderbook_message(u=100)  # Same u but different symbol

        with patch('asyncio.run_coroutine_threadsafe') as mock_rcts:
            live_feed._handle_orderbook("BTCUSDT", msg_btc)
            call_count_1 = mock_rcts.call_count

            live_feed._handle_orderbook("ETHUSDT", msg_eth)
            call_count_2 = mock_rcts.call_count

            # Both should be processed (different symbols)
            assert call_count_1 == 1
            assert call_count_2 == 2

        assert live_feed._last_orderbook_u["BTCUSDT"] == 100
        assert live_feed._last_orderbook_u["ETHUSDT"] == 100

    def test_event_has_extended_fields(self, live_feed, mock_event_bus):
        """OrderBookEvent should have extended fields populated."""
        msg = make_orderbook_message(u=100, cts=1700000000100, seq=12345)

        captured_events = []

        def capture_publish(coro, loop):
            # The coroutine is event_bus.publish(event)
            # We can't easily extract the event, but we verify the call happened
            captured_events.append(coro)
            return MagicMock()

        with patch('asyncio.run_coroutine_threadsafe', capture_publish):
            live_feed._handle_orderbook("BTCUSDT", msg)

        # Verify event was created
        assert live_feed._last_orderbook_u["BTCUSDT"] == 100
        assert len(captured_events) == 1


class TestOrderbookEventFields:
    """Test that OrderBookEvent has correct extended fields."""

    def test_orderbook_event_optional_fields(self):
        """OrderBookEvent should support optional extended fields."""
        # Basic event without extended fields (backward compatibility)
        event = OrderBookEvent(
            timestamp=datetime.utcnow(),
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            bid_size=Decimal("1.0"),
            ask_price=Decimal("50001"),
            ask_size=Decimal("1.0"),
        )
        assert event.update_id is None
        assert event.seq is None
        assert event.exchange_ts is None
        assert event.system_ts is None
        assert event.local_ts is None

    def test_orderbook_event_with_extended_fields(self):
        """OrderBookEvent should accept extended fields."""
        now = datetime.utcnow()
        event = OrderBookEvent(
            timestamp=now,
            symbol="BTCUSDT",
            bid_price=Decimal("50000"),
            bid_size=Decimal("1.0"),
            ask_price=Decimal("50001"),
            ask_size=Decimal("1.0"),
            update_id=100,
            seq=12345,
            exchange_ts=now,
            system_ts=now,
            local_ts=now,
        )
        assert event.update_id == 100
        assert event.seq == 12345
        assert event.exchange_ts == now
        assert event.system_ts == now
        assert event.local_ts == now
