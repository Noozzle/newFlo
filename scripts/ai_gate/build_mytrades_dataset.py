"""Build labeled dataset from my_trades + normalized market data.

For each trade, reconstructs market features at ts_entry using ONLY data <= ts_entry.
No leakage: closed candles only, tape/L1 strictly before entry.

Usage:
    python -m scripts.ai_gate.build_mytrades_dataset \
        --symbols DOGEUSDT,ETHUSDT --in data/normalized --out data/datasets
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger


# ── L1 features ──────────────────────────────────────────────────


def _l1_features(l1_ts: np.ndarray, l1: pd.DataFrame, ts_entry: int) -> dict[str, float]:
    """Last L1 snapshot with ts <= ts_entry."""
    idx = np.searchsorted(l1_ts, ts_entry, side="right") - 1
    if idx < 0:
        return {"rel_spread": np.nan, "ob_imbalance": np.nan}

    row = l1.iloc[idx]
    mid = row["mid_price"]
    spread = row["spread"]
    bid_sz = row["bid_size"]
    ask_sz = row["ask_size"]

    return {
        "rel_spread": spread / mid if mid else 0.0,
        "ob_imbalance": (bid_sz - ask_sz) / (bid_sz + ask_sz + 1e-12),
    }


# ── Tape features ────────────────────────────────────────────────

TAPE_WINDOWS_MS = [30_000, 60_000, 120_000]


def _tape_features(
    tape_ts: np.ndarray,
    tape_price: np.ndarray,
    tape_amount: np.ndarray,
    tape_side: np.ndarray,
    ts_entry: int,
) -> dict[str, float]:
    """Tape aggregates over windows ending at ts_entry."""
    feats: dict[str, float] = {}

    # Find right boundary: last row with ts <= ts_entry
    right = np.searchsorted(tape_ts, ts_entry, side="right")

    for win_ms in TAPE_WINDOWS_MS:
        suffix = f"_{win_ms // 1000}s"
        left = np.searchsorted(tape_ts, ts_entry - win_ms, side="left")

        if left >= right:
            # No data in window
            for key in ["buy_vol", "sell_vol", "delta", "trade_count", "avg_trade_size", "delta_ratio"]:
                feats[key + suffix] = 0.0
            continue

        slc_amount = tape_amount[left:right]
        slc_side = tape_side[left:right]

        buy_mask = slc_side == "buy"
        buy_vol = float(slc_amount[buy_mask].sum())
        sell_vol = float(slc_amount[~buy_mask].sum())
        delta = buy_vol - sell_vol
        count = right - left
        avg_size = float(slc_amount.mean()) if count > 0 else 0.0

        feats[f"buy_vol{suffix}"] = buy_vol
        feats[f"sell_vol{suffix}"] = sell_vol
        feats[f"delta{suffix}"] = delta
        feats[f"trade_count{suffix}"] = float(count)
        feats[f"avg_trade_size{suffix}"] = avg_size
        feats[f"delta_ratio{suffix}"] = delta / (buy_vol + sell_vol + 1e-12)

    return feats


# ── 1m candle features ──────────────────────────────────────────


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    """ATR from arrays of high, low, close (last `period` candles)."""
    if len(closes) < 2:
        return 0.0
    n = min(len(closes), period + 1)
    h = highs[-n:]
    l = lows[-n:]
    c = closes[-n:]
    # True range
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    return float(tr[-min(period, len(tr)):].mean())


def _candles_1m_features(
    c1m_ts: np.ndarray,
    c1m: pd.DataFrame,
    ts_entry: int,
) -> dict[str, float]:
    """1m candle features from last CLOSED candle (ts < floor(ts_entry/60s))."""
    # Floor to minute boundary — candles with ts < this are closed
    boundary = (ts_entry // 60_000) * 60_000
    idx = np.searchsorted(c1m_ts, boundary, side="left") - 1

    if idx < 0:
        return {"range_pct_1m": np.nan, "close_pos_1m": np.nan, "atr_14_1m": np.nan}

    row = c1m.iloc[idx]
    h, l, c = row["high"], row["low"], row["close"]
    rng = h - l

    # ATR from up to last 15 candles (need 14+1 for TR calc)
    start = max(0, idx - 14)
    sl = c1m.iloc[start:idx + 1]

    return {
        "range_pct_1m": rng / c if c else 0.0,
        "close_pos_1m": (c - l) / (rng + 1e-12),
        "atr_14_1m": _atr(
            sl["high"].values, sl["low"].values, sl["close"].values, 14
        ),
    }


# ── 15m candle features ─────────────────────────────────────────


def _candles_15m_features(
    c15m_ts: np.ndarray,
    c15m: pd.DataFrame,
    ts_entry: int,
) -> dict[str, float]:
    """15m candle features from last CLOSED candle (ts < floor(ts_entry/15m))."""
    boundary = (ts_entry // 900_000) * 900_000
    idx = np.searchsorted(c15m_ts, boundary, side="left") - 1

    if idx < 0:
        return {
            "atr_14_15m": np.nan,
            "close_minus_ma20_15m": np.nan,
            "trend_slope_15m": np.nan,
        }

    # ATR
    start_atr = max(0, idx - 14)
    sl_atr = c15m.iloc[start_atr:idx + 1]
    atr_15m = _atr(sl_atr["high"].values, sl_atr["low"].values, sl_atr["close"].values, 14)

    # MA20 and trend slope
    start_ma = max(0, idx - 19)
    closes_ma = c15m.iloc[start_ma:idx + 1]["close"].values

    if len(closes_ma) >= 20:
        ma20 = closes_ma[-20:].mean()
    else:
        ma20 = closes_ma.mean()

    last_close = c15m.iloc[idx]["close"]
    close_minus_ma20 = (last_close - ma20) / (ma20 + 1e-12)

    # Trend slope: slope of MA20 over last N=6 candles
    # Compute rolling MA20 at each of the last 6 points, then linear slope
    trend_slope = 0.0
    n_slope = 6
    if idx >= n_slope + 19:
        ma_vals = []
        for i in range(n_slope):
            j = idx - (n_slope - 1 - i)
            s = max(0, j - 19)
            window = c15m.iloc[s:j + 1]["close"].values
            ma_vals.append(window[-20:].mean() if len(window) >= 20 else window.mean())
        ma_arr = np.array(ma_vals)
        # Normalize slope by mean price level
        x = np.arange(n_slope, dtype=float)
        if ma_arr.std() > 0:
            slope = np.polyfit(x, ma_arr, 1)[0]
            trend_slope = slope / (ma_arr.mean() + 1e-12)

    return {
        "atr_14_15m": atr_15m,
        "close_minus_ma20_15m": close_minus_ma20,
        "trend_slope_15m": trend_slope,
    }


# ── Main builder ─────────────────────────────────────────────────


def build_dataset(
    symbols: list[str],
    norm_dir: Path,
    out_dir: Path,
) -> pd.DataFrame:
    """Build labeled dataset for all symbols."""
    my_trades = pd.read_parquet(norm_dir / "my_trades.parquet")
    logger.info(f"Loaded {len(my_trades)} trades total")

    all_rows: list[dict] = []

    for symbol in symbols:
        sym_dir = norm_dir / symbol
        trades = my_trades[my_trades["symbol"] == symbol].copy()

        if trades.empty:
            logger.info(f"{symbol}: no trades, skipping")
            continue

        logger.info(f"{symbol}: {len(trades)} trades, loading market data...")

        # Load market data
        c1m = pd.read_parquet(sym_dir / "candles_1m.parquet")
        c15m = pd.read_parquet(sym_dir / "candles_15m.parquet")
        l1 = pd.read_parquet(sym_dir / "l1.parquet")

        # Exchange trades: load only needed columns to save memory
        tape = pd.read_parquet(
            sym_dir / "exchange_trades.parquet",
            columns=["ts", "price", "amount", "side"],
        )

        # Pre-extract numpy arrays for fast searchsorted
        c1m_ts = c1m["ts"].values
        c15m_ts = c15m["ts"].values
        l1_ts = l1["ts"].values
        tape_ts = tape["ts"].values
        tape_price = tape["price"].values
        tape_amount = tape["amount"].values
        tape_side = tape["side"].values

        for _, trade in trades.iterrows():
            ts_e = int(trade["ts_entry"])

            row: dict[str, object] = {
                "symbol": symbol,
                "ts_entry": ts_e,
                "side": trade["side"],
                "entry_price": trade["entry_price"],
                "exit_price": trade["exit_price"],
                "hold_time_seconds": trade["hold_time_seconds"],
                "exit_reason": trade["exit_reason"],
                "net_pnl": trade["net_pnl"],
            }

            # Labels
            notional = trade["entry_price"] * trade["size"]
            row["net_return"] = trade["net_pnl"] / (notional + 1e-12)
            row["win"] = 1 if trade["net_pnl"] > 0 else 0

            # Features
            row.update(_l1_features(l1_ts, l1, ts_e))
            row.update(_tape_features(tape_ts, tape_price, tape_amount, tape_side, ts_e))
            row.update(_candles_1m_features(c1m_ts, c1m, ts_e))
            row.update(_candles_15m_features(c15m_ts, c15m, ts_e))

            # side_enc + hour_utc
            row["side_enc"] = 1.0 if trade["side"] == "buy" else -1.0
            from datetime import datetime as _dt
            row["hour_utc"] = float(_dt.utcfromtimestamp(ts_e / 1000).hour) if ts_e > 1e12 else float(_dt.utcfromtimestamp(ts_e).hour)

            all_rows.append(row)

        # Free memory before next symbol
        del tape, l1, c1m, c15m
        logger.info(f"{symbol}: {len(trades)} rows built")

    df = pd.DataFrame(all_rows)
    df.sort_values("ts_entry", inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Save
    out_path = out_dir / "mytrades_labeled.parquet"
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)

    n_win = df["win"].sum()
    logger.info(
        f"Dataset saved: {out_path} ({len(df)} rows, "
        f"{n_win}W/{len(df)-n_win}L, "
        f"WR={n_win/len(df):.1%})"
    )
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Build labeled dataset from my_trades + market data")
    parser.add_argument(
        "--symbols", type=str, required=True,
        help="Comma-separated symbols (e.g. DOGEUSDT,ETHUSDT) or 'all'"
    )
    parser.add_argument("--in", dest="in_dir", type=str, default="data/normalized")
    parser.add_argument("--out", type=str, default="data/datasets")
    args = parser.parse_args()

    norm_dir = Path(args.in_dir)

    if args.symbols.lower() == "all":
        symbols = sorted(
            p.name for p in norm_dir.iterdir()
            if p.is_dir() and p.name != "my_trades.parquet"
        )
    else:
        symbols = [s.strip() for s in args.symbols.split(",")]

    build_dataset(symbols, norm_dir, Path(args.out))


if __name__ == "__main__":
    main()
