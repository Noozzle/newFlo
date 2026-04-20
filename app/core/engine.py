"""Main trading engine - shared between backtest and live modes."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from loguru import logger

from app.adapters.data_feed import DataFeed
from app.adapters.exchange_adapter import ExchangeAdapter
from app.adapters.historical_data_feed import HistoricalDataFeed
from app.adapters.simulated_adapter import SimulatedExchangeAdapter
from app.config import Config, Mode
from app.core.event_bus import EventBus
from app.core.events import (
    BaseEvent,
    KlineEvent,
    MarketTradeEvent,
    OrderBookEvent,
    SignalEvent,
    TradeClosedEvent,
)
from app.trading.ai_gate import AIGate, GateAction, GateDecision
from app.trading.order_manager import OrderManager
from app.trading.portfolio import Portfolio
from app.trading.risk_manager import RiskManager
from app.trading.signals import EntrySignal, ExitSignal

if TYPE_CHECKING:
    from app.notifications.telegram import TelegramNotifier
    from app.storage.trade_store import TradeStore
    from app.strategies.base import BaseStrategy


class Engine:
    """
    Main trading engine.

    This is the core of the system - it orchestrates data feeds,
    strategies, risk management, and order execution. The same
    engine is used for both backtest and live trading.

    In backtest mode:
    - HistoricalDataFeed provides events from CSV files
    - SimulatedExchangeAdapter simulates order fills

    In live mode:
    - LiveDataFeed provides events from Bybit WebSocket
    - BybitAdapter executes real orders
    """

    def __init__(
        self,
        config: Config,
        data_feed: DataFeed,
        exchange: ExchangeAdapter,
        strategy: "BaseStrategy",
        event_bus: EventBus | None = None,
        telegram: "TelegramNotifier | None" = None,
        trade_store: "TradeStore | None" = None,
    ) -> None:
        """
        Initialize engine.

        Args:
            config: Application configuration
            data_feed: Data feed (historical or live)
            exchange: Exchange adapter (simulated or live)
            strategy: Trading strategy
            event_bus: Event bus (shared with adapters)
            telegram: Optional telegram notifier for trade alerts
            trade_store: Optional trade store for persisting trades
        """
        self._config = config
        self._data_feed = data_feed
        self._exchange = exchange
        self._strategy = strategy
        self._trade_store = trade_store

        # Event bus - use provided or create new
        self._event_bus = event_bus or EventBus()

        # Portfolio and risk
        initial_balance = config.backtest.initial_capital if config.mode == Mode.BACKTEST else Decimal("0")
        self._portfolio = Portfolio(initial_balance=initial_balance)
        self._risk_manager = RiskManager(
            config=config.risk,
            costs=config.costs,
            portfolio=self._portfolio,
        )

        # AI gate
        try:
            gate_cfg = config.ai_gate
            self._ai_gate = AIGate(
                model_path=gate_cfg.model_path,
                full_threshold=gate_cfg.full_threshold,
                half_threshold=gate_cfg.half_threshold,
                fallback_action=gate_cfg.fallback_action,
                log_path=gate_cfg.log_path if gate_cfg.log_signals else None,
                enabled=gate_cfg.enabled,
                rules_config=gate_cfg.rules,
            )
        except Exception:
            self._ai_gate = AIGate(enabled=False)

        # Order manager
        self._order_manager = OrderManager(
            event_bus=self._event_bus,
            exchange=exchange,
            portfolio=self._portfolio,
            risk_manager=self._risk_manager,
            costs=config.costs,
            telegram=telegram,
            trade_store=trade_store,
        )

        # Warm-up: if data feed has a trade_start_date, skip signals before it
        self._trade_start_date: datetime | None = None
        if isinstance(data_feed, HistoricalDataFeed):
            self._trade_start_date = getattr(data_feed, 'trade_start_date', None)

        # State
        self._running = False
        self._current_time: datetime | None = None
        self._current_prices: dict[str, Decimal | float] = {}
        self._events_processed = 0

        # Profiling accumulators (seconds)
        self._prof = {
            "data_iter": 0.0,
            "dispatch": 0.0,
            "strategy": 0.0,
            "exchange": 0.0,
            "signal": 0.0,
        }

        # Register event handlers
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register event handlers."""
        self._event_bus.subscribe(KlineEvent, self._on_kline)
        self._event_bus.subscribe(MarketTradeEvent, self._on_trade)
        self._event_bus.subscribe(OrderBookEvent, self._on_orderbook)
        self._event_bus.subscribe(SignalEvent, self._on_signal)
        self._event_bus.subscribe(TradeClosedEvent, self._on_trade_closed)

    async def _on_kline(self, event: KlineEvent) -> None:
        """Handle kline event."""
        self._current_time = event.timestamp
        self._current_prices[event.symbol] = event.close

        # Check for new trading day (reset daily loss limit)
        # In live mode, use realtime clock; in backtest, use event time
        is_live = self._config.mode == Mode.LIVE
        self._risk_manager.check_new_day(event.timestamp, use_realtime=is_live)

        # Forward to strategy
        t0 = time.perf_counter()
        await self._strategy.on_kline(event)
        self._prof["strategy"] += time.perf_counter() - t0

        # Update equity curve periodically
        if self._events_processed % 100 == 0:
            self._portfolio.update_equity_curve(event.timestamp, self._current_prices)

    async def _on_trade(self, event: MarketTradeEvent) -> None:
        """Handle market trade event."""
        self._current_time = event.timestamp
        self._current_prices[event.symbol] = event.price

        # Forward to strategy
        t0 = time.perf_counter()
        await self._strategy.on_trade(event)
        self._prof["strategy"] += time.perf_counter() - t0

    async def _on_orderbook(self, event: OrderBookEvent) -> None:
        """Handle orderbook event."""
        self._current_time = event.timestamp
        self._current_prices[event.symbol] = event.mid_price

        # Forward to simulated exchange for fill checking
        if isinstance(self._exchange, SimulatedExchangeAdapter):
            t0 = time.perf_counter()
            await self._exchange.process_orderbook(event)
            self._prof["exchange"] += time.perf_counter() - t0

        # Forward to strategy
        t0 = time.perf_counter()
        await self._strategy.on_orderbook(event)
        self._prof["strategy"] += time.perf_counter() - t0

    async def _on_signal(self, event: SignalEvent) -> None:
        """Handle trading signal from strategy."""
        # Skip signals during warm-up period
        if self._trade_start_date and event.timestamp < self._trade_start_date:
            return

        # Only trade symbols from the trade list, not record-only symbols
        if event.symbol not in self._config.symbols.trade:
            return

        if event.signal_type == "entry":
            signal = EntrySignal(
                timestamp=event.timestamp,
                symbol=event.symbol,
                side=event.side,
                entry_price=event.price or self._current_prices.get(event.symbol, Decimal("0")),
                sl_price=event.sl_price or Decimal("0"),
                tp_price=event.tp_price or Decimal("0"),
                size=event.size,
                reason=event.reason,
                metadata=event.metadata,
            )

            # AI gate: SKIP / HALF / FULL
            t0 = time.perf_counter()
            decision = await self._ai_gate.decide(signal)

            # Persist gate decision for post-hoc analysis / retune.
            # Uses p_win as the scalar score (None if no model).
            if self._trade_store is not None:
                try:
                    await self._trade_store.save_gate_decision(
                        timestamp=signal.timestamp,
                        symbol=signal.symbol,
                        side=signal.side.value,
                        action=decision.action.value,
                        reason=decision.reason,
                        score=decision.p_win,
                        features_snapshot=signal.metadata,
                        trade_id=None,
                    )
                except Exception as exc:
                    logger.warning(f"Failed to persist AI gate decision: {exc}")

            if decision.action == GateAction.SKIP:
                self._prof["signal"] += time.perf_counter() - t0
                logger.info(f"AI gate SKIP: {signal.symbol} {signal.side.value} ({decision.reason})")
                return
            if decision.action == GateAction.HALF:
                signal.metadata["risk_scale"] = 0.5

            await self._order_manager.execute_entry(signal)
            self._prof["signal"] += time.perf_counter() - t0

        elif event.signal_type == "modify":
            await self._order_manager.execute_modify(
                symbol=event.symbol,
                sl_price=event.sl_price,
                tp_price=event.tp_price,
            )

        elif event.signal_type == "exit":
            signal = ExitSignal(
                timestamp=event.timestamp,
                symbol=event.symbol,
                reason=event.reason,
                exit_price=event.price,
                metadata=event.metadata,
            )
            t0 = time.perf_counter()
            await self._order_manager.execute_exit(signal)
            self._prof["signal"] += time.perf_counter() - t0

    async def _on_trade_closed(self, event: TradeClosedEvent) -> None:
        """Forward trade closed event to strategy."""
        await self._strategy.on_trade_closed(event)

    async def run_backtest(self) -> None:
        """
        Run backtest mode.

        Loads historical data and processes events in chronological order.
        """
        if not isinstance(self._data_feed, HistoricalDataFeed):
            raise ValueError("Backtest mode requires HistoricalDataFeed")

        logger.info("Starting backtest...")

        # Start components
        await self._data_feed.start()
        await self._exchange.start()

        # Subscribe to symbols (match live: trade + record)
        symbols = self._config.backtest.symbols or list(
            set(self._config.symbols.trade + self._config.symbols.record)
        )
        for symbol in symbols:
            await self._data_feed.subscribe(symbol)

        logger.info(f"Loaded {self._data_feed.total_events} events")

        # Initialize strategy
        await self._strategy.initialize(
            event_bus=self._event_bus,
            portfolio=self._portfolio,
            config=self._config,
        )

        self._running = True
        wall_start = time.perf_counter()

        # Fast-path: direct handler dispatch bypasses EventBus for data feed events.
        # HistoricalDataFeed produces time-sorted events (k-way merge), so no
        # priority queue is needed.  Internal events (SignalEvent, TradeClosedEvent,
        # FillEvent, etc.) still route through EventBus via publish_immediate.
        _fast = {
            KlineEvent: self._on_kline,
            MarketTradeEvent: self._on_trade,
            OrderBookEvent: self._on_orderbook,
        }
        _fast_get = _fast.get  # avoid attr lookup in hot loop
        _eb_publish = self._event_bus.publish_immediate  # fallback
        is_sim = isinstance(self._exchange, SimulatedExchangeAdapter)
        _update_time = self._exchange.update_time if is_sim else None
        _prof = self._prof

        # Process events one by one
        # Note: _check_day is called inside _on_kline (every ~60s), not per-event
        try:
            t_iter = time.perf_counter()
            for event in self._data_feed.iter_events():
                _prof["data_iter"] += time.perf_counter() - t_iter

                if not self._running:
                    break

                # Update simulated exchange time
                if _update_time is not None:
                    _update_time(event.timestamp)

                # Dispatch event — direct call for data feed types, EventBus for others
                t_disp = time.perf_counter()
                handler = _fast_get(type(event))
                if handler is not None:
                    await handler(event)
                else:
                    await _eb_publish(event)
                _prof["dispatch"] += time.perf_counter() - t_disp

                self._events_processed += 1

                # Progress logging
                if self._events_processed % 500000 == 0:
                    logger.info(f"Processed {self._events_processed} events")

                t_iter = time.perf_counter()

        finally:
            self._running = False
            await self._exchange.stop()
            await self._data_feed.stop()

        # Final equity curve update
        if self._current_time:
            self._portfolio.update_equity_curve(self._current_time, self._current_prices)

        wall_total = time.perf_counter() - wall_start

        logger.info(
            f"Backtest complete. Processed {self._events_processed} events, "
            f"{len(self._portfolio.trades)} trades"
        )
        self._print_profile(wall_total)

    def _print_profile(self, wall_total: float) -> None:
        """Print profiling summary table."""
        p = self._prof
        dispatch_overhead = p["dispatch"] - p["strategy"] - p["exchange"] - p["signal"]
        accounted = p["data_iter"] + p["dispatch"]
        other = wall_total - accounted

        eps = self._events_processed / wall_total if wall_total > 0 else 0

        logger.info(
            f"\n{'─'*50}\n"
            f"  BACKTEST PROFILE  ({self._events_processed:,} events in {wall_total:.1f}s = {eps:,.0f} ev/s)\n"
            f"{'─'*50}\n"
            f"  Data load/parse     {p['data_iter']:>8.2f}s  ({p['data_iter']/wall_total*100:5.1f}%)\n"
            f"  Event dispatch tot  {p['dispatch']:>8.2f}s  ({p['dispatch']/wall_total*100:5.1f}%)\n"
            f"    Strategy handlers {p['strategy']:>8.2f}s  ({p['strategy']/wall_total*100:5.1f}%)\n"
            f"    Simulated exch    {p['exchange']:>8.2f}s  ({p['exchange']/wall_total*100:5.1f}%)\n"
            f"    Signal (gate+OM)  {p['signal']:>8.2f}s  ({p['signal']/wall_total*100:5.1f}%)\n"
            f"    Bus overhead      {dispatch_overhead:>8.2f}s  ({dispatch_overhead/wall_total*100:5.1f}%)\n"
            f"  Other (setup/log)   {other:>8.2f}s  ({other/wall_total*100:5.1f}%)\n"
            f"{'─'*50}"
        )

    async def run_live(self) -> None:
        """
        Run live trading mode.

        Connects to exchange, subscribes to data feeds, and processes
        events in real-time.
        """
        logger.info("Starting live trading...")

        # Start components
        await self._exchange.start()
        await self._data_feed.start()

        # Reconcile state (skip if no API keys - allows data recording without trading)
        if self._config.bybit.api_key and self._config.bybit.api_secret:
            await self._order_manager.reconcile()
        else:
            logger.warning("No API keys configured - skipping reconcile, trading disabled")

        # Subscribe to symbols (deduplicate in case symbol is in both trade and record)
        all_symbols = set(self._config.symbols.trade + self._config.symbols.record)
        for symbol in all_symbols:
            await self._data_feed.subscribe(symbol)

        # Initialize strategy
        await self._strategy.initialize(
            event_bus=self._event_bus,
            portfolio=self._portfolio,
            config=self._config,
        )

        self._running = True
        stop_event = asyncio.Event()

        # Run event processing loop
        try:
            await self._event_bus.run(stop_event)
        except KeyboardInterrupt:
            logger.info("Received shutdown signal")
        finally:
            self._running = False
            stop_event.set()
            await self._exchange.stop()
            await self._data_feed.stop()

        logger.info("Live trading stopped")

    def stop(self) -> None:
        """Signal the engine to stop."""
        self._running = False
        self._event_bus.stop()

    @property
    def portfolio(self) -> Portfolio:
        """Get portfolio."""
        return self._portfolio

    @property
    def trades(self) -> list:
        """Get completed trades."""
        return self._portfolio.trades

    @property
    def event_bus(self) -> EventBus:
        """Get event bus."""
        return self._event_bus

    @property
    def events_processed(self) -> int:
        """Get number of events processed."""
        return self._events_processed
