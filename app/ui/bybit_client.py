"""Bybit client for fetching closed PnL data."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from loguru import logger
from pybit.unified_trading import HTTP

from app.ui.models import ClosedPnLRecord, OpenPosition

if TYPE_CHECKING:
    from app.config import BybitConfig


class BybitPnLClient:
    """Fetch closed PnL records from Bybit."""

    def __init__(self, config: BybitConfig) -> None:
        self._config = config
        self._client = HTTP(
            testnet=config.testnet,
            api_key=config.api_key,
            api_secret=config.api_secret,
        )
        logger.info(
            f"BybitPnLClient initialized: testnet={config.testnet}, "
            f"category={config.category}"
        )

    def get_closed_pnl(
        self,
        start_date: datetime,
        end_date: datetime,
        symbol: str | None = None,
    ) -> list[ClosedPnLRecord]:
        """
        Fetch closed PnL records for date range.

        Handles 7-day chunking and pagination as required by Bybit API.

        Args:
            start_date: Start of date range
            end_date: End of date range
            symbol: Optional symbol filter

        Returns:
            List of closed PnL records
        """
        records: list[ClosedPnLRecord] = []
        current_start = start_date

        logger.info(
            f"Fetching closed PnL: {start_date} to {end_date}, "
            f"symbol={symbol or 'ALL'}"
        )

        while current_start < end_date:
            # Bybit allows max 7 days per request
            chunk_end = min(current_start + timedelta(days=7), end_date)

            logger.debug(f"Fetching chunk: {current_start} to {chunk_end}")

            cursor: str | None = None
            page = 0
            while True:
                page += 1
                params: dict = {
                    "category": self._config.category,
                    "startTime": int(current_start.timestamp() * 1000),
                    "endTime": int(chunk_end.timestamp() * 1000),
                    "limit": 100,
                }
                if symbol:
                    params["symbol"] = symbol
                if cursor:
                    params["cursor"] = cursor

                logger.debug(f"API request page {page}: {params}")

                try:
                    result = self._client.get_closed_pnl(**params)
                except Exception as e:
                    logger.error(f"Error fetching closed PnL: {e}")
                    break

                ret_code = result.get("retCode")
                if ret_code != 0:
                    logger.error(
                        f"Bybit API error: code={ret_code}, msg={result.get('retMsg')}"
                    )
                    break

                data = result.get("result", {})
                items = data.get("list", [])

                logger.debug(f"Page {page}: received {len(items)} items")

                for item in items:
                    try:
                        record = ClosedPnLRecord.from_api(item)
                        records.append(record)
                        logger.debug(
                            f"  Trade: {record.symbol} {record.side} "
                            f"PnL={record.closed_pnl} @ {record.exit_time}"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to parse PnL record: {e}, data={item}")
                        continue

                cursor = data.get("nextPageCursor")
                if not cursor:
                    break

            current_start = chunk_end

        logger.info(f"Total fetched: {len(records)} closed PnL records")
        return records

    def get_month_trades(self, year: int, month: int) -> list[ClosedPnLRecord]:
        """Get all trades for a specific month.

        Note: Bybit API filters by exit time (updatedTime), so we fetch
        the entire month. Filtering by entry date should be done by caller
        with proper timezone handling.
        """
        month_start = datetime(year, month, 1, tzinfo=timezone.utc)
        if month == 12:
            month_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            month_end = datetime(year, month + 1, 1, tzinfo=timezone.utc)

        return self.get_closed_pnl(month_start, month_end)

    def get_day_trades(self, target_date: date) -> list[ClosedPnLRecord]:
        """Get all trades for a specific day (by entry time in UTC).

        Note: This returns all trades for the month. For proper timezone
        handling, use get_month_trades and filter in the caller.
        """
        return self.get_month_trades(target_date.year, target_date.month)

    def get_open_positions(self) -> list[OpenPosition]:
        """Get all currently open positions."""
        positions: list[OpenPosition] = []

        logger.debug("Fetching open positions from Bybit...")

        try:
            result = self._client.get_positions(
                category=self._config.category,
                settleCoin="USDT",
            )
        except Exception as e:
            logger.error(f"Error fetching open positions: {e}")
            return positions

        ret_code = result.get("retCode")
        if ret_code != 0:
            logger.error(f"Bybit API error: code={ret_code}, msg={result.get('retMsg')}")
            return positions

        data = result.get("result", {})
        items = data.get("list", [])

        logger.debug(f"Received {len(items)} position entries from API")

        for item in items:
            try:
                size = Decimal(item.get("size", "0"))
                logger.debug(f"Position: {item.get('symbol')} size={size} side={item.get('side')}")
                if size > 0:  # Only include positions with actual size
                    positions.append(OpenPosition.from_api(item))
            except Exception as e:
                logger.warning(f"Failed to parse position: {e}, data={item}")
                continue

        logger.info(f"Fetched {len(positions)} open positions")
        return positions

    def get_ticker(self, symbol: str) -> dict | None:
        """Get current ticker for a symbol."""
        try:
            result = self._client.get_tickers(
                category=self._config.category,
                symbol=symbol,
            )
            if result.get("retCode") == 0:
                items = result.get("result", {}).get("list", [])
                if items:
                    return items[0]
        except Exception as e:
            logger.error(f"Error fetching ticker for {symbol}: {e}")
        return None

    def get_klines(
        self,
        symbol: str,
        interval: str = "1",  # 1 minute
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Get kline/candlestick data for charting with pagination."""
        all_klines = []

        # Bybit max is 1000 per request
        batch_limit = min(limit, 1000)
        current_end = end_time

        # Interval in milliseconds for pagination
        interval_ms = {
            "1": 60000, "3": 180000, "5": 300000, "15": 900000,
            "30": 1800000, "60": 3600000, "120": 7200000,
            "240": 14400000, "360": 21600000, "720": 43200000,
            "D": 86400000, "W": 604800000,
        }.get(interval, 60000)

        remaining = limit

        while remaining > 0:
            params: dict = {
                "category": self._config.category,
                "symbol": symbol,
                "interval": interval,
                "limit": min(batch_limit, remaining),
            }

            if start_time:
                params["start"] = int(start_time.timestamp() * 1000)
            if current_end:
                params["end"] = int(current_end.timestamp() * 1000)

            try:
                result = self._client.get_kline(**params)

                if result.get("retCode") != 0:
                    logger.error(f"Bybit API error fetching klines: {result.get('retMsg')}")
                    break

                data = result.get("result", {})
                items = data.get("list", [])

                if not items:
                    break

                # Bybit returns newest first
                batch_klines = []
                for item in items:
                    # item format: [startTime, open, high, low, close, volume, turnover]
                    batch_klines.append({
                        "time": int(item[0]) // 1000,  # Convert to seconds
                        "open": float(item[1]),
                        "high": float(item[2]),
                        "low": float(item[3]),
                        "close": float(item[4]),
                        "volume": float(item[5]),
                    })

                # Prepend to all_klines (since we're going backwards in time)
                all_klines = batch_klines + all_klines
                remaining -= len(items)

                # If we got less than requested, we've reached the start
                if len(items) < batch_limit:
                    break

                # Move end time to before the oldest candle we got
                oldest_time_ms = int(items[-1][0])
                current_end = datetime.fromtimestamp((oldest_time_ms - interval_ms) / 1000, tz=timezone.utc)

                # Safety check: don't go before start_time
                if start_time and current_end < start_time:
                    break

            except Exception as e:
                logger.error(f"Error fetching klines: {e}")
                break

        # Sort by time (oldest first) and remove duplicates
        all_klines.sort(key=lambda x: x["time"])

        # Remove duplicates based on time
        seen_times = set()
        unique_klines = []
        for k in all_klines:
            if k["time"] not in seen_times:
                seen_times.add(k["time"])
                unique_klines.append(k)

        return unique_klines

    def get_trade_tpsl(
        self,
        symbol: str,
        entry_time: datetime,
        exit_time: datetime,
    ) -> dict:
        """
        Try to find TP/SL for a closed trade by looking at order history.

        Returns dict with 'take_profit' and 'stop_loss' prices if found.
        """
        result = {
            "take_profit": None,
            "stop_loss": None,
        }

        # Search window: from entry to exit + some buffer
        start_ms = int(entry_time.timestamp() * 1000)
        end_ms = int(exit_time.timestamp() * 1000) + 60000  # +1 min buffer

        try:
            # Get order history for this symbol
            response = self._client.get_order_history(
                category=self._config.category,
                symbol=symbol,
                startTime=start_ms,
                endTime=end_ms,
                limit=50,
            )

            if response.get("retCode") != 0:
                logger.warning(f"Failed to get order history: {response.get('retMsg')}")
                return result

            orders = response.get("result", {}).get("list", [])

            for order in orders:
                stop_order_type = order.get("stopOrderType", "")
                order_status = order.get("orderStatus", "")
                trigger_price = order.get("triggerPrice", "")

                # Look for TP/SL orders (filled, cancelled, or triggered)
                if stop_order_type == "TakeProfit" and trigger_price:
                    result["take_profit"] = float(trigger_price)
                    logger.debug(f"Found TP: {trigger_price} for {symbol}")
                elif stop_order_type == "StopLoss" and trigger_price:
                    result["stop_loss"] = float(trigger_price)
                    logger.debug(f"Found SL: {trigger_price} for {symbol}")

                # Also check position TP/SL from regular orders
                if order.get("takeProfit"):
                    result["take_profit"] = float(order["takeProfit"])
                if order.get("stopLoss"):
                    result["stop_loss"] = float(order["stopLoss"])

        except Exception as e:
            logger.error(f"Error fetching TP/SL for {symbol}: {e}")

        return result

    def get_wallet_balance(self) -> dict:
        """Get wallet balance."""
        try:
            result = self._client.get_wallet_balance(
                accountType="UNIFIED",
            )
            if result.get("retCode") == 0:
                accounts = result.get("result", {}).get("list", [])
                if accounts:
                    account = accounts[0]
                    total_equity = Decimal(account.get("totalEquity", "0") or "0")
                    available = Decimal(account.get("totalAvailableBalance", "0") or "0")

                    # Get USDT specifically
                    coins = account.get("coin", [])
                    usdt_balance = Decimal("0")
                    for coin in coins:
                        if coin.get("coin") == "USDT":
                            usdt_balance = Decimal(coin.get("walletBalance", "0") or "0")
                            break

                    return {
                        "total_equity": total_equity,
                        "available": available,
                        "usdt_balance": usdt_balance,
                    }
        except Exception as e:
            logger.error(f"Error fetching wallet balance: {e}")
        return {"total_equity": Decimal("0"), "available": Decimal("0"), "usdt_balance": Decimal("0")}
