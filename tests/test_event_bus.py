"""Tests for event bus."""

import asyncio
from datetime import datetime
from decimal import Decimal

import pytest

from app.core.event_bus import EventBus
from app.core.events import KlineEvent, Interval, MarketTradeEvent, Side


@pytest.fixture
def event_bus():
    """Create event bus for testing."""
    return EventBus()


@pytest.fixture
def sample_kline():
    """Create sample kline event."""
    return KlineEvent(
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        symbol="BTCUSDT",
        interval=Interval.M1,
        open=Decimal("50000"),
        high=Decimal("50100"),
        low=Decimal("49900"),
        close=Decimal("50050"),
        volume=Decimal("100"),
    )


@pytest.fixture
def sample_trade():
    """Create sample trade event."""
    return MarketTradeEvent(
        timestamp=datetime(2024, 1, 1, 12, 0, 1),
        symbol="BTCUSDT",
        price=Decimal("50000"),
        amount=Decimal("0.1"),
        side=Side.BUY,
    )


def make_kline(timestamp: datetime) -> KlineEvent:
    """Helper to create kline events."""
    return KlineEvent(
        timestamp=timestamp,
        symbol="BTCUSDT",
        interval=Interval.M1,
        open=Decimal("50000"),
        high=Decimal("50000"),
        low=Decimal("50000"),
        close=Decimal("50000"),
        volume=Decimal("1"),
    )


@pytest.mark.asyncio
async def test_publish_subscribe(event_bus, sample_kline):
    """Test basic publish/subscribe."""
    received = []

    async def handler(event):
        received.append(event)

    event_bus.subscribe(KlineEvent, handler)
    await event_bus.publish(sample_kline)
    await event_bus.process_one()

    assert len(received) == 1
    assert received[0] == sample_kline


@pytest.mark.asyncio
async def test_global_handler(event_bus, sample_kline, sample_trade):
    """Test global handler receives all events."""
    received = []

    async def global_handler(event):
        received.append(event)

    event_bus.subscribe(None, global_handler)  # Global subscription

    await event_bus.publish(sample_kline)
    await event_bus.publish(sample_trade)
    await event_bus.process_all()

    assert len(received) == 2


@pytest.mark.asyncio
async def test_event_ordering(event_bus):
    """Test events are processed in timestamp order."""
    received = []

    async def handler(event):
        received.append(event.timestamp)

    event_bus.subscribe(KlineEvent, handler)

    # Publish out of order
    events = [
        make_kline(datetime(2024, 1, 1, 12, 0, 2)),
        make_kline(datetime(2024, 1, 1, 12, 0, 0)),
        make_kline(datetime(2024, 1, 1, 12, 0, 1)),
    ]

    for event in events:
        await event_bus.publish(event)

    await event_bus.process_all()

    # Should be in chronological order
    assert received[0] < received[1] < received[2]


@pytest.mark.asyncio
async def test_unsubscribe(event_bus, sample_kline):
    """Test unsubscribe stops handler from being called."""
    received = []

    async def handler(event):
        received.append(event)

    event_bus.subscribe(KlineEvent, handler)
    event_bus.unsubscribe(KlineEvent, handler)

    await event_bus.publish(sample_kline)
    await event_bus.process_all()

    assert len(received) == 0


@pytest.mark.asyncio
async def test_process_until(event_bus):
    """Test process_until processes events up to timestamp."""
    received = []

    async def handler(event):
        received.append(event.timestamp)

    event_bus.subscribe(KlineEvent, handler)

    events = [
        make_kline(datetime(2024, 1, 1, 12, 0, 0)),
        make_kline(datetime(2024, 1, 1, 12, 1, 0)),
        make_kline(datetime(2024, 1, 1, 12, 2, 0)),
    ]

    for event in events:
        await event_bus.publish(event)

    # Process only events up to 12:01
    count = await event_bus.process_until(datetime(2024, 1, 1, 12, 1, 0))

    assert count == 2
    assert len(received) == 2
    assert event_bus.queue_size == 1  # One event remaining
