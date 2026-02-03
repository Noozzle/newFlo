"""Bybit client for fetching closed PnL data."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger
from pybit.unified_trading import HTTP

from app.ui.models import ClosedPnLRecord

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
        """Get all trades for a specific day."""
        if isinstance(target_date, date):
            start = datetime(target_date.year, target_date.month, target_date.day)
        else:
            start = target_date

        end = start + timedelta(days=1)
        return self.get_closed_pnl(start, end)
