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
                            f"PnL={record.closed_pnl} @ {record.closed_time}"
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

    def get_day_trades(self, target_date: date) -> list[ClosedPnLRecord]:
        """Get all trades for a specific day (by entry time)."""
        if isinstance(target_date, date):
            start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
        else:
            start = target_date

        end = start + timedelta(days=1)
        return self.get_closed_pnl(start, end)

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
