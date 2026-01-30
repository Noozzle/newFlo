"""Simulated exchange adapter for backtesting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

from loguru import logger

from app.adapters.exchange_adapter import (
    Balance,
    ExchangeAdapter,
    Order,
    OrderRequest,
    OrderResponse,
    Position,
)
from app.config import CostsConfig
from app.core.events import (
    FillEvent,
    OrderBookEvent,
    OrderStatus,
    OrderType,
    OrderUpdateEvent,
    PositionEvent,
    Side,
)

if TYPE_CHECKING:
    from app.core.event_bus import EventBus


@dataclass
class SimulatedPosition:
    """Internal position tracking."""
    symbol: str
    side: Side | None = None
    size: Decimal = Decimal("0")
    entry_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    sl_price: Decimal | None = None
    tp_price: Decimal | None = None

    @property
    def is_open(self) -> bool:
        return self.size > 0


@dataclass
class PendingOrder:
    """Pending limit order."""
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    qty: Decimal
    price: Decimal
    filled_qty: Decimal = Decimal("0")
    sl_price: Decimal | None = None
    tp_price: Decimal | None = None
    reduce_only: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)


class SimulatedExchangeAdapter(ExchangeAdapter):
    """
    Simulated exchange for backtesting.

    Features:
    - Market orders: Fill at best bid/ask + configurable slippage
    - Limit orders: Fill when price crosses order level
    - Fee calculation
    - Position tracking with SL/TP simulation
    """

    def __init__(
        self,
        event_bus: "EventBus",
        costs: CostsConfig,
        initial_balance: Decimal = Decimal("10000"),
        leverage: int = 10,
    ) -> None:
        """
        Initialize simulated exchange.

        Args:
            event_bus: Event bus for publishing events
            costs: Trading costs configuration
            initial_balance: Initial account balance
            leverage: Default leverage
        """
        super().__init__(event_bus)
        self._costs = costs
        self._leverage = leverage

        # Account state
        self._balance = initial_balance
        self._equity = initial_balance
        self._initial_balance = initial_balance

        # Positions and orders
        self._positions: dict[str, SimulatedPosition] = {}
        self._pending_orders: dict[str, PendingOrder] = {}
        self._order_history: list[Order] = []

        # Market state (updated by process_orderbook)
        self._last_prices: dict[str, tuple[Decimal, Decimal]] = {}  # symbol -> (bid, ask)
        self._current_time: datetime = datetime.utcnow()

    async def start(self) -> None:
        """Start the simulated exchange."""
        self._running = True
        logger.info(
            f"Simulated exchange started with balance={self._balance}, leverage={self._leverage}"
        )

    async def stop(self) -> None:
        """Stop the simulated exchange."""
        self._running = False
        # Close all positions at current price
        for symbol, pos in list(self._positions.items()):
            if pos.is_open:
                await self._close_position(symbol, "end_of_backtest")
        logger.info(
            f"Simulated exchange stopped. Final balance: {self._balance}, "
            f"P&L: {self._balance - self._initial_balance}"
        )

    async def place_order(self, request: OrderRequest) -> OrderResponse:
        """Place an order (market or limit)."""
        order_id = str(uuid4())[:8]
        client_order_id = request.client_order_id or f"sim_{order_id}"

        if request.order_type == OrderType.MARKET:
            return await self._execute_market_order(request, order_id, client_order_id)
        else:
            return await self._create_limit_order(request, order_id, client_order_id)

    async def _execute_market_order(
        self,
        request: OrderRequest,
        order_id: str,
        client_order_id: str,
    ) -> OrderResponse:
        """Execute a market order immediately."""
        symbol = request.symbol
        prices = self._last_prices.get(symbol)

        if not prices:
            return OrderResponse(
                success=False,
                order_id=order_id,
                client_order_id=client_order_id,
                message="No price data available",
                status=OrderStatus.REJECTED,
            )

        bid_price, ask_price = prices

        # Fill price with slippage
        if request.side == Side.BUY:
            fill_price = ask_price * (1 + self._costs.slippage_pct)
        else:
            fill_price = bid_price * (1 - self._costs.slippage_pct)

        # Calculate fee
        notional = fill_price * request.qty
        fee = notional * self._costs.fees_pct

        # Execute the fill
        await self._process_fill(
            symbol=symbol,
            side=request.side,
            qty=request.qty,
            price=fill_price,
            fee=fee,
            order_id=order_id,
            client_order_id=client_order_id,
            reduce_only=request.reduce_only,
        )

        # Set SL/TP if provided
        if request.sl_price or request.tp_price:
            pos = self._positions.get(symbol)
            if pos and pos.is_open:
                pos.sl_price = request.sl_price
                pos.tp_price = request.tp_price

        # Publish order update
        await self._event_bus.publish_immediate(
            OrderUpdateEvent(
                timestamp=self._current_time,
                symbol=symbol,
                order_id=order_id,
                client_order_id=client_order_id,
                status=OrderStatus.FILLED,
                side=request.side,
                order_type=OrderType.MARKET,
                price=fill_price,
                qty=request.qty,
                filled_qty=request.qty,
                avg_fill_price=fill_price,
                fee=fee,
            )
        )

        return OrderResponse(
            success=True,
            order_id=order_id,
            client_order_id=client_order_id,
            status=OrderStatus.FILLED,
        )

    async def _create_limit_order(
        self,
        request: OrderRequest,
        order_id: str,
        client_order_id: str,
    ) -> OrderResponse:
        """Create a pending limit order."""
        if request.price is None:
            return OrderResponse(
                success=False,
                order_id=order_id,
                client_order_id=client_order_id,
                message="Limit order requires price",
                status=OrderStatus.REJECTED,
            )

        pending = PendingOrder(
            order_id=order_id,
            client_order_id=client_order_id,
            symbol=request.symbol,
            side=request.side,
            order_type=OrderType.LIMIT,
            qty=request.qty,
            price=request.price,
            sl_price=request.sl_price,
            tp_price=request.tp_price,
            reduce_only=request.reduce_only,
            created_at=self._current_time,
        )

        self._pending_orders[order_id] = pending

        await self._event_bus.publish_immediate(
            OrderUpdateEvent(
                timestamp=self._current_time,
                symbol=request.symbol,
                order_id=order_id,
                client_order_id=client_order_id,
                status=OrderStatus.NEW,
                side=request.side,
                order_type=OrderType.LIMIT,
                price=request.price,
                qty=request.qty,
                filled_qty=Decimal("0"),
            )
        )

        return OrderResponse(
            success=True,
            order_id=order_id,
            client_order_id=client_order_id,
            status=OrderStatus.NEW,
        )

    async def _process_fill(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        fee: Decimal,
        order_id: str,
        client_order_id: str,
        reduce_only: bool = False,
        stop_order_type: str = "",
    ) -> None:
        """Process a fill and update position."""
        pos = self._positions.get(symbol)
        if pos is None:
            pos = SimulatedPosition(symbol=symbol)
            self._positions[symbol] = pos

        realized_pnl = Decimal("0")

        if not pos.is_open:
            # Opening new position
            if not reduce_only:
                pos.side = side
                pos.size = qty
                pos.entry_price = price
        elif pos.side == side:
            # Adding to position
            if not reduce_only:
                # Average entry price
                total_cost = pos.entry_price * pos.size + price * qty
                pos.size += qty
                pos.entry_price = total_cost / pos.size
        else:
            # Reducing or closing position
            close_qty = min(pos.size, qty)
            if pos.side == Side.BUY:
                realized_pnl = (price - pos.entry_price) * close_qty
            else:
                realized_pnl = (pos.entry_price - price) * close_qty

            pos.size -= close_qty
            pos.realized_pnl += realized_pnl

            # If fully closed, reset position
            if pos.size <= 0:
                pos.side = None
                pos.size = Decimal("0")
                pos.entry_price = Decimal("0")
                pos.sl_price = None
                pos.tp_price = None

            # If there's remaining qty and not reduce_only, open new position
            remaining = qty - close_qty
            if remaining > 0 and not reduce_only:
                pos.side = side
                pos.size = remaining
                pos.entry_price = price

        # Update balance
        self._balance -= fee
        self._balance += realized_pnl

        # Publish fill event
        await self._event_bus.publish_immediate(
            FillEvent(
                timestamp=self._current_time,
                symbol=symbol,
                order_id=order_id,
                client_order_id=client_order_id,
                trade_id=str(uuid4())[:8],
                side=side,
                price=price,
                qty=qty,
                fee=fee,
                realized_pnl=realized_pnl,
                stop_order_type=stop_order_type,
            )
        )

        logger.debug(
            f"Fill: {symbol} {side.value} {qty}@{price}, fee={fee}, pnl={realized_pnl}"
        )

    async def _close_position(self, symbol: str, reason: str = "") -> None:
        """Close a position at current market price."""
        pos = self._positions.get(symbol)
        if not pos or not pos.is_open:
            return

        prices = self._last_prices.get(symbol)
        if not prices:
            return

        bid, ask = prices
        close_side = pos.side.opposite
        close_price = ask if close_side == Side.BUY else bid
        close_price = close_price * (
            1 + self._costs.slippage_pct if close_side == Side.BUY
            else 1 - self._costs.slippage_pct
        )

        fee = close_price * pos.size * self._costs.fees_pct

        order_id = f"close_{symbol}_{reason}"

        await self._process_fill(
            symbol=symbol,
            side=close_side,
            qty=pos.size,
            price=close_price,
            fee=fee,
            order_id=order_id,
            client_order_id=order_id,
            reduce_only=True,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> OrderResponse:
        """Cancel a pending order."""
        if order_id not in self._pending_orders:
            return OrderResponse(
                success=False,
                order_id=order_id,
                message="Order not found",
                status=OrderStatus.REJECTED,
            )

        order = self._pending_orders.pop(order_id)

        await self._event_bus.publish_immediate(
            OrderUpdateEvent(
                timestamp=self._current_time,
                symbol=symbol,
                order_id=order_id,
                client_order_id=order.client_order_id,
                status=OrderStatus.CANCELLED,
                side=order.side,
                order_type=order.order_type,
                price=order.price,
                qty=order.qty,
                filled_qty=order.filled_qty,
            )
        )

        return OrderResponse(
            success=True,
            order_id=order_id,
            client_order_id=order.client_order_id,
            status=OrderStatus.CANCELLED,
        )

    async def cancel_all_orders(self, symbol: str | None = None) -> OrderResponse:
        """Cancel all pending orders."""
        to_cancel = [
            oid for oid, o in self._pending_orders.items()
            if symbol is None or o.symbol == symbol
        ]

        for order_id in to_cancel:
            order = self._pending_orders.pop(order_id)
            await self._event_bus.publish_immediate(
                OrderUpdateEvent(
                    timestamp=self._current_time,
                    symbol=order.symbol,
                    order_id=order_id,
                    client_order_id=order.client_order_id,
                    status=OrderStatus.CANCELLED,
                    side=order.side,
                    order_type=order.order_type,
                    price=order.price,
                    qty=order.qty,
                    filled_qty=order.filled_qty,
                )
            )

        return OrderResponse(
            success=True,
            message=f"Cancelled {len(to_cancel)} orders",
        )

    async def modify_order(
        self,
        symbol: str,
        order_id: str,
        qty: Decimal | None = None,
        price: Decimal | None = None,
    ) -> OrderResponse:
        """Modify a pending order."""
        if order_id not in self._pending_orders:
            return OrderResponse(
                success=False,
                order_id=order_id,
                message="Order not found",
            )

        order = self._pending_orders[order_id]
        if qty is not None:
            order.qty = qty
        if price is not None:
            order.price = price

        return OrderResponse(
            success=True,
            order_id=order_id,
            client_order_id=order.client_order_id,
        )

    async def set_sl_tp(
        self,
        symbol: str,
        sl_price: Decimal | None = None,
        tp_price: Decimal | None = None,
    ) -> OrderResponse:
        """Set SL/TP for a position."""
        pos = self._positions.get(symbol)
        if not pos or not pos.is_open:
            return OrderResponse(
                success=False,
                message="No open position",
            )

        pos.sl_price = sl_price
        pos.tp_price = tp_price

        logger.debug(f"Set SL/TP for {symbol}: SL={sl_price}, TP={tp_price}")

        return OrderResponse(
            success=True,
            message=f"SL={sl_price}, TP={tp_price}",
        )

    async def get_position(self, symbol: str) -> Position | None:
        """Get position for a symbol."""
        pos = self._positions.get(symbol)
        if not pos or not pos.is_open:
            return None

        prices = self._last_prices.get(symbol, (pos.entry_price, pos.entry_price))
        mark_price = (prices[0] + prices[1]) / 2

        if pos.side == Side.BUY:
            unrealized = (mark_price - pos.entry_price) * pos.size
        else:
            unrealized = (pos.entry_price - mark_price) * pos.size

        return Position(
            symbol=symbol,
            side=pos.side,
            size=pos.size,
            entry_price=pos.entry_price,
            mark_price=mark_price,
            unrealized_pnl=unrealized,
            leverage=self._leverage,
        )

    async def get_all_positions(self) -> list[Position]:
        """Get all open positions."""
        positions = []
        for symbol in self._positions:
            pos = await self.get_position(symbol)
            if pos:
                positions.append(pos)
        return positions

    async def get_balance(self, asset: str = "USDT") -> Balance:
        """Get account balance."""
        # Calculate unrealized PnL
        unrealized = Decimal("0")
        for symbol, pos in self._positions.items():
            if pos.is_open:
                position = await self.get_position(symbol)
                if position:
                    unrealized += position.unrealized_pnl

        return Balance(
            asset=asset,
            available=self._balance,
            total=self._balance + unrealized,
            unrealized_pnl=unrealized,
        )

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """Get open orders."""
        orders = []
        for order_id, pending in self._pending_orders.items():
            if symbol is None or pending.symbol == symbol:
                orders.append(
                    Order(
                        order_id=order_id,
                        client_order_id=pending.client_order_id,
                        symbol=pending.symbol,
                        side=pending.side,
                        order_type=pending.order_type,
                        status=OrderStatus.NEW,
                        price=pending.price,
                        qty=pending.qty,
                        filled_qty=pending.filled_qty,
                        avg_fill_price=Decimal("0"),
                        sl_price=pending.sl_price,
                        tp_price=pending.tp_price,
                        reduce_only=pending.reduce_only,
                    )
                )
        return orders

    async def get_order(self, symbol: str, order_id: str) -> Order | None:
        """Get specific order."""
        pending = self._pending_orders.get(order_id)
        if pending and pending.symbol == symbol:
            return Order(
                order_id=order_id,
                client_order_id=pending.client_order_id,
                symbol=pending.symbol,
                side=pending.side,
                order_type=pending.order_type,
                status=OrderStatus.NEW,
                price=pending.price,
                qty=pending.qty,
                filled_qty=pending.filled_qty,
                avg_fill_price=Decimal("0"),
            )
        return None

    async def process_orderbook(self, event: OrderBookEvent) -> None:
        """
        Process orderbook update - check limit orders and SL/TP.

        This should be called by the engine for each orderbook event.
        """
        self._current_time = event.timestamp
        self._last_prices[event.symbol] = (event.bid_price, event.ask_price)

        # Check pending limit orders
        await self._check_limit_orders(event)

        # Check SL/TP
        await self._check_sl_tp(event)

    async def _check_limit_orders(self, event: OrderBookEvent) -> None:
        """Check if any limit orders should be filled."""
        to_fill = []

        for order_id, order in self._pending_orders.items():
            if order.symbol != event.symbol:
                continue

            # Buy limit fills when ask <= order price
            if order.side == Side.BUY and event.ask_price <= order.price:
                to_fill.append((order_id, event.ask_price))
            # Sell limit fills when bid >= order price
            elif order.side == Side.SELL and event.bid_price >= order.price:
                to_fill.append((order_id, event.bid_price))

        for order_id, fill_price in to_fill:
            order = self._pending_orders.pop(order_id)
            fee = fill_price * order.qty * self._costs.fees_pct

            await self._process_fill(
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                price=fill_price,
                fee=fee,
                order_id=order_id,
                client_order_id=order.client_order_id,
                reduce_only=order.reduce_only,
            )

            # Set SL/TP if provided
            if order.sl_price or order.tp_price:
                pos = self._positions.get(order.symbol)
                if pos and pos.is_open:
                    pos.sl_price = order.sl_price
                    pos.tp_price = order.tp_price

            await self._event_bus.publish_immediate(
                OrderUpdateEvent(
                    timestamp=self._current_time,
                    symbol=order.symbol,
                    order_id=order_id,
                    client_order_id=order.client_order_id,
                    status=OrderStatus.FILLED,
                    side=order.side,
                    order_type=order.order_type,
                    price=fill_price,
                    qty=order.qty,
                    filled_qty=order.qty,
                    avg_fill_price=fill_price,
                    fee=fee,
                )
            )

    async def _check_sl_tp(self, event: OrderBookEvent) -> None:
        """Check if SL/TP should be triggered."""
        pos = self._positions.get(event.symbol)
        if not pos or not pos.is_open:
            return

        triggered = None
        trigger_price = None

        if pos.side == Side.BUY:
            # Long position: SL triggers on bid, TP triggers on bid
            if pos.sl_price and event.bid_price <= pos.sl_price:
                triggered = "sl"
                trigger_price = event.bid_price
            elif pos.tp_price and event.bid_price >= pos.tp_price:
                triggered = "tp"
                trigger_price = event.bid_price
        else:
            # Short position: SL triggers on ask, TP triggers on ask
            if pos.sl_price and event.ask_price >= pos.sl_price:
                triggered = "sl"
                trigger_price = event.ask_price
            elif pos.tp_price and event.ask_price <= pos.tp_price:
                triggered = "tp"
                trigger_price = event.ask_price

        if triggered and trigger_price:
            close_side = pos.side.opposite
            fee = trigger_price * pos.size * self._costs.fees_pct

            # Map to Bybit-style stop_order_type for consistency
            stop_order_type = "StopLoss" if triggered == "sl" else "TakeProfit"

            logger.info(
                f"{triggered.upper()} triggered for {event.symbol} at {trigger_price}"
            )

            await self._process_fill(
                symbol=event.symbol,
                side=close_side,
                qty=pos.size,
                price=trigger_price,
                fee=fee,
                order_id=f"{triggered}_{event.symbol}",
                client_order_id=f"{triggered}_{event.symbol}",
                reduce_only=True,
                stop_order_type=stop_order_type,
            )

    def update_time(self, timestamp: datetime) -> None:
        """Update current simulation time."""
        self._current_time = timestamp

    @property
    def current_balance(self) -> Decimal:
        """Get current balance."""
        return self._balance

    @property
    def equity(self) -> Decimal:
        """Get current equity (balance + unrealized PnL)."""
        unrealized = Decimal("0")
        for symbol, pos in self._positions.items():
            if pos.is_open:
                prices = self._last_prices.get(symbol)
                if prices:
                    mid = (prices[0] + prices[1]) / 2
                    if pos.side == Side.BUY:
                        unrealized += (mid - pos.entry_price) * pos.size
                    else:
                        unrealized += (pos.entry_price - mid) * pos.size
        return self._balance + unrealized
