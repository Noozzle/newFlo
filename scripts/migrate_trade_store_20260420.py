"""
One-shot migration for `live_trades.db` (run manually, not automated).

Repairs data written before the TradeStore bug fixes (2026-04-20):

  Bug 1 — fills.trade_id stored the exchange execId instead of the journal
          trade_id (T000001). Backfill `exec_id` from the old column and
          relink `trade_id` by (symbol, timestamp window) to the matching row
          in `trades`. Old code also didn't persist `client_order_id`, so we
          can't recover it for historical fills.

  Bug 2 — slippage_estimate is bogus when the trade had sl_price == 0 (a
          reconciled position). We can't reconstruct the real SL price, so
          we zero out obviously broken rows where slippage >= 50% of notional
          and recompute net_pnl accordingly.

  Bug 3 — trades.fees is under-reported because only the first partial fill's
          fee was captured. Recompute from the summed fills now that
          trade_id linkage has been repaired.

Usage (from repo root):

    python scripts/migrate_trade_store_20260420.py --db live_trades.db

Add --apply to actually write changes. Default is dry-run.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path


def _to_decimal(value: str | float | None) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("live_trades.db"))
    ap.add_argument("--apply", action="store_true", help="actually write changes")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    # --- Schema upgrades (matches TradeStore.initialize) ----------------------
    cur.execute("PRAGMA table_info(fills)")
    cols = {r[1] for r in cur.fetchall()}
    if "exec_id" not in cols:
        print("schema: adding fills.exec_id")
        if args.apply:
            cur.execute("ALTER TABLE fills ADD COLUMN exec_id TEXT")
    if "client_order_id" not in cols:
        print("schema: adding fills.client_order_id")
        if args.apply:
            cur.execute("ALTER TABLE fills ADD COLUMN client_order_id TEXT")

    if args.apply:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_fills_symbol_ts ON fills(symbol, timestamp)"
        )

    # --- Bug 1: move old trade_id -> exec_id, null out trade_id ----------------
    # Heuristic: pre-fix trade_id looks like a uuid (has a dash) or is not T+digits.
    cur.execute(
        """
        SELECT COUNT(*) FROM fills
         WHERE trade_id IS NOT NULL
           AND (trade_id LIKE '%-%' OR trade_id NOT LIKE 'T%')
        """
    )
    bad_count = cur.fetchone()[0]
    print(f"bug1: {bad_count} fills have exchange-style trade_id to migrate")
    if args.apply and bad_count:
        cur.execute(
            """
            UPDATE fills
               SET exec_id = COALESCE(exec_id, trade_id),
                   trade_id = NULL
             WHERE trade_id IS NOT NULL
               AND (trade_id LIKE '%-%' OR trade_id NOT LIKE 'T%')
            """
        )

    # --- Bug 1: re-link fills to journal trade_id by window --------------------
    cur.execute("SELECT trade_id, symbol, entry_time, exit_time FROM trades")
    trades = cur.fetchall()
    relinked = 0
    for tid, symbol, entry_time, exit_time in trades:
        # Window: entry_time .. exit_time + 30s tail buffer
        exit_dt = datetime.fromisoformat(exit_time)
        window_end = (exit_dt + timedelta(seconds=30)).isoformat()
        cur.execute(
            """
            SELECT COUNT(*) FROM fills
             WHERE symbol = ? AND timestamp >= ? AND timestamp <= ?
               AND trade_id IS NULL
            """,
            (symbol, entry_time, window_end),
        )
        n = cur.fetchone()[0]
        relinked += n
        if args.apply and n:
            cur.execute(
                """
                UPDATE fills
                   SET trade_id = ?
                 WHERE symbol = ? AND timestamp >= ? AND timestamp <= ?
                   AND trade_id IS NULL
                """,
                (tid, symbol, entry_time, window_end),
            )
    print(f"bug1: relinked {relinked} fills to journal trade_ids")

    # --- Bug 2: zero out catastrophic slippage rows ----------------------------
    cur.execute(
        """
        SELECT trade_id, symbol, side, entry_price, size, gross_pnl, fees,
               slippage_estimate
          FROM trades
         WHERE CAST(slippage_estimate AS REAL) > 0.5 *
               CAST(entry_price AS REAL) * CAST(size AS REAL)
        """
    )
    bad_slip = cur.fetchall()
    print(f"bug2: {len(bad_slip)} trades with slippage > 50% of notional")
    for row in bad_slip:
        tid, symbol, side, ep, sz, gpnl, fees, slip = row
        print(f"  {tid} {symbol} {side}: slip={slip} notional={_to_decimal(ep)*_to_decimal(sz)}")
        if args.apply:
            new_net = _to_decimal(gpnl) - _to_decimal(fees)
            cur.execute(
                "UPDATE trades SET slippage_estimate = '0', net_pnl = ? WHERE trade_id = ?",
                (str(new_net), tid),
            )

    # --- Bug 3: recompute fees from linked fills -------------------------------
    cur.execute(
        """
        SELECT t.trade_id, t.fees, t.gross_pnl, t.slippage_estimate,
               COALESCE(SUM(CAST(f.fee AS REAL)), 0) AS recovered
          FROM trades t
          LEFT JOIN fills f ON f.trade_id = t.trade_id
         GROUP BY t.trade_id
        """
    )
    fee_fixes = 0
    for tid, old_fees, gross_pnl, slip, recovered in cur.fetchall():
        old = _to_decimal(old_fees)
        new = Decimal(str(recovered))
        if new > old and new - old > Decimal("0.001"):
            fee_fixes += 1
            if args.apply:
                net = _to_decimal(gross_pnl) - new - _to_decimal(slip)
                cur.execute(
                    "UPDATE trades SET fees = ?, net_pnl = ? WHERE trade_id = ?",
                    (str(new), str(net), tid),
                )
    print(f"bug3: {fee_fixes} trades would get updated fees from linked fills")

    if args.apply:
        conn.commit()
        print("committed.")
    else:
        print("dry-run — pass --apply to commit.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
