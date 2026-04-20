"""Ad-hoc: fetch April 2026 executions from Bybit, break down fees vs gross P&L."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from app.config import Config
from app.ui.bybit_client import BybitPnLClient

CHUNK_DAYS = 7
PAGE_LIMIT = 100


def fetch_executions(client, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch executions with 7-day chunking + pagination."""
    all_exec: list[dict] = []
    chunk_ms = CHUNK_DAYS * 24 * 60 * 60 * 1000

    cur_start = start_ms
    while cur_start < end_ms:
        cur_end = min(cur_start + chunk_ms, end_ms)
        cursor = ""
        while True:
            params = {
                "category": "linear",
                "startTime": cur_start,
                "endTime": cur_end,
                "limit": PAGE_LIMIT,
            }
            if cursor:
                params["cursor"] = cursor
            resp = client._client.get_executions(**params)
            if resp.get("retCode") != 0:
                raise RuntimeError(f"get_executions failed: {resp}")
            page = resp["result"]["list"]
            all_exec.extend(page)
            cursor = resp["result"].get("nextPageCursor") or ""
            if not cursor or not page:
                break
        cur_start = cur_end

    return all_exec


def fetch_closed_pnl_raw(client, start_ms: int, end_ms: int) -> list[dict]:
    all_cp: list[dict] = []
    chunk_ms = CHUNK_DAYS * 24 * 60 * 60 * 1000
    cur_start = start_ms
    while cur_start < end_ms:
        cur_end = min(cur_start + chunk_ms, end_ms)
        cursor = ""
        while True:
            params = {
                "category": "linear",
                "startTime": cur_start,
                "endTime": cur_end,
                "limit": PAGE_LIMIT,
            }
            if cursor:
                params["cursor"] = cursor
            resp = client._client.get_closed_pnl(**params)
            if resp.get("retCode") != 0:
                raise RuntimeError(f"get_closed_pnl failed: {resp}")
            page = resp["result"]["list"]
            all_cp.extend(page)
            cursor = resp["result"].get("nextPageCursor") or ""
            if not cursor or not page:
                break
        cur_start = cur_end
    return all_cp


