"""Tests for simulated exchange adapter."""

from datetime import datetime
from decimal import Decimal

import pytest

from app.adapters.exchange_adapter import OrderRequest
from app.adapters.simulated_adapter import SimulatedExchangeAdapter
from app.config import CostsConfig
from app.core.event_bus import EventBus
from app.core.events import OrderBookEvent, OrderStatus, OrderType, Side


@pytest.fixture
def event_bus():
    """Create event bus for testing."""
    return EventBus()


@pytest.fixture
def costs():
    """Create costs config for testing."""
    return CostsConfig(
        fees_bps=Decimal("6"),
        slippage_bps=Decimal("2"),
    )


@pytest.fixture
def exchange(event_bus, costs):
    """Create simulated exchange for testing."""
    return SimulatedExchangeAdapter(
        event_bus=event_bus,
        costs=costs,
        initial_balance=Decimal("10000"),
        leverage=10,
    )


def make_orderbook(
    timestamp: datetime = datetime(2024, 1, 1, 12, 0, 0),
    bid_price: Decimal = Decimal("50000"),
    ask_price: Decimal = Decimal("50010"),
) -> OrderBookEvent:
    """Helper to create orderbook events."""
    return OrderBookEvent(
        timestamp=timestamp,
        symbol="BTCUSDT",
        bid_price=bid_price,
        bid_size=Decimal("10"),
        ask_price=ask_price,
        ask_size=Decimal("10"),
    )


@pytest.fixture
def orderbook_event():
    """Create sample orderbook event."""
    return make_orderbook()


@pytest.mark.asyncio
async def test_market_buy_fill(exchange, orderbook_event):
    """Test market buy order fills at ask + slippage."""
    await exchange.start()
    await exchange.process_orderbook(orderbook_event)

    request = OrderRequest(
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        qty=Decimal("0.1"),
    )

    response = await exchange.place_order(request)

    assert response.success
    assert response.status == OrderStatus.FILLED

    # Check position was opened
    position = await exchange.get_position("BTCUSDT")
    assert position is not None
    assert position.side == Side.BUY
    assert position.size == Decimal("0.1")

    # Fill price should be ask + slippage
    expected_fill = orderbook_event.ask_price * (1 + exchange._costs.slippage_pct)
    assert position.entry_price == expected_fill

    await exchange.stop()


@pytest.mark.asyncio
async def test_market_sell_fill(exchange, orderbook_event):
    """Test market sell order fills at bid - slippage."""
    await exchange.start()
    await exchange.process_orderbook(orderbook_event)

    request = OrderRequest(
        symbol="BTCUSDT",
        side=Side.SELL,
        order_type=OrderType.MARKET,
        qty=Decimal("0.1"),
    )

    response = await exchange.place_order(request)

    assert response.success
    position = await exchange.get_position("BTCUSDT")
    assert position is not None
    assert position.side == Side.SELL

    # Fill price should be bid - slippage
    expected_fill = orderbook_event.bid_price * (1 - exchange._costs.slippage_pct)
    assert position.entry_price == expected_fill

    await exchange.stop()


@pytest.mark.asyncio
async def test_fees_deducted(exchange, orderbook_event):
    """Test fees are deducted from balance."""
    await exchange.start()
    initial_balance = exchange.current_balance
    await exchange.process_orderbook(orderbook_event)

    request = OrderRequest(
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        qty=Decimal("0.1"),
    )

    await exchange.place_order(request)

    # Balance should be reduced by fees
    expected_fee = orderbook_event.ask_price * (1 + exchange._costs.slippage_pct) * Decimal("0.1") * exchange._costs.fees_pct
    assert exchange.current_balance < initial_balance
    assert exchange.current_balance == initial_balance - expected_fee

    await exchange.stop()


@pytest.mark.asyncio
async def test_limit_order_pending(exchange, orderbook_event):
    """Test limit order is added to pending orders."""
    await exchange.start()
    await exchange.process_orderbook(orderbook_event)

    # Place limit buy below current price
    request = OrderRequest(
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal("0.1"),
        price=Decimal("49900"),  # Below bid
    )

    response = await exchange.place_order(request)

    assert response.success
    assert response.status == OrderStatus.NEW

    # Should be in pending orders
    orders = await exchange.get_open_orders("BTCUSDT")
    assert len(orders) == 1
    assert orders[0].price == Decimal("49900")

    await exchange.stop()


