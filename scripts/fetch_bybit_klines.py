"""Fetch historical klines from Bybit for regime-diagnostics window.

Downloads 1m + 5m bars for a configurable symbol set and UTC date range,
then writes them to Parquet with the same schema as the existing
`live_data/{SYMBOL}/1m.parquet` files:
    timestamp_ms: int64 (ms since Unix epoch, UTC)
    open, high, low, close, volume: double

Category is taken from config.yaml (bybit.category) — typically `linear`.

Output layout:
    live_data/{SYMBOL}/{tf}_{tag}.parquet    (parquet, same schema as live_data)

Rationale for this location: the project's historical_data_feed reads
parquet klines from live_data/<SYM>/<tf>.parquet via the `timestamp_ms`
column, so using that schema keeps everything consistent. A tag suffix
avoids overwriting earlier snapshots (which end in Feb 2026).

Usage:
    python -m scripts.fetch_bybit_klines \
        --symbols SOLUSDT SUIUSDT DOGEUSDT XRPUSDT ETHUSDT \
        --start 2026-04-02 --end 2026-04-18 \
        --tfs 1 5 --tag apr2026
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml
from loguru import logger
from pybit.unified_trading import HTTP

BYBIT_MAX_LIMIT = 1000  # bars per request

# Interval minutes -> Bybit V5 interval string
INTERVAL_MAP = {
    1: "1",
    3: "3",
    5: "5",
    15: "15",
    30: "30",
    60: "60",
    240: "240",
}


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load_category(config_path: Path) -> str:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("bybit", {}).get("category", "linear")


def fetch_klines(
    client: HTTP,
    category: str,
    symbol: str,
    interval_min: int,
    start_ms: int,
    end_ms: int,
) -> pd.DataFrame:
    """Fetch klines in reverse-time chunks until the whole window is covered."""
    interval_str = INTERVAL_MAP[interval_min]
    frames: list[pd.DataFrame] = []
    seen: set[int] = set()

    # Bybit V5 returns newest-first and respects `end` as upper bound.
    cursor_end = end_ms
    pages = 0
    while cursor_end > start_ms:
        pages += 1
        resp = client.get_kline(
            category=category,
            symbol=symbol,
            interval=interval_str,
            start=start_ms,
            end=cursor_end,
            limit=BYBIT_MAX_LIMIT,
        )
        if resp.get("retCode") != 0:
            raise RuntimeError(f"{symbol} {interval_str}m: {resp}")

        rows = resp.get("result", {}).get("list", [])
        if not rows:
            break

        # Rows: [startMs, open, high, low, close, volume, turnover]
        df = pd.DataFrame(
            rows,
            columns=["timestamp_ms", "open", "high", "low", "close", "volume", "turnover"],
        )
        df["timestamp_ms"] = df["timestamp_ms"].astype("int64")
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = df[c].astype("float64")
        df = df.drop(columns=["turnover"])

        # Filter to window and dedupe cross-page overlaps.
        mask = (df["timestamp_ms"] >= start_ms) & (df["timestamp_ms"] <= end_ms)
        df = df.loc[mask]
        df = df.loc[~df["timestamp_ms"].isin(list(seen))]
        if df.empty:
            break
        ts_list = df["timestamp_ms"].tolist()
        seen.update(ts_list)
        frames.append(df)

        oldest = int(min(ts_list))
        # Next page ends just before the oldest bar we already have.
        new_cursor = oldest - 1
        if new_cursor >= cursor_end:
            break
        cursor_end = new_cursor
        time.sleep(0.1)  # gentle rate-limit

    if not frames:
        return pd.DataFrame(
            columns=["timestamp_ms", "open", "high", "low", "close", "volume"]
        )

    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values("timestamp_ms").drop_duplicates("timestamp_ms")
    out = out.reset_index(drop=True)
    logger.info(
        f"  {symbol} {interval_min}m: {len(out)} bars over {pages} pages "
        f"({_iso(int(out.timestamp_ms.min()))} -> {_iso(int(out.timestamp_ms.max()))})"
    )
    return out


def validate(df: pd.DataFrame, interval_min: int, start_ms: int, end_ms: int) -> dict:
    """Return a dict of validation metrics."""
    if df.empty:
        return {"bars": 0, "expected": 0, "gaps": None, "ohlc_invariant_ok": None, "nan": None}

    bar_ms = interval_min * 60 * 1000
    expected = (end_ms - start_ms) // bar_ms  # approximate
    # Gaps: diff between successive bars / bar_ms should be 1
    diffs = df["timestamp_ms"].diff().dropna().astype("int64")
    gap_count = int(((diffs / bar_ms) > 1).sum())
    # OHLC invariant
    inv = (
        (df["high"] >= df[["open", "close"]].max(axis=1))
        & (df["low"] <= df[["open", "close"]].min(axis=1))
        & (df["high"] >= df["low"])
    )
    invariant_ok = bool(inv.all())
    nan_count = int(df[["open", "high", "low", "close", "volume"]].isna().sum().sum())
    return {
        "bars": len(df),
        "expected": int(expected),
        "gaps": gap_count,
        "ohlc_invariant_ok": invariant_ok,
        "nan": nan_count,
        "first": _iso(int(df.timestamp_ms.min())),
        "last": _iso(int(df.timestamp_ms.max())),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", required=True)
    ap.add_argument("--start", required=True, help="UTC start, YYYY-MM-DD or ISO")
    ap.add_argument("--end", required=True, help="UTC end, YYYY-MM-DD or ISO")
    ap.add_argument("--tfs", nargs="+", type=int, default=[1, 5])
    ap.add_argument("--tag", default="apr2026")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out-root", default="live_data")
    args = ap.parse_args()

    # Parse UTC range (inclusive end day 23:59:59 if only a date was given).
    def _to_ms(s: str, end: bool) -> int:
        if "T" in s or " " in s:
            dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
            if end:
                dt = dt.replace(hour=23, minute=59, second=59)
        return int(dt.timestamp() * 1000)

    start_ms = _to_ms(args.start, end=False)
    end_ms = _to_ms(args.end, end=True)

    category = load_category(Path(args.config))
    logger.info(f"Bybit category: {category}")
    logger.info(f"Window: {_iso(start_ms)} -> {_iso(end_ms)} (UTC)")

    # Public data — no auth required.
    client = HTTP(testnet=False)

    out_root = Path(args.out_root)
    report: list[dict] = []

    for symbol in args.symbols:
        logger.info(f"=== {symbol} ===")
        sym_dir = out_root / symbol
        sym_dir.mkdir(parents=True, exist_ok=True)

        for tf in args.tfs:
            if tf not in INTERVAL_MAP:
                logger.error(f"Unsupported tf={tf}")
                continue
            df = fetch_klines(client, category, symbol, tf, start_ms, end_ms)
            out_path = sym_dir / f"{tf}m_{args.tag}.parquet"
            df.to_parquet(out_path, index=False)
            m = validate(df, tf, start_ms, end_ms)
            m.update({"symbol": symbol, "tf": f"{tf}m", "path": str(out_path)})
            report.append(m)
            logger.info(f"  -> {out_path}  |  validation: {m}")

    logger.info("=== SUMMARY ===")
    rep_df = pd.DataFrame(report)
    logger.info("\n" + rep_df.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
