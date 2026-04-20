"""Tests for TradeStore bug fixes."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.events import FillEvent, Side
from app.storage.trade_store import TradeStore
from app.trading.signals import Trade


def _make_fill(
    symbol: str,
    ts: datetime,
    price: Decimal,
    qty: Decimal,
    fee: Decimal,
    side: Side,
    client_order_id: str,
    order_id: str = "order-1",
    exec_id: str = "exec-abc",
) -> FillEvent:
    return FillEvent(
        timestamp=ts,
        symbol=symbol,
        order_id=order_id,
        client_order_id=client_order_id,
        trade_id=exec_id,  # exchange execId (NOT the journal trade_id)
        side=side,
        price=price,
        qty=qty,
        fee=fee,
    )


def _make_trade(
    trade_id: str,
    symbol: str,
    entry_time: datetime,
    exit_time: datetime,
    fees: Decimal = Decimal("0.01"),
    side: Side = Side.BUY,
) -> Trade:
    return Trade(
        trade_id=trade_id,
        symbol=symbol,
        side=side,
        entry_time=entry_time,
        exit_time=exit_time,
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        size=Decimal("1"),
        gross_pnl=Decimal("1"),
        fees=fees,
        slippage_estimate=Decimal("0"),
        exit_reason="tp",
    )


@pytest.mark.asyncio
async def test_fills_linked_to_journal_trade_id(tmp_path: Path) -> None:
    """Bug 1: fills must be linked to the journal trade_id (T000001), not execId."""
    store = TradeStore(db_path=tmp_path / "t.db", csv_dir=tmp_path / "csv")
    await store.initialize()

    t_entry = datetime(2026, 4, 20, 12, 0, 0)
    t_exit = t_entry + timedelta(minutes=5)

    # Two partial entry fills + one exit fill, all with exchange execIds.
    await store.save_fill(_make_fill(
        "BTCUSDT", t_entry, Decimal("50000"), Decimal("0.5"),
        Decimal("0.03"), Side.BUY, client_order_id="entry_BTC_1", exec_id="exec-1",
    ))
    await store.save_fill(_make_fill(
        "BTCUSDT", t_entry + timedelta(seconds=1), Decimal("50001"),
        Decimal("0.5"), Decimal("0.03"), Side.BUY,
        client_order_id="entry_BTC_1", exec_id="exec-2",
    ))
    await store.save_fill(_make_fill(
        "BTCUSDT", t_exit, Decimal("51000"), Decimal("1"),
        Decimal("0.06"), Side.SELL, client_order_id="exit_BTC_1", exec_id="exec-3",
    ))

    trade = _make_trade("T000001", "BTCUSDT", t_entry, t_exit, fees=Decimal("0.03"))
    await store.save_trade(trade)

    # All 3 fills should now link back to T000001.
    assert store._db is not None  # initialized
    async with store._db.execute(
        "SELECT exec_id, trade_id FROM fills ORDER BY timestamp"
    ) as cursor:
        rows = await cursor.fetchall()

    assert len(rows) == 3
    for exec_id, journal_tid in rows:
        assert journal_tid == "T000001"
        assert exec_id.startswith("exec-")  # exchange id preserved separately

    await store.close()


@pytest.mark.asyncio
async def test_fees_re_aggregated_from_partial_fills(tmp_path: Path) -> None:
    """Bug 3: trade.fees should equal SUM(fills.fee) across all partial fills."""
    store = TradeStore(db_path=tmp_path / "t.db", csv_dir=tmp_path / "csv")
    await store.initialize()

    t_entry = datetime(2026, 4, 20, 12, 0, 0)
    t_exit = t_entry + timedelta(minutes=5)

    # 6 partial fills, total fee = 0.20 USDT. Caller only knows about 2 of them
    # (first entry + first exit) -> stored trade.fees = 0.04, but true total is 0.20.
    real_fees: list[Decimal] = [
        Decimal("0.02"), Decimal("0.03"), Decimal("0.04"),
        Decimal("0.05"), Decimal("0.03"), Decimal("0.03"),
    ]
    for i, fee in enumerate(real_fees):
        side = Side.BUY if i < 3 else Side.SELL
        coid = "entry_X" if i < 3 else "exit_X"
        await store.save_fill(_make_fill(
            "ETHUSDT", t_entry + timedelta(seconds=i),
            Decimal("3000"), Decimal("0.1"), fee, side,
            client_order_id=coid, exec_id=f"exec-{i}",
        ))

    trade = _make_trade("T000002", "ETHUSDT", t_entry, t_exit, fees=Decimal("0.04"))
    await store.save_trade(trade)

    # The in-memory trade should have been mutated to reflect recovered fees.
    expected = sum(real_fees, start=Decimal("0"))
    assert trade.fees == expected

    async with store._db.execute(
        "SELECT fees FROM trades WHERE trade_id = 'T000002'"
    ) as cursor:
        (stored_fees,) = await cursor.fetchone()  # type: ignore[misc]
    assert Decimal(stored_fees) == expected

    await store.close()


@pytest.mark.asyncio
async def test_resume_trade_counter_prevents_collisions(tmp_path: Path) -> None:
    """Bug 4: get_max_trade_counter enables resuming numbering across restarts."""
    store = TradeStore(db_path=tmp_path / "t.db", csv_dir=tmp_path / "csv")
    await store.initialize()

    t = datetime(2026, 4, 20, 12, 0, 0)
    await store.save_trade(_make_trade("T000001", "BTCUSDT", t, t + timedelta(minutes=1)))
    await store.save_trade(_make_trade("T000042", "BTCUSDT", t + timedelta(hours=1), t + timedelta(hours=1, minutes=1)))

    assert await store.get_max_trade_counter() == 42

    # INSERT OR IGNORE must NOT clobber the existing row when a restart collides.
    duplicate = _make_trade("T000001", "XRPUSDT", t + timedelta(days=1), t + timedelta(days=1, minutes=1))
    duplicate.gross_pnl = Decimal("99")
    await store.save_trade(duplicate)

    async with store._db.execute(
        "SELECT symbol, gross_pnl FROM trades WHERE trade_id = 'T000001'"
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    symbol, gross_pnl = row
    # Original row preserved, duplicate ignored.
    assert symbol == "BTCUSDT"
    assert Decimal(gross_pnl) == Decimal("1")

    await store.close()
