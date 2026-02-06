"""Tests for LIVE/BACKTEST kline consistency in OrderflowStrategy.

Verifies that:
1. Open candles (is_closed=False) do NOT affect ATR, trend, or reset volumes
2. Only closed candles (is_closed=True) update indicators and reset trade flow
3. Kline volume estimation does NOT overwrite real trade volumes
"""

import pytest
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.events import Interval, KlineEvent, MarketTradeEvent, Side
from app.strategies.orderflow_1m import OrderflowStrategy, SymbolState


def make_kline(
    symbol: str = "BTCUSDT",
    interval: Interval = Interval.M1,
    is_closed: bool = True,
    open_: Decimal = Decimal("100"),
    high: Decimal = Decimal("105"),
    low: Decimal = Decimal("95"),
    close: Decimal = Decimal("102"),
    volume: Decimal = Decimal("1000"),
) -> KlineEvent:
    """Helper to create KlineEvent."""
    return KlineEvent(
        timestamp=datetime.utcnow(),
        symbol=symbol,
        interval=interval,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        is_closed=is_closed,
    )


@pytest.mark.asyncio
async def test_open_candle_does_not_change_atr():
    """Open candle (is_closed=False) should NOT update ATR."""
    strategy = OrderflowStrategy()

    # Feed some closed candles first to establish ATR
    for i in range(15):
        event = make_kline(
            is_closed=True,
            high=Decimal(100 + i),
            low=Decimal(90 + i),
            close=Decimal(95 + i),
        )
        await strategy.on_kline(event)

    state = strategy._get_state("BTCUSDT")
    atr_before = state.atr
    atr_values_len_before = len(state.atr_values)

    # Now feed an open candle with extreme values
    open_event = make_kline(
        is_closed=False,
        high=Decimal("500"),  # Extreme value
        low=Decimal("50"),
        close=Decimal("200"),
    )
    await strategy.on_kline(open_event)

    # ATR should NOT change
    assert state.atr == atr_before, "ATR changed on open candle!"
    assert len(state.atr_values) == atr_values_len_before, "ATR values appended on open candle!"


@pytest.mark.asyncio
async def test_open_candle_does_not_change_trend():
    """Open candle (is_closed=False) on 15m should NOT update trend."""
    strategy = OrderflowStrategy()

    # Feed closed 15m candles to establish bullish trend (need 3 for trend_candles default)
    closes = [Decimal("100"), Decimal("110"), Decimal("120")]
    for close in closes:
        event = make_kline(
            interval=Interval.M15,
            is_closed=True,
            close=close,
        )
        await strategy.on_kline(event)

    state = strategy._get_state("BTCUSDT")
    assert state.trend == "bullish"

    # Feed an open 15m candle with bearish close
    open_event = make_kline(
        interval=Interval.M15,
        is_closed=False,
        close=Decimal("50"),  # Would make trend bearish if processed
    )
    await strategy.on_kline(open_event)

    # Trend should NOT change
    assert state.trend == "bullish", "Trend changed on open candle!"


@pytest.mark.asyncio
async def test_reset_trade_flow_only_on_closed_1m():
    """reset_trade_flow should only be called on closed 1m candles."""
    strategy = OrderflowStrategy()
    state = strategy._get_state("BTCUSDT")

    # Manually set volumes
    state.buy_volume = Decimal("100")
    state.sell_volume = Decimal("50")

    # Feed an open 1m candle
    open_event = make_kline(interval=Interval.M1, is_closed=False)
    await strategy.on_kline(open_event)

    # Volumes should NOT reset
    assert state.buy_volume == Decimal("100"), "buy_volume reset on open candle!"
    assert state.sell_volume == Decimal("50"), "sell_volume reset on open candle!"

    # Feed a closed 1m candle
    closed_event = make_kline(interval=Interval.M1, is_closed=True)
    await strategy.on_kline(closed_event)

    # Now volumes should be reset (or re-estimated from kline)
    # After reset + estimate, values will change
    # Key check: the reset_trade_flow was called (volumes were zeroed before estimate)
    # Since we have < 2 klines, estimate won't work, so volumes stay at 0
    assert state.buy_volume == Decimal("0"), "buy_volume not reset on closed candle!"
    assert state.sell_volume == Decimal("0"), "sell_volume not reset on closed candle!"