@pytest.mark.asyncio
async def test_limit_order_fills_when_price_crosses(exchange):
    """Test limit order fills when price crosses level."""
    await exchange.start()

    # Set initial price
    event1 = make_orderbook(
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        bid_price=Decimal("50000"),
        ask_price=Decimal("50010"),
    )
    await exchange.process_orderbook(event1)

    # Place limit buy at 49900
    request = OrderRequest(
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal("0.1"),
        price=Decimal("49900"),
    )
    await exchange.place_order(request)

    # Price drops to cross limit level
    event2 = make_orderbook(
        timestamp=datetime(2024, 1, 1, 12, 1, 0),
        bid_price=Decimal("49850"),
        ask_price=Decimal("49860"),  # Below limit price
    )
    await exchange.process_orderbook(event2)

    # Order should be filled
    orders = await exchange.get_open_orders("BTCUSDT")
    assert len(orders) == 0

    position = await exchange.get_position("BTCUSDT")
    assert position is not None
    assert position.side == Side.BUY

    await exchange.stop()


@pytest.mark.asyncio
async def test_sl_triggers(exchange, orderbook_event):
    """Test stop loss triggers when price hits SL level."""
    await exchange.start()
    await exchange.process_orderbook(orderbook_event)

    # Open long position with SL
    request = OrderRequest(
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        qty=Decimal("0.1"),
        sl_price=Decimal("49500"),
    )
    await exchange.place_order(request)

    # Price drops to SL level
    sl_event = make_orderbook(
        timestamp=datetime(2024, 1, 1, 12, 1, 0),
        bid_price=Decimal("49400"),  # Below SL
        ask_price=Decimal("49410"),
    )
    await exchange.process_orderbook(sl_event)

    # Position should be closed
    position = await exchange.get_position("BTCUSDT")
    assert position is None

    await exchange.stop()


@pytest.mark.asyncio
async def test_tp_triggers(exchange, orderbook_event):
    """Test take profit triggers when price hits TP level."""
    await exchange.start()
    await exchange.process_orderbook(orderbook_event)

    # Open long position with TP
    request = OrderRequest(
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        qty=Decimal("0.1"),
        tp_price=Decimal("51000"),
    )
    await exchange.place_order(request)

    # Price rises to TP level
    tp_event = make_orderbook(
        timestamp=datetime(2024, 1, 1, 12, 1, 0),
        bid_price=Decimal("51100"),  # Above TP
        ask_price=Decimal("51110"),
    )
    await exchange.process_orderbook(tp_event)

    # Position should be closed
    position = await exchange.get_position("BTCUSDT")
    assert position is None

    await exchange.stop()


@pytest.mark.asyncio
async def test_pnl_calculation(exchange, orderbook_event):
    """Test P&L is calculated correctly on close."""
    await exchange.start()
    await exchange.process_orderbook(orderbook_event)

    initial_balance = exchange.current_balance

    # Open position
    request = OrderRequest(
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        qty=Decimal("0.1"),
    )
    await exchange.place_order(request)

    position = await exchange.get_position("BTCUSDT")
    entry_price = position.entry_price

    # Price rises
    profit_event = make_orderbook(
        timestamp=datetime(2024, 1, 1, 12, 1, 0),
        bid_price=Decimal("51000"),
        ask_price=Decimal("51010"),
    )
    await exchange.process_orderbook(profit_event)

    # Close position
    close_request = OrderRequest(
        symbol="BTCUSDT",
        side=Side.SELL,
        order_type=OrderType.MARKET,
        qty=Decimal("0.1"),
        reduce_only=True,
    )
    await exchange.place_order(close_request)

    # Position should be closed
    position = await exchange.get_position("BTCUSDT")
    assert position is None

    # Check profit was realized (balance increased after fees)
    final_balance = exchange.current_balance

    # Gross profit: (exit_price - entry_price) * size
    exit_price = profit_event.bid_price * (1 - exchange._costs.slippage_pct)
    gross_pnl = (exit_price - entry_price) * Decimal("0.1")

    # Fees on both sides
    entry_fee = entry_price * Decimal("0.1") * exchange._costs.fees_pct
    exit_fee = exit_price * Decimal("0.1") * exchange._costs.fees_pct

    expected_final = initial_balance + gross_pnl - entry_fee - exit_fee

    assert abs(final_balance - expected_final) < Decimal("0.01")

    await exchange.stop()
