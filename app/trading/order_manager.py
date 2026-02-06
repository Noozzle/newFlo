"""Order manager for order lifecycle and execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

from loguru import logger

from app.adapters.exchange_adapter import ExchangeAdapter, OrderRequest, OrderResponse
from app.config import CostsConfig
from app.core.events import (
    FillEvent,
    OrderStatus,
    OrderType,
    OrderUpdateEvent,
    Side,
)
from app.trading.portfolio import Portfolio
from app.trading.risk_manager import RiskManager
from app.trading.signals import EntrySignal, ExitSignal, Trade

if TYPE_CHECKING:
    from app.core.event_bus import EventBus
    from app.notifications.telegram import TelegramNotifier
    from app.storage.trade_store import TradeStore


@dataclass
class PendingEntry:
    """Pending entry order tracking."""
    signal: EntrySignal
    order_id: str
    client_order_id: str
    size: Decimal
    status: OrderStatus = OrderStatus.NEW


class OrderManager:
    """
    Order manager handles order lifecycle.

    Responsibilities:
    - Execute entry/exit signals via exchange adapter
    - Ensure SL/TP are set on exchange after entry
    - Track pending orders
    - Handle order updates and fills
    - Reconcile state on startup (live mode)
    """

    def __init__(
        self,
        event_bus: "EventBus",
        exchange: ExchangeAdapter,
        portfolio: Portfolio,
        risk_manager: RiskManager,
        costs: CostsConfig,
        telegram: "TelegramNotifier | None" = None,
        trade_store: "TradeStore | None" = None,
    ) -> None:
        """
        Initialize order manager.

        Args:
            event_bus: Event bus for subscribing to events
            exchange: Exchange adapter for order execution
            portfolio: Portfolio for position tracking
            risk_manager: Risk manager for sizing
            costs: Trading costs configuration
            telegram: Optional telegram notifier for trade alerts
            trade_store: Optional trade store for persisting trades
        """
        self._event_bus = event_bus
        self._exchange = exchange
        self._portfolio = portfolio
        self._risk_manager = risk_manager
        self._costs = costs
        self._telegram = telegram
        self._trade_store = trade_store

        self._pending_entries: dict[str, PendingEntry] = {}
        self._pending_exits: dict[str, str] = {}  # symbol -> order_id

        # Subscribe to order events
        self._event_bus.subscribe(OrderUpdateEvent, self._on_order_update)
        self._event_bus.subscribe(FillEvent, self._on_fill)

    async def execute_entry(self, signal: EntrySignal) -> OrderResponse:
        """
        Execute an entry signal.

        Args:
            signal: Entry signal from strategy

        Returns:
            Order response
        """
        # Validate and size the position
        valid, reason = self._risk_manager.validate_signal(signal)
        if not valid:
            logger.warning(f"Signal rejected: {reason}")
            return OrderResponse(
                success=False,
                message=reason,
                status=OrderStatus.REJECTED,
            )

        size_result = self._risk_manager.calculate_position_size(signal)
        if not size_result.approved:
            logger.warning(f"Position sizing rejected: {size_result.reason}")
            return OrderResponse(
                success=False,
                message=size_result.reason,
                status=OrderStatus.REJECTED,
            )

        # Create order request
        client_order_id = f"entry_{signal.symbol}_{uuid4().hex[:8]}"

        request = OrderRequest(
            symbol=signal.symbol,
            side=signal.side,
            order_type=OrderType.MARKET,  # Always market for now
            qty=size_result.size,
            sl_price=signal.sl_price,
            tp_price=signal.tp_price,
            client_order_id=client_order_id,
        )

        logger.info(
            f"Executing entry: {signal.symbol} {signal.side.value} "
            f"size={size_result.size}, SL={signal.sl_price}, TP={signal.tp_price}"
        )

        # Mark symbol as pending BEFORE placing order to prevent duplicate entries
        # This is critical for LIVE mode where fills arrive asynchronously
        self._portfolio.mark_pending_entry(signal.symbol)

        # Track pending entry BEFORE placing order because
        # fill events are published synchronously during place_order
        # Use client_order_id as key since we control it and it's in FillEvent
        self._pending_entries[client_order_id] = PendingEntry(
            signal=signal,
            order_id="",  # Will be updated after place_order
            client_order_id=client_order_id,
            size=size_result.size,
        )

        # Place order
        response = await self._exchange.place_order(request)

        if response.success:
            # Update tracking with actual order_id
            if client_order_id in self._pending_entries:
                self._pending_entries[client_order_id].order_id = response.order_id

            # For market orders, we expect immediate fill, but SL/TP
            # should already be set via the order parameters
            # If exchange doesn't support that, set them explicitly
            if response.status == OrderStatus.FILLED:
                await self._ensure_sl_tp(signal.symbol, signal.sl_price, signal.tp_price)
        else:
            # Order failed, remove pending entry and clear pending status
            self._pending_entries.pop(client_order_id, None)
            self._portfolio.clear_pending_entry(signal.symbol)

        return response

    async def execute_exit(
        self,
        signal: ExitSignal,
        size: Decimal | None = None,
    ) -> OrderResponse:
        """
        Execute an exit signal.

        Args:
            signal: Exit signal from strategy
            size: Optional size (uses full position if None)

        Returns:
            Order response
        """
        position = self._portfolio.get_position(signal.symbol)
        if position is None:
            return OrderResponse(
                success=False,
                message="No position to exit",
                status=OrderStatus.REJECTED,
            )

        exit_size = size or position.size

        # Partial exit
        if signal.partial_pct < Decimal("100"):
            exit_size = position.size * (signal.partial_pct / 100)

        client_order_id = f"exit_{signal.symbol}_{uuid4().hex[:8]}"

        request = OrderRequest(
            symbol=signal.symbol,
            side=position.side.opposite,
            order_type=OrderType.MARKET,
            qty=exit_size,
            reduce_only=True,
            client_order_id=client_order_id,
        )

        logger.info(
            f"Executing exit: {signal.symbol} size={exit_size}, reason={signal.reason}"
        )

        response = await self._exchange.place_order(request)

        if response.success:
            self._pending_exits[signal.symbol] = response.order_id

        return response

    async def cancel_all_orders(self, symbol: str | None = None) -> None:
        """Cancel all pending orders for a symbol or all symbols."""
        await self._exchange.cancel_all_orders(symbol)

        # Clear tracking
        if symbol:
            self._pending_entries = {
                k: v for k, v in self._pending_entries.items()
                if v.signal.symbol != symbol
            }
            self._pending_exits.pop(symbol, None)
        else:
            self._pending_entries.clear()
            self._pending_exits.clear()

    async def _ensure_sl_tp(
        self,
        symbol: str,
        sl_price: Decimal | None,
        tp_price: Decimal | None,
    ) -> None:
        """Ensure SL/TP are set on the exchange."""
        if sl_price or tp_price:
            response = await self._exchange.set_sl_tp(symbol, sl_price, tp_price)
            if not response.success:
                logger.error(f"Failed to set SL/TP for {symbol}: {response.message}")

    async def _on_order_update(self, event: OrderUpdateEvent) -> None:
        """Handle order update events."""
        logger.debug(
            f"Order update: {event.order_id} status={event.status.value} "
            f"filled={event.filled_qty}/{event.qty}"
        )

        # Update pending entry status
        if event.order_id in self._pending_entries:
            pending = self._pending_entries[event.order_id]
            pending.status = event.status

            if event.status == OrderStatus.FILLED:
                # Entry completed, ensure SL/TP
                await self._ensure_sl_tp(
                    pending.signal.symbol,
                    pending.signal.sl_price,
                    pending.signal.tp_price,
                )
                del self._pending_entries[event.order_id]

            elif event.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                # Clear pending status for the symbol
                self._portfolio.clear_pending_entry(pending.signal.symbol)
                del self._pending_entries[event.order_id]

    async def _on_fill(self, event: FillEvent) -> None:
        """Handle fill events."""
        logger.info(
            f"Fill received: {event.symbol} {event.side.value} {event.qty}@{event.price}, "
            f"client_order_id={event.client_order_id}, order_id={event.order_id}, "
            f"stop_order_type={event.stop_order_type}"
        )

        # Save fill to trade store
        if self._trade_store:
            asyncio.create_task(self._trade_store.save_fill(event))

        # Log pending entries for debugging
        logger.debug(f"Pending entries keys: {list(self._pending_entries.keys())}")

        # Check if this is an entry fill (match by client_order_id)
        pending = self._pending_entries.get(event.client_order_id)
        if pending:
            logger.info(f"Matched pending entry for {event.client_order_id}")
            # Clear pending entry status (position will be added below)
            self._portfolio.clear_pending_entry(pending.signal.symbol)

            # Open position in portfolio
            position = self._portfolio.open_position(
                symbol=pending.signal.symbol,
                side=pending.signal.side,
                entry_price=event.price,
                size=event.qty,
                sl_price=pending.signal.sl_price,
                tp_price=pending.signal.tp_price,
                entry_fees=event.fee,
                timestamp=event.timestamp,
                metadata=pending.signal.metadata,
            )

            # Send telegram notification
            if self._telegram:
                logger.info(f"Sending telegram entry notification for {position.symbol}")
                try:
                    await self._telegram.notify_entry(position)
                    logger.info("Telegram entry notification sent")
                except Exception as e:
                    logger.error(f"Failed to send telegram entry notification: {e}")

            # Remove from pending
            del self._pending_entries[event.client_order_id]
            return

        # Check if this is an SL/TP or manual exit fill
        symbol = event.symbol
        position = self._portfolio.get_position(symbol)

        # Determine if this is an exit (SL/TP trigger or pending exit)
        # Check both: stop_order_type from live Bybit WS, and order_id prefix from backtest
        is_sl = event.stop_order_type == "StopLoss" or event.order_id.startswith("sl_")
        is_tp = event.stop_order_type == "TakeProfit" or event.order_id.startswith("tp_")
        is_sl_tp = is_sl or is_tp
        is_pending_exit = symbol in self._pending_exits

        if position and (is_sl_tp or is_pending_exit):
            # Determine exit reason
            exit_reason = "signal"
            if is_sl:
                exit_reason = "sl"
            elif is_tp:
                exit_reason = "tp"

            # Calculate slippage estimate
            if position.side == Side.BUY:
                slippage = (position.sl_price - event.price) if exit_reason == "sl" else Decimal("0")
            else:
                slippage = (event.price - position.sl_price) if exit_reason == "sl" else Decimal("0")
            slippage = max(slippage, Decimal("0")) * event.qty

            # Close position
            trade = self._portfolio.close_position(
                symbol=symbol,
                exit_price=event.price,
                exit_fees=event.fee,
                timestamp=event.timestamp,
                exit_reason=exit_reason,
                slippage_estimate=slippage,
            )

            if trade:
                if trade.exit_reason == "sl":
                    self._risk_manager.record_sl()

                # Send telegram notification
                if self._telegram:
                    logger.info(f"Sending telegram exit notification for {trade.symbol}")
                    try:
                        await self._telegram.notify_exit(trade)
                        logger.info("Telegram exit notification sent")
                    except Exception as e:
                        logger.error(f"Failed to send telegram exit notification: {e}")

                # Save trade to store
                if self._trade_store:
                    asyncio.create_task(self._trade_store.save_trade(trade))

            # Clean up pending exit tracking if present
            self._pending_exits.pop(symbol, None)

    async def reconcile(self) -> None:
        """
        Reconcile state with exchange (for live mode restart).

        Fetches current positions and orders from exchange and
        syncs with local state.
        """
        logger.info("Reconciling state with exchange...")

        # Get balance FIRST - needed for risk calculations
        balance = await self._exchange.get_balance()
        logger.info(f"Account balance: {balance.total} (available: {balance.available})")

        # Initialize portfolio with exchange balance
        if balance.total > 0:
            self._portfolio.initialize_balance(balance.total)
            logger.info(f"Portfolio initialized with balance: {balance.total}")
        else:
            logger.warning("Exchange returned 0 balance - cannot trade without balance")

        # Get positions from exchange
        positions = await self._exchange.get_all_positions()
        for pos in positions:
            if pos.size > 0 and pos.side is not None:
                logger.info(
                    f"Found existing position: {pos.symbol} {pos.side.value} "
                    f"{pos.size}@{pos.entry_price}"
                )
                # Restore position in portfolio (without fees since we don't know original)
                # SL/TP should be fetched from exchange or state store
                try:
                    self._portfolio.open_position(
                        symbol=pos.symbol,
                        side=pos.side,
                        entry_price=pos.entry_price,
                        size=pos.size,
                        sl_price=Decimal("0"),  # Unknown, should be fetched
                        tp_price=Decimal("0"),  # Unknown, should be fetched
                        entry_fees=Decimal("0"),  # Unknown
                        timestamp=datetime.utcnow(),
                        metadata={"reconciled": True},
                    )
                except ValueError as e:
                    logger.warning(f"Could not restore position {pos.symbol}: {e}")

        # Reconstruct daily SL count from today's closed trades
        if self._trade_store:
            from datetime import timezone
            today = datetime.now(timezone.utc).date()
            sl_count = await self._trade_store.get_daily_sl_count(today)
            self._risk_manager.reconstruct_daily_sl_count(sl_count, today)

        # Get open orders
        orders = await self._exchange.get_open_orders()
        for order in orders:
            logger.info(
                f"Found open order: {order.symbol} {order.side.value} "
                f"{order.qty}@{order.price} ({order.status.value})"
            )

    @property
    def has_pending_orders(self) -> bool:
        """Check if there are any pending orders."""
        return bool(self._pending_entries) or bool(self._pending_exits)