@pytest.mark.asyncio
async def test_closed_candle_updates_klines_1m():
    """Closed 1m candle should be stored in klines_1m."""
    strategy = OrderflowStrategy()
    state = strategy._get_state("BTCUSDT")

    assert len(state.klines_1m) == 0

    # Open candle - should NOT be stored
    open_event = make_kline(interval=Interval.M1, is_closed=False)
    await strategy.on_kline(open_event)
    assert len(state.klines_1m) == 0, "Open candle was stored!"

    # Closed candle - should be stored
    closed_event = make_kline(interval=Interval.M1, is_closed=True)
    await strategy.on_kline(closed_event)
    assert len(state.klines_1m) == 1, "Closed candle was not stored!"


@pytest.mark.asyncio
async def test_closed_candle_updates_klines_15m():
    """Closed 15m candle should be stored in klines_15m."""
    strategy = OrderflowStrategy()
    state = strategy._get_state("BTCUSDT")

    assert len(state.klines_15m) == 0

    # Open candle - should NOT be stored
    open_event = make_kline(interval=Interval.M15, is_closed=False)
    await strategy.on_kline(open_event)
    assert len(state.klines_15m) == 0, "Open 15m candle was stored!"

    # Closed candle - should be stored
    closed_event = make_kline(interval=Interval.M15, is_closed=True)
    await strategy.on_kline(closed_event)
    assert len(state.klines_15m) == 1, "Closed 15m candle was not stored!"


# --- Tests for kline volume estimation vs real trades ---

def make_trade(
    symbol: str = "BTCUSDT",
    side: Side = Side.BUY,
    amount: Decimal = Decimal("1.0"),
    price: Decimal = Decimal("100"),
    timestamp: datetime | None = None,
) -> MarketTradeEvent:
    """Helper to create MarketTradeEvent."""
    return MarketTradeEvent(
        timestamp=timestamp or datetime.utcnow(),
        symbol=symbol,
        price=price,
        amount=amount,
        side=side,
        trade_id="test_trade",
    )


@pytest.mark.asyncio
async def test_kline_volume_does_not_overwrite_recent_trades():
    """Kline volume estimation should NOT overwrite volume when recent trades exist."""
    strategy = OrderflowStrategy()
    strategy._use_kline_volume_when_no_trades = True
    strategy._no_trades_timeout_seconds = 5

    now = datetime.utcnow()

    # Send a trade event
    trade_event = make_trade(
        side=Side.BUY,
        amount=Decimal("50"),
        timestamp=now,
    )
    await strategy.on_trade(trade_event)

    state = strategy._get_state("BTCUSDT")
    assert state.buy_volume == Decimal("50")
    assert state.sell_volume == Decimal("0")
    assert state.last_trade_time == now

    # Feed some closed klines to have enough for estimation
    for i in range(3):
        kline = make_kline(
            is_closed=True,
            open_=Decimal("100"),
            close=Decimal("90"),  # Bearish - would set sell_volume > buy_volume
            volume=Decimal("1000"),
        )
        # Use a timestamp only 1 second after last trade (within timeout)
        kline = KlineEvent(
            timestamp=now + timedelta(seconds=1),
            symbol="BTCUSDT",
            interval=Interval.M1,
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("85"),
            close=Decimal("90"),
            volume=Decimal("1000"),
            is_closed=True,
        )
        await strategy.on_kline(kline)

    # Volumes should be reset by kline (reset_trade_flow), NOT overwritten by estimate
    # Because trades are recent (< 5 seconds), _estimate_volume_from_kline should be skipped
    # After reset, volumes are 0 (no estimate applied)
    assert state.buy_volume == Decimal("0"), "Volume should be reset, not estimated!"
    assert state.sell_volume == Decimal("0"), "Volume should be reset, not estimated!"


