"""Orderflow scalping strategy with imbalance + delta analysis."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from loguru import logger

from app.core.events import Interval, KlineEvent, MarketTradeEvent, OrderBookEvent, Side
from app.strategies.base import BaseStrategy

if TYPE_CHECKING:
    from app.config import Config
    from app.core.event_bus import EventBus
    from app.trading.portfolio import Portfolio


@dataclass
class TradeInfo:
    """Trade tick info for analysis."""
    timestamp: datetime
    price: Decimal
    amount: Decimal
    side: Side


@dataclass
class OrderbookSnapshot:
    """Orderbook snapshot for delta analysis."""
    timestamp: datetime
    bid_price: Decimal
    bid_size: Decimal
    ask_price: Decimal
    ask_size: Decimal

    @property
    def imbalance(self) -> Decimal:
        """Calculate bid/ask imbalance (-1 to 1)."""
        total = self.bid_size + self.ask_size
        if total == 0:
            return Decimal("0")
        return (self.bid_size - self.ask_size) / total


@dataclass
class SymbolState:
    """Per-symbol state for strategy."""
    # Trade flow analysis
    trades: deque = field(default_factory=lambda: deque(maxlen=1000))
    buy_volume: Decimal = Decimal("0")
    sell_volume: Decimal = Decimal("0")

    # Orderbook delta
    orderbook_history: deque = field(default_factory=lambda: deque(maxlen=100))
    last_orderbook: OrderbookSnapshot | None = None

    # Kline data
    klines_1m: deque = field(default_factory=lambda: deque(maxlen=60))
    klines_15m: deque = field(default_factory=lambda: deque(maxlen=20))

    # ATR for SL calculation
    atr: Decimal = Decimal("0")
    atr_values: deque = field(default_factory=lambda: deque(maxlen=14))

    # State
    trend: str = "neutral"  # "bullish", "bearish", "neutral"
    last_signal_time: datetime | None = None

    def reset_trade_flow(self) -> None:
        """Reset trade flow for new period."""
        self.buy_volume = Decimal("0")
        self.sell_volume = Decimal("0")


class OrderflowStrategy(BaseStrategy):
    """
    Orderflow scalping strategy.

    Entry logic:
    1. Calculate buy/sell volume imbalance from trades
    2. Analyze orderbook delta (bid vs ask pressure)
    3. Use 15m kline for trend filter
    4. Enter when imbalance + delta align with trend

    Exit logic:
    - SL based on ATR
    - TP at 3x risk (configurable RR)
    - Time exit if holding too long without hitting TP
    """

    def __init__(self) -> None:
        """Initialize strategy."""
        super().__init__()
        self._states: dict[str, SymbolState] = {}

        # Parameters (will be set from config)
        self._imbalance_threshold = Decimal("0.6")
        self._delta_threshold = Decimal("0.3")
        self._rr_ratio = Decimal("3.0")
        self._atr_period = 14
        self._atr_multiplier = Decimal("1.5")
        self._lookback_trades = 100
        self._cooldown_seconds = 60
        self._max_hold_seconds = 86400  # 24 hours - let SL/TP work, don't force exit

    async def _on_initialize(self) -> None:
        """Initialize strategy with config parameters."""
        if self._config is None:
            return

        params = self._config.strategy.params
        self._imbalance_threshold = params.imbalance_threshold
        self._delta_threshold = params.delta_threshold
        self._rr_ratio = params.rr_ratio
        self._atr_period = params.atr_period
        self._atr_multiplier = params.atr_multiplier
        self._lookback_trades = params.lookback_trades
        self._cooldown_seconds = params.cooldown_seconds

        logger.info(
            f"OrderflowStrategy initialized: imbalance_th={self._imbalance_threshold}, "
            f"delta_th={self._delta_threshold}, RR={self._rr_ratio}"
        )

    def _get_state(self, symbol: str) -> SymbolState:
        """Get or create state for a symbol."""
        if symbol not in self._states:
            self._states[symbol] = SymbolState()
        return self._states[symbol]

    async def on_kline(self, event: KlineEvent) -> None:
        """Handle kline event - update trend and ATR."""
        state = self._get_state(event.symbol)

        # Store kline
        if event.interval == Interval.M1:
            state.klines_1m.append(event)
            self._update_atr(state, event)
            # Estimate volume from kline if no tick data
            self._estimate_volume_from_kline(state)
        elif event.interval == Interval.M15:
            state.klines_15m.append(event)
            self._update_trend(state)

        # Reset trade flow at each new 1m candle
        if event.interval == Interval.M1 and event.is_closed:
            state.reset_trade_flow()

    async def on_trade(self, event: MarketTradeEvent) -> None:
        """Handle trade event - accumulate volume (optional, for tick data)."""
        state = self._get_state(event.symbol)

        # Store trade
        trade_info = TradeInfo(
            timestamp=event.timestamp,
            price=event.price,
            amount=event.amount,
            side=event.side,
        )
        state.trades.append(trade_info)

        # Accumulate volume
        if event.side == Side.BUY:
            state.buy_volume += event.amount
        else:
            state.sell_volume += event.amount

    def _estimate_volume_from_kline(self, state: SymbolState) -> None:
        """Estimate buy/sell volume from kline data when trades not available."""
        if len(state.klines_1m) < 2:
            return

        kline = state.klines_1m[-1]
        prev_kline = state.klines_1m[-2]

        # Simple heuristic: if close > open, more buying pressure
        if kline.close > kline.open:
            state.buy_volume = kline.volume * Decimal("0.6")
            state.sell_volume = kline.volume * Decimal("0.4")
        else:
            state.buy_volume = kline.volume * Decimal("0.4")
            state.sell_volume = kline.volume * Decimal("0.6")

    async def on_orderbook(self, event: OrderBookEvent) -> None:
        """Handle orderbook event - analyze and potentially signal."""
        state = self._get_state(event.symbol)

        # Store orderbook snapshot
        snapshot = OrderbookSnapshot(
            timestamp=event.timestamp,
            bid_price=event.bid_price,
            bid_size=event.bid_size,
            ask_price=event.ask_price,
            ask_size=event.ask_size,
        )
        state.orderbook_history.append(snapshot)
        state.last_orderbook = snapshot

        # Check for signal
        await self._check_signal(event.symbol, event)

    async def _check_signal(self, symbol: str, event: OrderBookEvent) -> None:
        """Check if conditions are met for a signal."""
        state = self._get_state(symbol)

        # Skip if already have position
        if self.has_position(symbol):
            # Check for time exit
            await self._check_time_exit(symbol, event)
            return

        # Check cooldown
        if state.last_signal_time:
            elapsed = (event.timestamp - state.last_signal_time).total_seconds()
            if elapsed < self._cooldown_seconds:
                return

        # Need sufficient data
        if len(state.klines_1m) < 5 or len(state.klines_15m) < 3:
            return

        if state.atr <= 0:
            return

        # If no trades data, estimate from klines
        if state.buy_volume == 0 and state.sell_volume == 0:
            self._estimate_volume_from_kline(state)

        # Calculate imbalance
        total_volume = state.buy_volume + state.sell_volume
        if total_volume == 0:
            return

        volume_imbalance = (state.buy_volume - state.sell_volume) / total_volume

        # Calculate orderbook delta
        delta = self._calculate_delta(state)

        # Debug logging (every 1000th check)
        if hasattr(self, '_check_count'):
            self._check_count += 1
        else:
            self._check_count = 0

        if self._check_count % 1000 == 0:
            logger.debug(
                f"{symbol}: trend={state.trend}, imbalance={volume_imbalance:.3f}, "
                f"delta={delta:.3f}, atr={state.atr:.2f}"
            )

        # Check conditions
        signal_side = None

        if state.trend == "bullish":
            # Long signal: positive imbalance + positive delta
            if (volume_imbalance >= self._imbalance_threshold and
                delta >= self._delta_threshold):
                signal_side = "buy"

        elif state.trend == "bearish":
            # Short signal: negative imbalance + negative delta
            if (volume_imbalance <= -self._imbalance_threshold and
                delta <= -self._delta_threshold):
                signal_side = "sell"

        if signal_side:
            await self._emit_entry(symbol, signal_side, state, event)
            state.last_signal_time = event.timestamp

    def _calculate_delta(self, state: SymbolState) -> Decimal:
        """Calculate orderbook delta over recent history."""
        if len(state.orderbook_history) < 2:
            return Decimal("0")

        # Compare recent vs earlier orderbook imbalance
        recent = list(state.orderbook_history)[-10:]
        earlier = list(state.orderbook_history)[-20:-10] if len(state.orderbook_history) >= 20 else []

        if not earlier:
            return Decimal("0")

        recent_imbalance = sum(ob.imbalance for ob in recent) / len(recent)
        earlier_imbalance = sum(ob.imbalance for ob in earlier) / len(earlier)

        return recent_imbalance - earlier_imbalance

    def _update_atr(self, state: SymbolState, kline: KlineEvent) -> None:
        """Update ATR calculation."""
        if len(state.klines_1m) < 2:
            return

        prev_kline = state.klines_1m[-2]
        tr = max(
            kline.high - kline.low,
            abs(kline.high - prev_kline.close),
            abs(kline.low - prev_kline.close),
        )

        state.atr_values.append(tr)

        if len(state.atr_values) >= self._atr_period:
            state.atr = sum(state.atr_values) / len(state.atr_values)

    def _update_trend(self, state: SymbolState) -> None:
        """Update trend from 15m klines."""
        if len(state.klines_15m) < 3:
            state.trend = "neutral"
            return

        klines = list(state.klines_15m)[-3:]

        # Simple trend: compare closes
        if klines[-1].close > klines[-2].close > klines[-3].close:
            state.trend = "bullish"
        elif klines[-1].close < klines[-2].close < klines[-3].close:
            state.trend = "bearish"
        else:
            state.trend = "neutral"

    async def _emit_entry(
        self,
        symbol: str,
        side: str,
        state: SymbolState,
        event: OrderBookEvent,
    ) -> None:
        """Emit entry signal with calculated SL/TP."""
        # Entry price from orderbook
        if side == "buy":
            entry_price = event.ask_price
            sl_distance = state.atr * self._atr_multiplier
            sl_price = entry_price - sl_distance
            tp_price = entry_price + (sl_distance * self._rr_ratio)
        else:
            entry_price = event.bid_price
            sl_distance = state.atr * self._atr_multiplier
            sl_price = entry_price + sl_distance
            tp_price = entry_price - (sl_distance * self._rr_ratio)

        logger.info(
            f"Signal: {symbol} {side.upper()} @ {entry_price}, "
            f"SL={sl_price}, TP={tp_price}, trend={state.trend}"
        )

        await self._emit_entry_signal(
            event=event,
            side=side,
            entry_price=float(entry_price),
            sl_price=float(sl_price),
            tp_price=float(tp_price),
            reason=f"orderflow_{state.trend}",
            trend=state.trend,
            atr=float(state.atr),
            volume_imbalance=float((state.buy_volume - state.sell_volume) / (state.buy_volume + state.sell_volume)),
        )

    async def _check_time_exit(self, symbol: str, event: OrderBookEvent) -> None:
        """Check if position should be closed due to time limit."""
        position = self.get_position(symbol)
        if position is None:
            return

        hold_time = (event.timestamp - position.entry_time).total_seconds()

        if hold_time >= self._max_hold_seconds:
            logger.info(f"Time exit for {symbol} after {hold_time:.0f}s")
            await self._emit_exit_signal(
                event=event,
                reason="time_exit",
                hold_time=hold_time,
            )
