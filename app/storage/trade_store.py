"""Trade journal storage (SQLite + CSV)."""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

from app.core.events import FillEvent, Side
from app.trading.signals import Trade


class TradeStore:
    """
    Trade journal storage.

    Stores trades in both SQLite (for querying) and CSV (for portability).
    Also logs fills for detailed execution analysis.
    """

    def __init__(self, db_path: str | Path = "trades.db", csv_dir: str | Path = "trades") -> None:
        """
        Initialize trade store.

        Args:
            db_path: Path to SQLite database
            csv_dir: Directory for CSV exports
        """
        self._db_path = Path(db_path)
        self._csv_dir = Path(csv_dir)
        self._csv_dir.mkdir(parents=True, exist_ok=True)
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Initialize database and create tables."""
        self._db = await aiosqlite.connect(self._db_path)

        # Create trades table
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                exit_time TEXT NOT NULL,
                entry_price TEXT NOT NULL,
                exit_price TEXT NOT NULL,
                size TEXT NOT NULL,
                gross_pnl TEXT NOT NULL,
                fees TEXT NOT NULL,
                slippage_estimate TEXT NOT NULL,
                net_pnl TEXT NOT NULL,
                exit_reason TEXT NOT NULL,
                hold_time_seconds REAL NOT NULL,
                metadata TEXT,
                created_at TEXT NOT NULL
            )
        """)

        # Create fills table
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                order_id TEXT NOT NULL,
                trade_id TEXT,
                side TEXT NOT NULL,
                price TEXT NOT NULL,
                qty TEXT NOT NULL,
                fee TEXT NOT NULL,
                fee_asset TEXT,
                realized_pnl TEXT,
                created_at TEXT NOT NULL
            )
        """)

        await self._db.commit()
        logger.info(f"Trade store initialized: {self._db_path}")

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()

    async def save_trade(self, trade: Trade, metadata: dict[str, Any] | None = None) -> None:
        """
        Save a completed trade.

        Args:
            trade: Trade record to save
            metadata: Optional additional metadata
        """
        if not self._db:
            raise RuntimeError("Trade store not initialized")

        now = datetime.utcnow().isoformat()

        await self._db.execute(
            """
            INSERT OR REPLACE INTO trades (
                trade_id, symbol, side, entry_time, exit_time,
                entry_price, exit_price, size, gross_pnl, fees,
                slippage_estimate, net_pnl, exit_reason, hold_time_seconds,
                metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.trade_id,
                trade.symbol,
                trade.side.value,
                trade.entry_time.isoformat(),
                trade.exit_time.isoformat(),
                str(trade.entry_price),
                str(trade.exit_price),
                str(trade.size),
                str(trade.gross_pnl),
                str(trade.fees),
                str(trade.slippage_estimate),
                str(trade.net_pnl),
                trade.exit_reason,
                trade.hold_time_seconds,
                json.dumps(metadata) if metadata else None,
                now,
            ),
        )
        await self._db.commit()

        # Also append to CSV
        await self._append_trade_csv(trade)

        logger.debug(f"Saved trade: {trade.trade_id}")

    async def save_fill(self, fill: FillEvent) -> None:
        """
        Save a fill/execution.

        Args:
            fill: Fill event to save
        """
        if not self._db:
            raise RuntimeError("Trade store not initialized")

        now = datetime.utcnow().isoformat()

        await self._db.execute(
            """
            INSERT INTO fills (
                timestamp, symbol, order_id, trade_id, side,
                price, qty, fee, fee_asset, realized_pnl, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.timestamp.isoformat(),
                fill.symbol,
                fill.order_id,
                fill.trade_id,
                fill.side.value,
                str(fill.price),
                str(fill.qty),
                str(fill.fee),
                fill.fee_asset,
                str(fill.realized_pnl),
                now,
            ),
        )
        await self._db.commit()

    async def get_trades(
        self,
        symbol: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 1000,
    ) -> list[Trade]:
        """
        Query trades from database.

        Args:
            symbol: Optional filter by symbol
            start_date: Optional start date filter
            end_date: Optional end date filter
            limit: Maximum number of trades to return

        Returns:
            List of Trade objects
        """
        if not self._db:
            raise RuntimeError("Trade store not initialized")

        query = "SELECT * FROM trades WHERE 1=1"
        params: list[Any] = []

        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)

        if start_date:
            query += " AND entry_time >= ?"
            params.append(start_date.isoformat())

        if end_date:
            query += " AND entry_time <= ?"
            params.append(end_date.isoformat())

        query += f" ORDER BY entry_time DESC LIMIT {limit}"

        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        trades = []
        for row in rows:
            trade = Trade(
                trade_id=row[0],
                symbol=row[1],
                side=Side(row[2]),
                entry_time=datetime.fromisoformat(row[3]),
                exit_time=datetime.fromisoformat(row[4]),
                entry_price=Decimal(row[5]),
                exit_price=Decimal(row[6]),
                size=Decimal(row[7]),
                gross_pnl=Decimal(row[8]),
                fees=Decimal(row[9]),
                slippage_estimate=Decimal(row[10]),
                exit_reason=row[12],
            )
            trades.append(trade)

        return trades

    async def get_daily_sl_count(self, target_date: date) -> int:
        """
        Count stop-loss exits for a specific day (by exit_time).

        Args:
            target_date: The date to query

        Returns:
            Number of SL exits today
        """
        if not self._db:
            return 0

        date_start = target_date.isoformat()
        date_end = (target_date + timedelta(days=1)).isoformat()

        query = (
            "SELECT COUNT(*) FROM trades "
            "WHERE exit_time >= ? AND exit_time < ? AND exit_reason = 'sl'"
        )
        async with self._db.execute(query, (date_start, date_end)) as cursor:
            row = await cursor.fetchone()

        return row[0] if row else 0

    async def _append_trade_csv(self, trade: Trade) -> None:
        """Append trade to daily CSV file."""
        date_str = trade.exit_time.strftime("%Y%m%d")
        csv_path = self._csv_dir / f"trades_{date_str}.csv"

        write_header = not csv_path.exists()

        with open(csv_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=trade.to_dict().keys())
            if write_header:
                writer.writeheader()
            writer.writerow(trade.to_dict())

    async def export_all_trades_csv(self, output_path: str | Path) -> int:
        """
        Export all trades to a single CSV file.

        Args:
            output_path: Output file path

        Returns:
            Number of trades exported
        """
        trades = await self.get_trades(limit=100000)

        if not trades:
            return 0

        output_path = Path(output_path)
        with open(output_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=trades[0].to_dict().keys())
            writer.writeheader()
            for trade in trades:
                writer.writerow(trade.to_dict())

        return len(trades)