def main() -> None:
    cfg = Config.from_yaml("config.yaml")
    client = BybitPnLClient(cfg.bybit)

    # April 2026 UTC
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 1, tzinfo=timezone.utc)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    print(f"Period: {start.isoformat()} -> {end.isoformat()}")

    execs = fetch_executions(client, start_ms, end_ms)
    print(f"\nExecutions fetched: {len(execs)}")

    # Total fees and volume
    total_fee = Decimal("0")
    total_turnover = Decimal("0")
    fees_by_symbol: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    execs_by_symbol: dict[str, int] = defaultdict(int)
    exec_types: dict[str, int] = defaultdict(int)
    stop_order_types: dict[str, int] = defaultdict(int)

    for e in execs:
        fee = Decimal(e.get("execFee") or "0")
        qty = Decimal(e.get("execQty") or "0")
        price = Decimal(e.get("execPrice") or "0")
        turnover = qty * price

        total_fee += fee
        total_turnover += turnover
        fees_by_symbol[e["symbol"]] += fee
        execs_by_symbol[e["symbol"]] += 1
        exec_types[e.get("execType") or "?"] += 1
        stop_order_types[e.get("stopOrderType") or "-"] += 1

    print(f"\n=== FEES (April 2026) ===")
    print(f"Total execFee:        ${total_fee:.4f}")
    print(f"Total turnover:       ${total_turnover:.2f}")
    fee_pct = (total_fee / total_turnover * 100) if total_turnover > 0 else Decimal("0")
    print(f"Fee rate (realized):  {fee_pct:.4f}%")

    # Closed PnL (net, already after fees)
    cp = fetch_closed_pnl_raw(client, start_ms, end_ms)
    print(f"\nClosed PnL records:   {len(cp)}")

    net_pnl = sum(Decimal(r["closedPnl"]) for r in cp)
    wins = [r for r in cp if Decimal(r["closedPnl"]) > 0]
    losses = [r for r in cp if Decimal(r["closedPnl"]) < 0]
    breakeven = [r for r in cp if Decimal(r["closedPnl"]) == 0]

    avg_win = (sum(Decimal(r["closedPnl"]) for r in wins) / len(wins)) if wins else Decimal("0")
    avg_loss = (sum(Decimal(r["closedPnl"]) for r in losses) / len(losses)) if losses else Decimal("0")
    realized_rr = (abs(avg_win) / abs(avg_loss)) if avg_loss != 0 else Decimal("0")
    wr = Decimal(len(wins)) / Decimal(len(cp)) * 100 if cp else Decimal("0")
    be_wr = (Decimal("1") / (Decimal("1") + realized_rr)) * 100 if realized_rr > 0 else Decimal("0")

    print(f"\n=== NET (from closedPnl, already after fees) ===")
    print(f"Trades:               {len(cp)}  (W={len(wins)} L={len(losses)} BE={len(breakeven)})")
    print(f"Net P&L:              ${net_pnl:.4f}")
    print(f"Win Rate:             {wr:.2f}%")
    print(f"Avg win / Avg loss:   ${avg_win:.4f} / ${avg_loss:.4f}")
    print(f"Realized RR:          {realized_rr:.2f}")
    print(f"Breakeven WR:         {be_wr:.2f}%")

    # Gross estimate: net + fees
    gross_pnl = net_pnl + total_fee
    print(f"\n=== GROSS estimate (net + fees) ===")
    print(f"Gross P&L:            ${gross_pnl:.4f}")
    fee_drag_pct = (total_fee / abs(gross_pnl) * 100) if gross_pnl != 0 else Decimal("0")
    print(f"Fee drag:             ${total_fee:.4f} ({fee_drag_pct:.1f}% of |gross|)")
    fee_per_trade = total_fee / len(cp) if cp else Decimal("0")
    print(f"Avg fee per trade:    ${fee_per_trade:.4f}")

    # Exit reason breakdown via stopOrderType from executions with execType=Trade
    # Group fills by orderId; TP/SL hits have stopOrderType set
    print(f"\n=== EXIT TYPE BREAKDOWN (from executions, stopOrderType) ===")
    for t, n in sorted(stop_order_types.items(), key=lambda x: -x[1]):
        print(f"  {t:20s} {n}")

    print(f"\n=== EXEC TYPE BREAKDOWN ===")
    for t, n in sorted(exec_types.items(), key=lambda x: -x[1]):
        print(f"  {t:20s} {n}")

    # Per-symbol fees + pnl
    print(f"\n=== PER-SYMBOL (fees / net P&L / n) ===")
    pnl_by_symbol: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    trades_by_symbol: dict[str, int] = defaultdict(int)
    wins_by_symbol: dict[str, int] = defaultdict(int)
    for r in cp:
        pnl_by_symbol[r["symbol"]] += Decimal(r["closedPnl"])
        trades_by_symbol[r["symbol"]] += 1
        if Decimal(r["closedPnl"]) > 0:
            wins_by_symbol[r["symbol"]] += 1
    for sym in sorted(trades_by_symbol.keys()):
        n = trades_by_symbol[sym]
        w = wins_by_symbol[sym]
        fee = fees_by_symbol.get(sym, Decimal("0"))
        pnl = pnl_by_symbol[sym]
        gross_sym = pnl + fee
        print(
            f"  {sym:10s} n={n:3d} WR={w/n*100:5.1f}% net=${pnl:+.3f} gross=${gross_sym:+.3f} fees=${fee:.3f}"
        )

    # Per-trade fee from matching: closedPnl has orderId, we can sum execFee where orderId == r['orderId']
    # But simpler: match by combining entry+exit execs of same side flips.
    # Use BOTH: closedPnl.orderId is the CLOSING order. Fees include both entry and exit fill fees.
    # We cross-check by summing execFee grouped by 'closedSize tracking' — too complex here, skip.

    # Distribution of wins/losses in R-terms (assuming 1R == |avg_loss|)
    if avg_loss != 0:
        r_unit = abs(avg_loss)
        print(f"\n=== R-DISTRIBUTION (1R = ${r_unit:.4f} = |avg_loss|) ===")
        bins = {"<-1.5R": 0, "-1.5..-1R": 0, "-1..-0.5R": 0, "-0.5..0R": 0,
                "0..0.5R": 0, "0.5..1R": 0, "1..2R": 0, "2..3R": 0, ">3R": 0}
        for r in cp:
            pnl = Decimal(r["closedPnl"])
            rv = float(pnl / r_unit)
            if rv < -1.5: bins["<-1.5R"] += 1
            elif rv < -1: bins["-1.5..-1R"] += 1
            elif rv < -0.5: bins["-1..-0.5R"] += 1
            elif rv < 0: bins["-0.5..0R"] += 1
            elif rv < 0.5: bins["0..0.5R"] += 1
            elif rv < 1: bins["0.5..1R"] += 1
            elif rv < 2: bins["1..2R"] += 1
            elif rv < 3: bins["2..3R"] += 1
            else: bins[">3R"] += 1
        for b, n in bins.items():
            pct = n / len(cp) * 100
            bar = "#" * int(pct / 2)
            print(f"  {b:12s} {n:3d}  {pct:5.1f}%  {bar}")


if __name__ == "__main__":
    main()
