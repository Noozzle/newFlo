"""Orderflow scalping strategy with imbalance + delta analysis."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

from app.core.events import Interval, KlineEvent, MarketTradeEvent, OrderBookEvent, Side, TradeClosedEvent
from app.strategies.base import BaseStrategy

if TYPE_CHECKING:
    from app.config import Config
    from app.core.event_bus import EventBus
    from app.trading.portfolio import Portfolio


@dataclass(slots=True)
class TradeInfo:
    """Trade tick info for analysis."""
    timestamp: datetime
    price: float
    amount: float
    side: Side


@dataclass(slots=True)
class OrderbookSnapshot:
    """Orderbook snapshot for delta analysis."""
    timestamp: datetime
    bid_price: float
    bid_size: float
    ask_price: float
    ask_size: float

    @property
    def imbalance(self) -> float:
        """Calculate bid/ask imbalance (-1 to 1)."""
        total = self.bid_size + self.ask_size
        if total == 0:
            return 0.0
        return (self.bid_size - self.ask_size) / total


@dataclass
class SymbolState:
    """Per-symbol state for strategy."""
    # Trade flow analysis
    trades: deque = field(default_factory=lambda: deque(maxlen=1000))
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    last_trade_time: datetime | None = None  # Track when last trade was received

    # Orderbook delta
    orderbook_history: deque = field(default_factory=lambda: deque(maxlen=50))
    last_orderbook: OrderbookSnapshot | None = None

    # Kline data
    klines_1m: deque = field(default_factory=lambda: deque(maxlen=60))
    klines_15m: deque = field(default_factory=lambda: deque(maxlen=20))

    # ATR for SL calculation
    atr: float = 0.0
    atr_values: deque = field(default_factory=lambda: deque(maxlen=14))

    # State
    trend: str = "neutral"  # "bullish", "bearish", "neutral"
    last_signal_time: datetime | None = None
    # Cached avg volume (updated on kline close, avoids list() in hot path)
    cached_avg_vol: float = 0.0

    # SL tracking
    last_sl_time: datetime | None = None
    last_sl_side: str | None = None  # "buy" or "sell"

    # Breakeven tracking
    be_triggered: bool = False

    def reset_trade_flow(self) -> None:
        """Reset trade flow for new period."""
        self.buy_volume = 0.0
        self.sell_volume = 0.0


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
        self._imbalance_threshold = 0.6
        self._delta_threshold = 0.3
        self._rr_ratio = 3.0
        self._atr_period = 14
        self._atr_multiplier = 1.5
        self._lookback_trades = 100
        self._cooldown_seconds = 60
        self._max_hold_seconds = 86400  # 24 hours - let SL/TP work, don't force exit
        # Volume estimation settings
        self._use_kline_volume_when_no_trades = True
        self._no_trades_timeout_seconds = 5
        # Orderbook delta calculation settings
        self._use_time_based_delta = True
        self._ob_window_ms = 500
        self._ob_compare_gap_ms = 0  # 0 means same as ob_window_ms
        # SL protection
        self._sl_cooldown_seconds = 300  # 5 min
        self._sl_direction_block_seconds = 1200  # 20 min
        # Global SL cooldown (cross-symbol)
        self._global_sl_cooldown_seconds = 600  # 10 min
        self._last_global_sl_time: datetime | None = None
        # Trading session filter
        self._session_start_utc = 0
        self._session_end_utc = 24  # 24 = no filter
        # Trend filter
        self._trend_candles = 3
        # Volume spike filter
        self._volume_spike_mult = 2.0
        self._avg_volume_periods = 5
        # Breakeven SL trigger
        self._be_trigger_rr = 1.5

    async def _on_initialize(self) -> None:
        """Initialize strategy with config parameters."""
        if self._config is None:
            return

        params = self._config.strategy.params
        self._imbalance_threshold = float(params.imbalance_threshold)
        self._delta_threshold = float(params.delta_threshold)
        self._rr_ratio = float(params.rr_ratio)
        self._atr_period = params.atr_period
        self._atr_multiplier = float(params.atr_multiplier)
        self._lookback_trades = params.lookback_trades
        self._cooldown_seconds = params.cooldown_seconds
        self._use_kline_volume_when_no_trades = params.use_kline_volume_when_no_trades
        self._no_trades_timeout_seconds = params.no_trades_timeout_seconds
        # Orderbook delta params
        self._use_time_based_delta = params.use_time_based_delta
        self._ob_window_ms = params.ob_window_ms
        self._ob_compare_gap_ms = params.ob_compare_gap_ms if params.ob_compare_gap_ms > 0 else params.ob_window_ms
        # SL protection params
        self._sl_cooldown_seconds = params.sl_cooldown_minutes * 60
        self._sl_direction_block_seconds = params.sl_direction_block_minutes * 60
        # Global SL cooldown
        self._global_sl_cooldown_seconds = params.global_sl_cooldown_minutes * 60
        # Trading session filter
        self._session_start_utc = params.session_start_utc
        self._session_end_utc = params.session_end_utc
        # Trend filter
        self._trend_candles = params.trend_candles
        # Volume spike filter
        self._volume_spike_mult = float(params.volume_spike_mult)
        self._avg_volume_periods = params.avg_volume_periods
        # Breakeven SL trigger
        self._be_trigger_rr = float(params.be_trigger_rr)

        logger.info(
            f"OrderflowStrategy initialized: imbalance_th={self._imbalance_threshold}, "
            f"delta_th={self._delta_threshold}, RR={self._rr_ratio}, "
            f"kline_vol_fallback={self._use_kline_volume_when_no_trades}, "
            f"time_delta={'on' if self._use_time_based_delta else 'off'} "
            f"(window={self._ob_window_ms}ms, gap={self._ob_compare_gap_ms}ms), "
            f"sl_cooldown={params.sl_cooldown_minutes}min, "
            f"sl_dir_block={params.sl_direction_block_minutes}min, "
            f"global_sl_cooldown={params.global_sl_cooldown_minutes}min, "
            f"session={self._session_start_utc}-{self._session_end_utc}UTC, "
            f"trend_candles={self._trend_candles}"
        )

    def _get_state(self, symbol: str) -> SymbolState:
        """Get or create state for a symbol."""
        if symbol not in self._states:
            self._states[symbol] = SymbolState()
        return self._states[symbol]

    def _should_estimate_volume(self, state: SymbolState, current_time: datetime) -> bool:
        """Check if we should estimate volume from kline instead of using real trades.

        Returns True if:
        - use_kline_volume_when_no_trades is True AND
        - (no trades received OR last trade was more than no_trades_timeout_seconds ago)
        """
        if not self._use_kline_volume_when_no_trades:
            return False

        # No trades received at all
        if state.last_trade_time is None:
            return True

        # Check if trades are stale (older than timeout)
        elapsed = (current_time - state.last_trade_time).total_seconds()
        return elapsed > self._no_trades_timeout_seconds

    def _should_estimate_volume_from_kline(self, state: SymbolState, event: KlineEvent) -> bool:
        """Check if we should estimate volume based on kline event timestamp."""
        return self._should_estimate_volume(state, event.timestamp)

    def _should_estimate_volume_from_event(self, state: SymbolState, event: OrderBookEvent) -> bool:
        """Check if we should estimate volume based on orderbook event timestamp."""
        return self._should_estimate_volume(state, event.timestamp)

    async def on_kline(self, event: KlineEvent) -> None:
        """Handle kline event - update trend and ATR only on closed candles.

        This ensures LIVE and BACKTEST behave identically:
        only confirmed/closed candles affect ATR, trend, and volume reset.
        """
        state = self._get_state(event.symbol)

        # Only process closed candles for ATR/trend/reset (LIVE == BACKTEST consistency)
        if not event.is_closed:
            return

        # Store kline and update indicators only on closed candles
        if event.interval == Interval.M1:
            state.klines_1m.append(event)
            self._update_atr(state, event)
            # Cache avg volume for volume spike filter (avoids list() in hot path)
            n_avg = self._avg_volume_periods
            if len(state.klines_1m) >= n_avg + 1:
                kl = state.klines_1m
                # Sum the n_avg candles before the last one
                total = 0.0
                for i in range(len(kl) - n_avg - 1, len(kl) - 1):
                    if i >= 0:
                        total += float(kl[i].volume)
                state.cached_avg_vol = total / n_avg
            # Reset trade flow first, then estimate if needed
            should_estimate = self._should_estimate_volume_from_kline(state, event)
            state.reset_trade_flow()
            # Estimate volume from kline ONLY if no recent trade data
            if should_estimate:
                self._estimate_volume_from_kline(state)
        elif event.interval == Interval.M15:
            state.klines_15m.append(event)
            self._update_trend(state)

    async def on_trade(self, event: MarketTradeEvent) -> None:
        """Handle trade event - accumulate volume (optional, for tick data)."""
        state = self._get_state(event.symbol)

        # Store trade (float for fast arithmetic)
        amt = float(event.amount)
        trade_info = TradeInfo(
            timestamp=event.timestamp,
            price=float(event.price),
            amount=amt,
            side=event.side,
        )
        state.trades.append(trade_info)

        # Track last trade time to avoid kline volume overwrite
        state.last_trade_time = event.timestamp

        # Accumulate volume
        if event.side == Side.BUY:
            state.buy_volume += amt
        else:
            state.sell_volume += amt

    def _estimate_volume_from_kline(self, state: SymbolState) -> None:
        """Estimate buy/sell volume from kline data when trades not available."""
        if len(state.klines_1m) < 2:
            return

        kline = state.klines_1m[-1]
        vol = float(kline.volume)

        # Simple heuristic: if close > open, more buying pressure
        if kline.close > kline.open:
            state.buy_volume = vol * 0.6
            state.sell_volume = vol * 0.4
        else:
            state.buy_volume = vol * 0.4
            state.sell_volume = vol * 0.6

    async def on_orderbook(self, event: OrderBookEvent) -> None:
        """Handle orderbook event - analyze and potentially signal."""
        state = self._get_state(event.symbol)

        # Store orderbook snapshot (float for fast arithmetic)
        snapshot = OrderbookSnapshot(
            timestamp=event.timestamp,
            bid_price=float(event.bid_price),
            bid_size=float(event.bid_size),
            ask_price=float(event.ask_price),
            ask_size=float(event.ask_size),
        )
        state.orderbook_history.append(snapshot)
        state.last_orderbook = snapshot

        # Check for signal
        await self._check_signal(event.symbol, event)

    async def on_trade_closed(self, event: TradeClosedEvent) -> None:
        """Track SL exits for per-symbol and global cooldown, reset breakeven state."""
        state = self._get_state(event.symbol)
        state.be_triggered = False  # Reset breakeven flag for next trade

        if event.exit_reason == "sl":
            state.last_sl_time = event.timestamp
            state.last_sl_side = event.side.value  # "buy" or "sell"
            # Global SL cooldown — block ALL symbols after any SL
            self._last_global_sl_time = event.timestamp
            logger.info(
                f"SL recorded for {event.symbol} {event.side.value}, "
                f"cooldown {self._sl_cooldown_seconds}s, "
                f"dir block {self._sl_direction_block_seconds}s, "
                f"global cooldown {self._global_sl_cooldown_seconds}s"
            )

    async def _check_signal(self, symbol: str, event: OrderBookEvent) -> None:
        """Check if conditions are met for a signal."""
        state = self._get_state(symbol)

        # If already have position — check breakeven trigger and time exit
        if self.has_position(symbol):
            await self._check_breakeven(symbol, event)
            await self._check_time_exit(symbol, event)
            return

        # Trading session filter (UTC hours)
        if self._session_end_utc < 24:
            hour = event.timestamp.hour
            if not (self._session_start_utc <= hour < self._session_end_utc):
                return

        # Check cooldown
        if state.last_signal_time:
            elapsed = (event.timestamp - state.last_signal_time).total_seconds()
            if elapsed < self._cooldown_seconds:
                return

        # Global SL cooldown: no entries on ANY symbol after any SL
        if self._last_global_sl_time:
            global_sl_elapsed = (event.timestamp - self._last_global_sl_time).total_seconds()
            if global_sl_elapsed < self._global_sl_cooldown_seconds:
                return

        # Per-symbol SL cooldown: no signals at all for sl_cooldown_seconds after SL
        if state.last_sl_time:
            sl_elapsed = (event.timestamp - state.last_sl_time).total_seconds()
            if sl_elapsed < self._sl_cooldown_seconds:
                return

        # Need sufficient data
        if len(state.klines_1m) < 5 or len(state.klines_15m) < self._trend_candles:
            return

        if state.atr <= 0:
            return

        # If no trades data, estimate from klines (only if config allows and no recent trades)
        if state.buy_volume == 0 and state.sell_volume == 0:
            if self._should_estimate_volume_from_event(state, event):
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

        # Volume spike filter: last 1m candle volume must exceed avg (uses cached value)
        if self._volume_spike_mult > 0 and state.cached_avg_vol > 0:
            recent_vol = float(state.klines_1m[-1].volume)
            if recent_vol < state.cached_avg_vol * self._volume_spike_mult:
                return

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
            # Direction block: after SL, block same direction for longer period
            if (state.last_sl_time and state.last_sl_side == signal_side):
                sl_elapsed = (event.timestamp - state.last_sl_time).total_seconds()
                if sl_elapsed < self._sl_direction_block_seconds:
                    return

            await self._emit_entry(symbol, signal_side, state, event)
            state.last_signal_time = event.timestamp

    def _calculate_delta(self, state: SymbolState) -> float:
        """Calculate orderbook delta over recent history.

        Supports two modes:
        - Time-based (default): uses ob_window_ms and ob_compare_gap_ms
        - Tick-based (legacy): uses last 10 vs previous 10 ticks
        """
        if len(state.orderbook_history) < 2:
            return 0.0

        if self._use_time_based_delta:
            return self._calculate_delta_time_based(state)
        else:
            return self._calculate_delta_tick_based(state)

    def _calculate_delta_tick_based(self, state: SymbolState) -> float:
        """Calculate delta using tick-based method (legacy)."""
        # Compare recent vs earlier orderbook imbalance
        recent = list(state.orderbook_history)[-10:]
        earlier = list(state.orderbook_history)[-20:-10] if len(state.orderbook_history) >= 20 else []

        if not earlier:
            return 0.0

        recent_imbalance = sum(ob.imbalance for ob in recent) / len(recent)
        earlier_imbalance = sum(ob.imbalance for ob in earlier) / len(earlier)

        return recent_imbalance - earlier_imbalance

    def _calculate_delta_time_based(self, state: SymbolState) -> float:
        """Calculate delta using time-based windows.

        Windows:
        - recent: [now - ob_window_ms, now]
        - earlier: [now - ob_window_ms - ob_compare_gap_ms, now - ob_compare_gap_ms]

        Example with ob_window_ms=500, ob_compare_gap_ms=500:
        - recent: last 500ms
        - earlier: 500ms to 1000ms ago
        """
        if not state.orderbook_history:
            return 0.0

        # Get current time from the most recent orderbook snapshot
        now = state.orderbook_history[-1].timestamp
        window_td = timedelta(milliseconds=self._ob_window_ms)
        gap_td = timedelta(milliseconds=self._ob_compare_gap_ms)

        # Define time boundaries
        recent_start = now - window_td
        recent_end = now

        earlier_end = now - gap_td
        earlier_start = earlier_end - window_td

        # Filter snapshots by time (reversed + early break for speed)
        recent: list[OrderbookSnapshot] = []
        earlier: list[OrderbookSnapshot] = []

        for ob in reversed(state.orderbook_history):
            if ob.timestamp < earlier_start:
                break
            if recent_start <= ob.timestamp <= recent_end:
                recent.append(ob)
            elif earlier_start <= ob.timestamp <= earlier_end:
                earlier.append(ob)

        # Need data in both windows
        if not recent or not earlier:
            return 0.0

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

        state.atr_values.append(float(tr))

        if len(state.atr_values) >= self._atr_period:
            state.atr = sum(state.atr_values) / len(state.atr_values)

    def _update_trend(self, state: SymbolState) -> None:
        """Update trend from 15m klines.

        Requires trend_candles consecutive candles all closing in
        the same direction (each close > previous close for bullish,
        each close < previous close for bearish).
        """
        n = self._trend_candles
        if len(state.klines_15m) < n:
            state.trend = "neutral"
            return

        klines = list(state.klines_15m)[-n:]

        # Check if all consecutive closes are increasing (bullish)
        all_bullish = all(
            klines[i].close > klines[i - 1].close for i in range(1, n)
        )
        # Check if all consecutive closes are decreasing (bearish)
        all_bearish = all(
            klines[i].close < klines[i - 1].close for i in range(1, n)
        )

        if all_bullish:
            state.trend = "bullish"
        elif all_bearish:
            state.trend = "bearish"
        else:
            state.trend = "neutral"

    def _compute_tape_features(self, state: SymbolState, ts: datetime) -> dict[str, float]:
        """Compute tape features for 30s/60s/120s windows from state.trades deque."""
        feats: dict[str, float] = {}
        windows_s = [30, 60, 120]

        for win_s in windows_s:
            suffix = f"_{win_s}s"
            cutoff = ts - timedelta(seconds=win_s)
            buy_vol = 0.0
            sell_vol = 0.0
            count = 0

            for t in reversed(state.trades):
                if t.timestamp < cutoff:
                    break
                amt = float(t.amount)
                if t.side == Side.BUY:
                    buy_vol += amt
                else:
                    sell_vol += amt
                count += 1

            delta = buy_vol - sell_vol
            total = buy_vol + sell_vol
            feats[f"buy_vol{suffix}"] = buy_vol
            feats[f"sell_vol{suffix}"] = sell_vol
            feats[f"delta{suffix}"] = delta
            feats[f"trade_count{suffix}"] = float(count)
            feats[f"avg_trade_size{suffix}"] = total / count if count > 0 else 0.0
            feats[f"delta_ratio{suffix}"] = delta / (total + 1e-12)

        return feats

    async def _emit_entry(
        self,
        symbol: str,
        side: str,
        state: SymbolState,
        event: OrderBookEvent,
    ) -> None:
        """Emit entry signal with calculated SL/TP."""
        # Entry price from orderbook (convert to float for calculations)
        if side == "buy":
            entry_price = float(event.ask_price)
            sl_distance = float(state.atr * self._atr_multiplier)
            sl_price = entry_price - sl_distance
            tp_price = entry_price + (sl_distance * float(self._rr_ratio))
        else:
            entry_price = float(event.bid_price)
            sl_distance = float(state.atr * self._atr_multiplier)
            sl_price = entry_price + sl_distance
            tp_price = entry_price - (sl_distance * float(self._rr_ratio))

        logger.info(
            f"Signal: {symbol} {side.upper()} @ {entry_price}, "
            f"SL={sl_price}, TP={tp_price}, trend={state.trend}"
        )

        # Gate features (all from data <= current timestamp)
        mid_price = float((event.bid_price + event.ask_price) / 2)
        spread = float(event.ask_price) - float(event.bid_price)
        rel_spread = spread / mid_price if mid_price > 0 else 0.0
        ob_imbalance = float(state.last_orderbook.imbalance) if state.last_orderbook else 0.0

        # Tape features for 30s/60s/120s windows
        tape_feats = self._compute_tape_features(state, event.timestamp)

        # 1m candle features
        range_pct_1m = 0.0
        close_pos_1m = 0.0
        atr_14_1m = 0.0
        if len(state.klines_1m) >= 2:
            k = state.klines_1m[-1]
            h, l, c = float(k.high), float(k.low), float(k.close)
            rng = h - l
            range_pct_1m = rng / c if c else 0.0
            close_pos_1m = (c - l) / (rng + 1e-12)
            # ATR from klines_1m
            atr_14_1m = float(state.atr) if state.atr else 0.0

        # 15m candle features
        atr_14_15m = 0.0
        close_minus_ma20_15m = 0.0
        trend_slope_15m = 0.0
        if len(state.klines_15m) >= 2:
            closes_15 = [float(k.close) for k in state.klines_15m]
            # ATR 15m (approximate from available candles)
            if len(state.klines_15m) >= 3:
                trs = []
                kl = list(state.klines_15m)
                for i in range(1, len(kl)):
                    tr = max(
                        float(kl[i].high) - float(kl[i].low),
                        abs(float(kl[i].high) - float(kl[i-1].close)),
                        abs(float(kl[i].low) - float(kl[i-1].close)),
                    )
                    trs.append(tr)
                atr_14_15m = sum(trs[-14:]) / min(len(trs), 14)

            # MA20 (use available candles)
            ma_window = closes_15[-20:] if len(closes_15) >= 20 else closes_15
            ma20 = sum(ma_window) / len(ma_window)
            close_minus_ma20_15m = (closes_15[-1] - ma20) / (ma20 + 1e-12)

            # Trend slope (slope of MA over last 6 points)
            if len(closes_15) >= 7:
                ma_vals = []
                for i in range(6):
                    end = len(closes_15) - 5 + i
                    start = max(0, end - 20)
                    w = closes_15[start:end + 1]
                    ma_vals.append(sum(w) / len(w))
                if len(ma_vals) >= 2:
                    x = list(range(len(ma_vals)))
                    mx = sum(x) / len(x)
                    my = sum(ma_vals) / len(ma_vals)
                    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, ma_vals))
                    den = sum((xi - mx) ** 2 for xi in x)
                    slope = num / (den + 1e-12)
                    trend_slope_15m = slope / (my + 1e-12)

        # hour_utc
        hour_utc = float(event.timestamp.hour)

        # Legacy features for backward compatibility
        volume_imbalance = float(
            (state.buy_volume - state.sell_volume) / (state.buy_volume + state.sell_volume)
        ) if (state.buy_volume + state.sell_volume) > 0 else 0.0
        atr_pct = float(state.atr) / mid_price if mid_price > 0 else 0.0
        ob_delta = float(self._calculate_delta(state))

        # Trend strength
        trend_strength = 0
        if len(state.klines_15m) >= 2:
            klines_15 = list(state.klines_15m)
            for i in range(len(klines_15) - 1, 0, -1):
                if state.trend == "bullish" and klines_15[i].close > klines_15[i - 1].close:
                    trend_strength += 1
                elif state.trend == "bearish" and klines_15[i].close < klines_15[i - 1].close:
                    trend_strength += 1
                else:
                    break

        metadata = {
            # New features matching model/config.json
            "rel_spread": rel_spread,
            "ob_imbalance": ob_imbalance,
            **tape_feats,
            "range_pct_1m": range_pct_1m,
            "close_pos_1m": close_pos_1m,
            "atr_14_1m": atr_14_1m,
            "atr_14_15m": atr_14_15m,
            "close_minus_ma20_15m": close_minus_ma20_15m,
            "trend_slope_15m": trend_slope_15m,
            "hour_utc": hour_utc,
            # Legacy features
            "volume_imbalance": volume_imbalance,
            "atr_pct": atr_pct,
            "spread_pct": rel_spread,
            "ob_delta": ob_delta,
            "trend_strength": trend_strength,
        }

        await self._emit_entry_signal(
            event=event,
            side=side,
            entry_price=float(entry_price),
            sl_price=float(sl_price),
            tp_price=float(tp_price),
            reason=f"orderflow_{state.trend}",
            trend=state.trend,
            atr=float(state.atr),
            **metadata,
        )

    async def _check_breakeven(self, symbol: str, event: OrderBookEvent) -> None:
        """Move SL to breakeven when price reaches be_trigger_rr × risk distance."""
        if self._be_trigger_rr <= 0:
            return

        state = self._get_state(symbol)
        if state.be_triggered:
            return

        position = self.get_position(symbol)
        if position is None:
            return

        entry_price = float(position.entry_price)
        sl_price = float(position.sl_price)
        risk_dist = abs(entry_price - sl_price)
        if risk_dist <= 0:
            return

        trigger_dist = risk_dist * float(self._be_trigger_rr)
        mid_price = float((event.bid_price + event.ask_price) / 2)

        triggered = False
        if position.side.value == "buy":
            triggered = mid_price >= entry_price + trigger_dist
        else:
            triggered = mid_price <= entry_price - trigger_dist

        if triggered:
            state.be_triggered = True
            logger.info(
                f"Breakeven triggered for {symbol}: moving SL to {entry_price}"
            )
            await self._emit_modify_signal(
                event=event,
                new_sl=entry_price,
                reason="breakeven",
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
