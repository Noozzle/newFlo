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
        # Note: `trade_id` here is the *journal* trade id (T000001),
        # populated retrospectively when the parent trade is saved.
        # `exec_id` stores the exchange execution id (Bybit execId / uuid).
        # `client_order_id` lets us group partial fills of the same order.
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                order_id TEXT NOT NULL,
                trade_id TEXT,
                exec_id TEXT,
                client_order_id TEXT,
                side TEXT NOT NULL,
                price TEXT NOT NULL,
                qty TEXT NOT NULL,
                fee TEXT NOT NULL,
                fee_asset TEXT,
                realized_pnl TEXT,
                created_at TEXT NOT NULL
            )
        """)

        # Backfill new columns for databases that pre-date the schema change.
        await self._ensure_fill_columns()

        # Helpful indexes for the retro-linking UPDATE in save_trade.
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fills_symbol_ts ON fills(symbol, timestamp)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fills_client_order_id ON fills(client_order_id)"
        )

        # AI gate decisions table — logs every SKIP/HALF/FULL decision
        # produced by the gate. Linked back to a trade via `trade_id` when
        # a trade actually executes (populated by save_trade).
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS ai_gate_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                score REAL,
                action TEXT NOT NULL,
                reason TEXT,
                trade_id TEXT,
                metadata TEXT
            )
        """)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_gate_decisions_ts ON ai_gate_decisions(timestamp)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_gate_decisions_trade_id ON ai_gate_decisions(trade_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_gate_decisions_symbol_ts ON ai_gate_decisions(symbol, timestamp)"
        )

        await self._db.commit()
        logger.info(f"Trade store initialized: {self._db_path}")

    async def _ensure_fill_columns(self) -> None:
        """Add `exec_id` / `client_order_id` columns on pre-existing DBs."""
        assert self._db is not None
        async with self._db.execute("PRAGMA table_info(fills)") as cursor:
            rows = await cursor.fetchall()
        existing = {row[1] for row in rows}
        if "exec_id" not in existing:
            await self._db.execute("ALTER TABLE fills ADD COLUMN exec_id TEXT")
        if "client_order_id" not in existing:
            await self._db.execute("ALTER TABLE fills ADD COLUMN client_order_id TEXT")

    async def get_max_trade_counter(self) -> int:
        """
        Return the highest existing journal trade number (e.g. ``T000042`` -> 42).

        Used by the portfolio to resume numbering across process restarts so we
        don't silently overwrite older rows via ``INSERT OR REPLACE``.
        """
        if not self._db:
            return 0
        async with self._db.execute(
            "SELECT trade_id FROM trades WHERE trade_id LIKE 'T%'"
        ) as cursor:
            rows = await cursor.fetchall()
        max_n = 0
        for (tid,) in rows:
            if isinstance(tid, str) and tid.startswith("T") and tid[1:].isdigit():
                max_n = max(max_n, int(tid[1:]))
        return max_n

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

        # NOTE: we intentionally avoid `INSERT OR REPLACE` here. If a restart
        # resets the in-memory trade counter, `INSERT OR REPLACE` would silently
        # clobber older rows that share the same `trade_id`. `INSERT OR IGNORE`
        # preserves history; callers can detect the collision via the logged
        # warning below.
        cursor = await self._db.execute(
            """
            INSERT OR IGNORE INTO trades (
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
        if cursor.rowcount == 0:
            logger.warning(
                f"Trade id collision in store: {trade.trade_id} "
                f"({trade.symbol} {trade.entry_time.isoformat()}) — row kept as-is."
            )

        # Link fills (entry + exit) to the journal trade_id.
        # Match by symbol + timestamp window; a small tail buffer accounts for
        # exit fills arriving moments after the bookkeeping close. Only rows
        # that haven't been linked yet are updated.
        window_end = (trade.exit_time + timedelta(seconds=30)).isoformat()
        await self._db.execute(
            """
            UPDATE fills
               SET trade_id = ?
             WHERE symbol = ?
               AND timestamp >= ?
               AND timestamp <= ?
               AND trade_id IS NULL
            """,
            (
                trade.trade_id,
                trade.symbol,
                trade.entry_time.isoformat(),
                window_end,
            ),
        )

        # Link AI gate decisions to the journal trade_id. We look for a
        # decision in a ±30s window around entry_time for the same symbol
        # that hasn't been attached to any trade yet.
        gate_window_start = (trade.entry_time - timedelta(seconds=30)).isoformat()
        gate_window_end = (trade.entry_time + timedelta(seconds=30)).isoformat()
        await self._db.execute(
            """
            UPDATE ai_gate_decisions
               SET trade_id = ?
             WHERE symbol = ?
               AND timestamp >= ?
               AND timestamp <= ?
               AND trade_id IS NULL
               AND action != 'skip'
            """,
            (
                trade.trade_id,
                trade.symbol,
                gate_window_start,
                gate_window_end,
            ),
        )

        # Bug 3 fix: re-aggregate fees from all partial fills belonging to
        # this trade. The caller (Portfolio.close_position) only sees the fee
        # from the first entry fill + first exit fill, so for orders that
        # executed in multiple partials the stored ``fees`` was systematically
        # under-reported (often by 2-3x). Fills are the ground truth since
        # each FillEvent carries its own ``execFee`` from Bybit.
        async with self._db.execute(
            "SELECT SUM(CAST(fee AS REAL)) FROM fills WHERE trade_id = ?",
            (trade.trade_id,),
        ) as cursor:
            row = await cursor.fetchone()
        recovered_fees: Decimal | None = None
        if row and row[0] is not None:
            recovered_fees = Decimal(str(row[0]))

        if recovered_fees is not None and recovered_fees > trade.fees:
            logger.info(
                f"Recovered fees for {trade.trade_id} {trade.symbol}: "
                f"{trade.fees} -> {recovered_fees} (from fills)"
            )
            # Mutate the in-memory Trade so downstream consumers (CSV, telegram,
            # strategy event) see the corrected figure, then update the row.
            trade.fees = recovered_fees
            await self._db.execute(
                "UPDATE trades SET fees = ?, net_pnl = ? WHERE trade_id = ?",
                (str(trade.fees), str(trade.net_pnl), trade.trade_id),
            )

        await self._db.commit()

        # Also append to CSV (after fee recovery so CSV matches SQLite).
        await self._append_trade_csv(trade)

        logger.debug(f"Saved trade: {trade.trade_id}")

    async def save_fill(self, fill: FillEvent) -> None:
        """
        Save a fill/execution.

        The exchange execution id (Bybit ``execId``) is stored in ``exec_id``.
        The journal ``trade_id`` (e.g. ``T000042``) is filled in later from
        :meth:`save_trade` once the parent trade is closed — so here we leave
        it ``NULL``.

        Args:
            fill: Fill event to save
        """
        if not self._db:
            raise RuntimeError("Trade store not initialized")

        now = datetime.utcnow().isoformat()

        await self._db.execute(
            """
            INSERT INTO fills (
                timestamp, symbol, order_id, trade_id, exec_id, client_order_id,
                side, price, qty, fee, fee_asset, realized_pnl, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.timestamp.isoformat(),
                fill.symbol,
                fill.order_id,
                None,  # journal trade_id is assigned by save_trade()
                fill.trade_id,  # exchange execId / uuid
                fill.client_order_id,
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

    async def save_gate_decision(
        self,
        timestamp: datetime,
        symbol: str,
        side: str,
        action: str,
        reason: str = "",
        score: float | None = None,
        features_snapshot: dict[str, Any] | None = None,
        trade_id: str | None = None,
    ) -> None:
        """Persist an AI gate decision for post-hoc analysis / retune.

        Args:
            timestamp: Decision timestamp (matches signal.timestamp)
            symbol: Trading pair
            side: 'buy' / 'sell'
            action: 'skip' / 'half' / 'full'
            reason: Human-readable reason from GateDecision.reason
            score: Scalar score (e.g. p_win); None if no model
            features_snapshot: Feature dict snapshot (stored as JSON)
            trade_id: Optional journal trade_id (if already known)
        """
        if not self._db:
            raise RuntimeError("Trade store not initialized")

        # Normalize features to JSON-safe primitives (Decimals -> str etc.)
        meta_json: str | None = None
        if features_snapshot is not None:
            try:
                safe: dict[str, Any] = {}
                for k, v in features_snapshot.items():
                    if isinstance(v, Decimal):
                        safe[k] = str(v)
                    elif isinstance(v, (int, float, str, bool)) or v is None:
                        safe[k] = v
                    else:
                        safe[k] = str(v)
                meta_json = json.dumps(safe)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(f"Failed to serialize gate features: {exc}")
                meta_json = None

        await self._db.execute(
            """
            INSERT INTO ai_gate_decisions (
                timestamp, symbol, side, score, action, reason, trade_id, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp.isoformat(),
                symbol,
                side,
                float(score) if score is not None else None,
                action,
                reason,
                trade_id,
                meta_json,
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