@pytest.mark.asyncio
async def test_kline_volume_estimation_when_no_trades():
    """Kline volume estimation should apply when no trades received."""
    strategy = OrderflowStrategy()
    strategy._use_kline_volume_when_no_trades = True
    strategy._no_trades_timeout_seconds = 5

    state = strategy._get_state("BTCUSDT")

    # No trades sent - last_trade_time is None
    assert state.last_trade_time is None

    # Feed 2+ closed klines for estimation to work
    now = datetime.utcnow()
    for i in range(3):
        kline = KlineEvent(
            timestamp=now + timedelta(seconds=i),
            symbol="BTCUSDT",
            interval=Interval.M1,
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("108"),  # Bullish - should set buy_volume > sell_volume
            volume=Decimal("1000"),
            is_closed=True,
        )
        await strategy.on_kline(kline)

    # With no trades, estimation should apply: bullish candle -> 60% buy, 40% sell
    assert state.buy_volume == Decimal("600"), f"Expected 600, got {state.buy_volume}"
    assert state.sell_volume == Decimal("400"), f"Expected 400, got {state.sell_volume}"


@pytest.mark.asyncio
async def test_kline_volume_estimation_when_trades_stale():
    """Kline volume estimation should apply when trades are stale (timeout exceeded)."""
    strategy = OrderflowStrategy()
    strategy._use_kline_volume_when_no_trades = True
    strategy._no_trades_timeout_seconds = 5

    now = datetime.utcnow()
    old_time = now - timedelta(seconds=10)  # 10 seconds ago

    # Send a stale trade
    trade_event = make_trade(
        side=Side.BUY,
        amount=Decimal("50"),
        timestamp=old_time,
    )
    await strategy.on_trade(trade_event)

    state = strategy._get_state("BTCUSDT")

    # Feed 2+ klines for estimation
    for i in range(3):
        kline = KlineEvent(
            timestamp=now + timedelta(seconds=i),
            symbol="BTCUSDT",
            interval=Interval.M1,
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("108"),  # Bullish
            volume=Decimal("500"),
            is_closed=True,
        )
        await strategy.on_kline(kline)

    # Trades are stale (10s > 5s timeout), so estimation should apply
    assert state.buy_volume == Decimal("300"), f"Expected 300, got {state.buy_volume}"
    assert state.sell_volume == Decimal("200"), f"Expected 200, got {state.sell_volume}"


@pytest.mark.asyncio
async def test_kline_volume_disabled_flag():
    """When use_kline_volume_when_no_trades=False, estimation never applies."""
    strategy = OrderflowStrategy()
    strategy._use_kline_volume_when_no_trades = False  # Disabled
    strategy._no_trades_timeout_seconds = 5

    state = strategy._get_state("BTCUSDT")
    assert state.last_trade_time is None  # No trades

    # Feed klines
    now = datetime.utcnow()
    for i in range(3):
        kline = KlineEvent(
            timestamp=now + timedelta(seconds=i),
            symbol="BTCUSDT",
            interval=Interval.M1,
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("108"),
            volume=Decimal("1000"),
            is_closed=True,
        )
        await strategy.on_kline(kline)

    # Even with no trades, estimation is disabled - volumes should be 0
    assert state.buy_volume == Decimal("0"), "Volume estimated when disabled!"
    assert state.sell_volume == Decimal("0"), "Volume estimated when disabled!"


@pytest.mark.asyncio
async def test_on_trade_updates_last_trade_time():
    """on_trade should update last_trade_time in state."""
    strategy = OrderflowStrategy()
    state = strategy._get_state("BTCUSDT")

    assert state.last_trade_time is None

    now = datetime.utcnow()
    trade_event = make_trade(timestamp=now)
    await strategy.on_trade(trade_event)

    assert state.last_trade_time == now, "last_trade_time not updated!"
