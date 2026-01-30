"""Bybit exchange adapter for live trading."""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
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
from app.config import BybitConfig
from app.core.events import (
    BalanceEvent,
    FillEvent,
    OrderStatus,
    OrderType,
    OrderUpdateEvent,
    PositionEvent,
    Side,
)

if TYPE_CHECKING:
    from app.core.event_bus import EventBus

try:
    from pybit.unified_trading import HTTP, WebSocket
    PYBIT_AVAILABLE = True
except ImportError:
    PYBIT_AVAILABLE = False
    HTTP = None
    WebSocket = None


class BybitAdapter(ExchangeAdapter):
    """
    Bybit exchange adapter using pybit SDK.

    Features:
    - REST API for order management
    - Private WebSocket for real-time updates
    - Automatic reconnection
    - Rate limit handling
    """

    def __init__(
        self,
        event_bus: "EventBus",
        config: BybitConfig,
    ) -> None:
        """
        Initialize Bybit adapter.

        Args:
            event_bus: Event bus for publishing events
            config: Bybit configuration
        """
        super().__init__(event_bus)
        self._config = config
        self._client: Any = None
        self._ws: Any = None
        self._reconnect_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None  # Store loop for thread-safe publish

        if not PYBIT_AVAILABLE:
            raise ImportError("pybit is required for Bybit adapter. Install with: pip install pybit")

        if not config.api_key or not config.api_secret:
            raise ValueError("Bybit API key and secret are required for live trading")

    async def start(self) -> None:
        """Start the adapter and connect to Bybit."""
        self._running = True
        self._loop = asyncio.get_running_loop()  # Store loop for thread-safe callbacks

        # Initialize REST client
        self._client = HTTP(
            testnet=self._config.testnet,
            api_key=self._config.api_key,
            api_secret=self._config.api_secret,
            recv_window=self._config.recv_window,
        )

        # Initialize private WebSocket only if we have API keys
        if self._config.api_key and self._config.api_secret:
            try:
                self._ws = WebSocket(
                    testnet=self._config.testnet,
                    channel_type="private",
                    api_key=self._config.api_key,
                    api_secret=self._config.api_secret,
                )

                # Subscribe to private channels
                self._ws.order_stream(callback=self._handle_order)
                self._ws.execution_stream(callback=self._handle_execution)
                self._ws.position_stream(callback=self._handle_position)
                self._ws.wallet_stream(callback=self._handle_wallet)
                logger.info(f"Private WebSocket connected (testnet={self._config.testnet})")
            except Exception as e:
                logger.error(f"Failed to connect private WebSocket: {e}")
                self._ws = None
        else:
            logger.warning("No API keys - private WebSocket disabled, trading not available")
            self._ws = None

        logger.info(f"Bybit adapter started (testnet={self._config.testnet})")

    async def stop(self) -> None:
        """Stop the adapter."""
        self._running = False

        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            try:
                self._ws.exit()
            except Exception as e:
                logger.debug(f"Error closing WebSocket: {e}")

        logger.info("Bybit adapter stopped")

    # Quantity step sizes for common symbols (Bybit linear perpetuals)
    # Check https://api.bybit.com/v5/market/instruments-info?category=linear&symbol=XXXUSDT
    QTY_STEPS = {
        "BTCUSDT": Decimal("0.001"),
        "ETHUSDT": Decimal("0.01"),
        "SOLUSDT": Decimal("0.1"),
        "SUIUSDT": Decimal("10"),     # SUI requires multiples of 10
        "XRPUSDT": Decimal("1"),      # XRP requires whole numbers
        "DOGEUSDT": Decimal("1"),
        "BNBUSDT": Decimal("0.01"),
        "ADAUSDT": Decimal("1"),
        "MATICUSDT": Decimal("0.1"),
        "LINKUSDT": Decimal("0.01"),
    }

    def _round_qty(self, symbol: str, qty: Decimal) -> Decimal:
        """Round quantity to symbol's step size."""
        step = self.QTY_STEPS.get(symbol, Decimal("0.01"))
        # Round down to avoid exceeding available balance
        return (qty // step) * step

    # Price tick sizes for common symbols
    PRICE_TICKS = {
        "BTCUSDT": Decimal("0.1"),
        "ETHUSDT": Decimal("0.01"),
        "SOLUSDT": Decimal("0.001"),
        "SUIUSDT": Decimal("0.0001"),
        "XRPUSDT": Decimal("0.0001"),
        "DOGEUSDT": Decimal("0.00001"),
        "BNBUSDT": Decimal("0.01"),
        "ADAUSDT": Decimal("0.0001"),
        "MATICUSDT": Decimal("0.0001"),
        "LINKUSDT": Decimal("0.001"),
    }

    def _round_price(self, symbol: str, price: Decimal) -> Decimal:
        """Round price to symbol's tick size."""
        tick = self.PRICE_TICKS.get(symbol, Decimal("0.0001"))
        return (price / tick).quantize(Decimal("1")) * tick

    async def place_order(self, request: OrderRequest) -> OrderResponse:
        """Place an order on Bybit."""
        try:
            # Round qty to symbol's step size
            rounded_qty = self._round_qty(request.symbol, request.qty)
            if rounded_qty <= 0:
                return OrderResponse(
                    success=False,
                    message=f"Qty too small after rounding: {request.qty} -> {rounded_qty}",
                    status=OrderStatus.REJECTED,
                )

            params = {
                "category": self._config.category,
                "symbol": request.symbol,
                "side": "Buy" if request.side == Side.BUY else "Sell",
                "orderType": "Market" if request.order_type == OrderType.MARKET else "Limit",
                "qty": str(rounded_qty),
                "timeInForce": request.time_in_force,
            }

            if request.order_type == OrderType.LIMIT and request.price:
                params["price"] = str(request.price)

            if request.reduce_only:
                params["reduceOnly"] = True

            if request.client_order_id:
                params["orderLinkId"] = request.client_order_id

            # Add SL/TP if provided (round to tick size)
            if request.sl_price:
                params["stopLoss"] = str(self._round_price(request.symbol, request.sl_price))
            if request.tp_price:
                params["takeProfit"] = str(self._round_price(request.symbol, request.tp_price))

            logger.debug(f"Placing order: {params}")

            result = self._client.place_order(**params)

            if result.get("retCode") == 0:
                order_result = result.get("result", {})
                return OrderResponse(
                    success=True,
                    order_id=order_result.get("orderId", ""),
                    client_order_id=order_result.get("orderLinkId", ""),
                    status=OrderStatus.NEW,
                )
            else:
                return OrderResponse(
                    success=False,
                    message=result.get("retMsg", "Unknown error"),
                    status=OrderStatus.REJECTED,
                )

        except Exception as e:
            logger.error(f"Error placing order: {e}")
            return OrderResponse(
                success=False,
                message=str(e),
                status=OrderStatus.REJECTED,
            )

    async def cancel_order(self, symbol: str, order_id: str) -> OrderResponse:
        """Cancel an order on Bybit."""
        try:
            result = self._client.cancel_order(
                category=self._config.category,
                symbol=symbol,
                orderId=order_id,
            )

            if result.get("retCode") == 0:
                return OrderResponse(
                    success=True,
                    order_id=order_id,
                    status=OrderStatus.CANCELLED,
                )
            else:
                return OrderResponse(
                    success=False,
                    order_id=order_id,
                    message=result.get("retMsg", "Unknown error"),
                )

        except Exception as e:
            logger.error(f"Error canceling order: {e}")
            return OrderResponse(
                success=False,
                order_id=order_id,
                message=str(e),
            )

    async def cancel_all_orders(self, symbol: str | None = None) -> OrderResponse:
        """Cancel all orders."""
        try:
            params = {"category": self._config.category}
            if symbol:
                params["symbol"] = symbol

            result = self._client.cancel_all_orders(**params)

            if result.get("retCode") == 0:
                return OrderResponse(
                    success=True,
                    message="All orders cancelled",
                )
            else:
                return OrderResponse(
                    success=False,
                    message=result.get("retMsg", "Unknown error"),
                )

        except Exception as e:
            logger.error(f"Error canceling all orders: {e}")
            return OrderResponse(success=False, message=str(e))

    async def modify_order(
        self,
        symbol: str,
        order_id: str,
        qty: Decimal | None = None,
        price: Decimal | None = None,
    ) -> OrderResponse:
        """Modify an existing order."""
        try:
            params = {
                "category": self._config.category,
                "symbol": symbol,
                "orderId": order_id,
            }

            if qty:
                params["qty"] = str(qty)
            if price:
                params["price"] = str(price)

            result = self._client.amend_order(**params)

            if result.get("retCode") == 0:
                return OrderResponse(
                    success=True,
                    order_id=order_id,
                )
            else:
                return OrderResponse(
                    success=False,
                    order_id=order_id,
                    message=result.get("retMsg", "Unknown error"),
                )

        except Exception as e:
            logger.error(f"Error modifying order: {e}")
            return OrderResponse(success=False, order_id=order_id, message=str(e))

    async def set_sl_tp(
        self,
        symbol: str,
        sl_price: Decimal | None = None,
        tp_price: Decimal | None = None,
    ) -> OrderResponse:
        """Set stop loss and take profit for a position."""
        try:
            params = {
                "category": self._config.category,
                "symbol": symbol,
                "positionIdx": 0,  # One-way mode
            }

            if sl_price:
                params["stopLoss"] = str(sl_price)
            if tp_price:
                params["takeProfit"] = str(tp_price)

            result = self._client.set_trading_stop(**params)

            if result.get("retCode") == 0:
                return OrderResponse(
                    success=True,
                    message=f"SL={sl_price}, TP={tp_price}",
                )
            else:
                return OrderResponse(
                    success=False,
                    message=result.get("retMsg", "Unknown error"),
                )

        except Exception as e:
            logger.error(f"Error setting SL/TP: {e}")
            return OrderResponse(success=False, message=str(e))

    async def get_position(self, symbol: str) -> Position | None:
        """Get current position for a symbol."""
        try:
            result = self._client.get_positions(
                category=self._config.category,
                symbol=symbol,
            )

            if result.get("retCode") != 0:
                return None

            positions = result.get("result", {}).get("list", [])
            if not positions:
                return None

            pos = positions[0]
            size = Decimal(pos.get("size") or "0")

            if size == 0:
                return None

            side_str = pos.get("side", "")
            side = Side.BUY if side_str == "Buy" else Side.SELL if side_str == "Sell" else None
            liq_price_val = pos.get("liqPrice")

            return Position(
                symbol=symbol,
                side=side,
                size=size,
                entry_price=Decimal(pos.get("avgPrice") or "0"),
                mark_price=Decimal(pos.get("markPrice") or "0"),
                unrealized_pnl=Decimal(pos.get("unrealisedPnl") or "0"),
                leverage=int(pos.get("leverage") or 1),
                liq_price=Decimal(liq_price_val) if liq_price_val and liq_price_val != "" else None,
            )

        except Exception as e:
            logger.error(f"Error getting position: {e}")
            return None

    async def get_all_positions(self) -> list[Position]:
        """Get all open positions."""
        try:
            result = self._client.get_positions(
                category=self._config.category,
                settleCoin="USDT",
            )

            if result.get("retCode") != 0:
                return []

            positions = []
            for pos in result.get("result", {}).get("list", []):
                size = Decimal(pos.get("size") or "0")
                if size == 0:
                    continue

                side_str = pos.get("side", "")
                side = Side.BUY if side_str == "Buy" else Side.SELL if side_str == "Sell" else None

                positions.append(Position(
                    symbol=pos.get("symbol", ""),
                    side=side,
                    size=size,
                    entry_price=Decimal(pos.get("avgPrice") or "0"),
                    mark_price=Decimal(pos.get("markPrice") or "0"),
                    unrealized_pnl=Decimal(pos.get("unrealisedPnl") or "0"),
                    leverage=int(pos.get("leverage") or 1),
                ))

            return positions

        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []

    async def get_balance(self, asset: str = "USDT") -> Balance:
        """Get account balance."""
        try:
            result = self._client.get_wallet_balance(
                accountType="UNIFIED",
                coin=asset,
            )

            if result.get("retCode") != 0:
                return Balance(asset=asset, available=Decimal("0"), total=Decimal("0"))

            coins = result.get("result", {}).get("list", [{}])[0].get("coin", [])
            for coin in coins:
                if coin.get("coin") == asset:
                    # Handle empty strings from API
                    return Balance(
                        asset=asset,
                        available=Decimal(coin.get("availableToWithdraw") or "0"),
                        total=Decimal(coin.get("walletBalance") or "0"),
                        unrealized_pnl=Decimal(coin.get("unrealisedPnl") or "0"),
                    )

            return Balance(asset=asset, available=Decimal("0"), total=Decimal("0"))

        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return Balance(asset=asset, available=Decimal("0"), total=Decimal("0"))

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """Get open orders."""
        try:
            params = {"category": self._config.category}
            if symbol:
                params["symbol"] = symbol
            else:
                # Bybit requires at least one of: symbol, settleCoin, or baseCoin
                params["settleCoin"] = "USDT"

            result = self._client.get_open_orders(**params)

            if result.get("retCode") != 0:
                return []

            orders = []
            for order_data in result.get("result", {}).get("list", []):
                orders.append(self._parse_order(order_data))

            return orders

        except Exception as e:
            logger.error(f"Error getting open orders: {e}")
            return []

    async def get_order(self, symbol: str, order_id: str) -> Order | None:
        """Get specific order by ID."""
        try:
            result = self._client.get_order_history(
                category=self._config.category,
                symbol=symbol,
                orderId=order_id,
            )

            if result.get("retCode") != 0:
                return None

            orders = result.get("result", {}).get("list", [])
            if not orders:
                return None

            return self._parse_order(orders[0])

        except Exception as e:
            logger.error(f"Error getting order: {e}")
            return None

    def _parse_order(self, data: dict[str, Any]) -> Order:
        """Parse order data from API response."""
        status_map = {
            "New": OrderStatus.NEW,
            "PartiallyFilled": OrderStatus.PARTIALLY_FILLED,
            "Filled": OrderStatus.FILLED,
            "Cancelled": OrderStatus.CANCELLED,
            "Rejected": OrderStatus.REJECTED,
        }

        # Helper to safely parse decimal (Bybit sometimes returns empty strings)
        def safe_decimal(value: Any, default: str = "0") -> Decimal:
            if value is None or value == "":
                return Decimal(default)
            return Decimal(str(value))

        sl_val = data.get("stopLoss")
        tp_val = data.get("takeProfit")

        return Order(
            order_id=data.get("orderId", ""),
            client_order_id=data.get("orderLinkId", ""),
            symbol=data.get("symbol", ""),
            side=Side.BUY if data.get("side") == "Buy" else Side.SELL,
            order_type=OrderType.MARKET if data.get("orderType") == "Market" else OrderType.LIMIT,
            status=status_map.get(data.get("orderStatus", ""), OrderStatus.NEW),
            price=safe_decimal(data.get("price")),
            qty=safe_decimal(data.get("qty")),
            filled_qty=safe_decimal(data.get("cumExecQty")),
            avg_fill_price=safe_decimal(data.get("avgPrice")),
            sl_price=safe_decimal(sl_val) if sl_val and sl_val != "" else None,
            tp_price=safe_decimal(tp_val) if tp_val and tp_val != "" else None,
            reduce_only=data.get("reduceOnly", False),
        )

    def _handle_order(self, message: dict[str, Any]) -> None:
        """Handle order update from WebSocket."""
        try:
            data = message.get("data", [])
            for order_data in data:
                status_map = {
                    "New": OrderStatus.NEW,
                    "PartiallyFilled": OrderStatus.PARTIALLY_FILLED,
                    "Filled": OrderStatus.FILLED,
                    "Cancelled": OrderStatus.CANCELLED,
                    "Rejected": OrderStatus.REJECTED,
                }

                event = OrderUpdateEvent(
                    timestamp=datetime.utcnow(),
                    symbol=order_data.get("symbol", ""),
                    order_id=order_data.get("orderId", ""),
                    client_order_id=order_data.get("orderLinkId", ""),
                    status=status_map.get(order_data.get("orderStatus", ""), OrderStatus.NEW),
                    side=Side.BUY if order_data.get("side") == "Buy" else Side.SELL,
                    order_type=OrderType.MARKET if order_data.get("orderType") == "Market" else OrderType.LIMIT,
                    price=Decimal(order_data.get("price") or "0"),
                    qty=Decimal(order_data.get("qty") or "0"),
                    filled_qty=Decimal(order_data.get("cumExecQty") or "0"),
                    avg_fill_price=Decimal(order_data.get("avgPrice") or "0"),
                )

                # Publish to event bus (thread-safe from WS callback)
                if self._loop:
                    asyncio.run_coroutine_threadsafe(self._event_bus.publish_immediate(event), self._loop)

        except Exception as e:
            logger.error(f"Error handling order update: {e}")

    def _handle_execution(self, message: dict[str, Any]) -> None:
        """Handle execution/fill from WebSocket."""
        try:
            data = message.get("data", [])
            for exec_data in data:
                # Bybit V5 stopOrderType: "StopLoss", "TakeProfit", "TrailingStop", ""
                stop_order_type = exec_data.get("stopOrderType", "")

                event = FillEvent(
                    timestamp=datetime.utcnow(),
                    symbol=exec_data.get("symbol", ""),
                    order_id=exec_data.get("orderId", ""),
                    client_order_id=exec_data.get("orderLinkId", ""),
                    trade_id=exec_data.get("execId", ""),
                    side=Side.BUY if exec_data.get("side") == "Buy" else Side.SELL,
                    price=Decimal(exec_data.get("execPrice") or "0"),
                    qty=Decimal(exec_data.get("execQty") or "0"),
                    fee=Decimal(exec_data.get("execFee") or "0"),
                    fee_asset=exec_data.get("feeCurrency", "USDT"),
                    is_maker=exec_data.get("isMaker", False),
                    stop_order_type=stop_order_type,
                )

                logger.debug(
                    f"Execution: {exec_data.get('symbol')} {exec_data.get('side')} "
                    f"qty={exec_data.get('execQty')} price={exec_data.get('execPrice')} "
                    f"stopOrderType={stop_order_type}"
                )

                # Publish to event bus (thread-safe from WS callback)
                if self._loop:
                    asyncio.run_coroutine_threadsafe(self._event_bus.publish_immediate(event), self._loop)

        except Exception as e:
            logger.error(f"Error handling execution: {e}")

    def _handle_position(self, message: dict[str, Any]) -> None:
        """Handle position update from WebSocket."""
        try:
            data = message.get("data", [])
            for pos_data in data:
                size = Decimal(pos_data.get("size") or "0")
                side_str = pos_data.get("side", "")
                side = Side.BUY if side_str == "Buy" else Side.SELL if side_str == "Sell" else None

                event = PositionEvent(
                    timestamp=datetime.utcnow(),
                    symbol=pos_data.get("symbol", ""),
                    side=side if size > 0 else None,
                    size=size,
                    entry_price=Decimal(pos_data.get("avgPrice") or "0"),
                    mark_price=Decimal(pos_data.get("markPrice") or "0"),
                    unrealized_pnl=Decimal(pos_data.get("unrealisedPnl") or "0"),
                    leverage=int(pos_data.get("leverage") or 1),
                )

                # Publish to event bus (thread-safe from WS callback)
                if self._loop:
                    asyncio.run_coroutine_threadsafe(self._event_bus.publish_immediate(event), self._loop)

        except Exception as e:
            logger.error(f"Error handling position update: {e}")

    def _handle_wallet(self, message: dict[str, Any]) -> None:
        """Handle wallet update from WebSocket."""
        try:
            data = message.get("data", [])
            for wallet_data in data:
                for coin_data in wallet_data.get("coin", []):
                    event = BalanceEvent(
                        timestamp=datetime.utcnow(),
                        symbol="",
                        asset=coin_data.get("coin", ""),
                        available=Decimal(coin_data.get("availableToWithdraw") or "0"),
                        total=Decimal(coin_data.get("walletBalance") or "0"),
                        unrealized_pnl=Decimal(coin_data.get("unrealisedPnl") or "0"),
                    )

                    # Publish to event bus (thread-safe from WS callback)
                    if self._loop:
                        asyncio.run_coroutine_threadsafe(self._event_bus.publish_immediate(event), self._loop)

        except Exception as e:
            logger.error(f"Error handling wallet update: {e}")
