"""Generate ALL entry signals by replaying strategy over historical data.

Runs the actual OrderflowStrategy over HistoricalDataFeed without executing
trades. Since has_position() always returns False, all signals are captured
(no position blocking, no SL cooldowns). Normal cooldown_seconds still applies.

Label modes:
  horizon    — fixed 30m horizon, exit = 1m candle close at t+30m
  sl_tp_sim  — simulate SL/TP using 1m candle high/low, max horizon configurable

Usage:
    python -m scripts.ai_gate.generate_signals [--config config.yaml] [--label-mode sl_tp_sim]
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

from scripts.ai_gate.build_mytrades_dataset import (
    _l1_features,
    _tape_features,
    _candles_1m_features,
    _candles_15m_features,
)

HORIZON_S = 1800  # 30 minutes


def _dt_to_ts(dt: datetime) -> int:
    """Convert datetime to int seconds (UTC)."""
    return calendar.timegm(dt.timetuple())


async def collect_signals(config_path: str, start_date: str | None, end_date: str | None) -> list[dict]:
    """Phase 1: replay strategy over historical data, collect all entry signals."""
    from app.config import Config, Mode
    from app.core.event_bus import EventBus
    from app.core.events import KlineEvent, MarketTradeEvent, OrderBookEvent, SignalEvent
    from app.adapters.historical_data_feed import HistoricalDataFeed
    from app.strategies.orderflow_1m import OrderflowStrategy
    from app.trading.portfolio import Portfolio

    config = Config.from_yaml(config_path)
    config.mode = Mode.BACKTEST

    if start_date:
        config.backtest.start_date = start_date
    if end_date:
        config.backtest.end_date = end_date

    sd = datetime.strptime(config.backtest.start_date, "%Y-%m-%d")
    ed = datetime.strptime(config.backtest.end_date, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59,
    )

    event_bus = EventBus()
    data_feed = HistoricalDataFeed(
        event_bus=event_bus,
        config=config,
        start_date=sd,
        end_date=ed,
        warmup_minutes=60,
    )
    strategy = OrderflowStrategy()
    portfolio = Portfolio(initial_balance=Decimal("0"))

    collected: list[dict] = []

    async def on_signal(event: SignalEvent) -> None:
        if event.signal_type != "entry":
            return
        # Skip signals during warm-up period
        if event.timestamp < sd:
            return
        collected.append({
            "timestamp": event.timestamp,
            "symbol": event.symbol,
            "side": event.side.value,
            "entry_price": float(event.price),
            "sl_price": float(event.sl_price) if event.sl_price is not None else None,
            "tp_price": float(event.tp_price) if event.tp_price is not None else None,
            "metadata": dict(event.metadata),
        })

    event_bus.subscribe(KlineEvent, strategy.on_kline)
    event_bus.subscribe(MarketTradeEvent, strategy.on_trade)
    event_bus.subscribe(OrderBookEvent, strategy.on_orderbook)
    event_bus.subscribe(SignalEvent, on_signal)

    await strategy.initialize(event_bus=event_bus, portfolio=portfolio, config=config)
    await data_feed.start()

    symbols = config.backtest.symbols or list(
        set(config.symbols.trade + config.symbols.record)
    )
    for sym in symbols:
        await data_feed.subscribe(sym)

    logger.info(f"Streaming {data_feed.total_events} events for {symbols} ({config.backtest.start_date} → {config.backtest.end_date})")

    n = 0
    for event in data_feed.iter_events():
        await event_bus.publish_immediate(event)
        n += 1
        if n % 100_000 == 0:
            logger.info(f"  {n} events, {len(collected)} signals")

    await data_feed.stop()
    logger.info(f"Phase 1 done: {n} events processed, {len(collected)} signals collected")
    return collected


def build_features_and_labels(
    signals: list[dict],
    norm_dir: Path,
    fee_estimate: float,
) -> pd.DataFrame:
    """Phase 2+3: build features and 30m-horizon labels for each signal."""
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for sig in signals:
        by_symbol[sig["symbol"]].append(sig)

    all_rows: list[dict] = []

    for symbol, sym_signals in by_symbol.items():
        sym_dir = norm_dir / symbol
        if not sym_dir.exists():
            logger.warning(f"{symbol}: no normalized data, skipping {len(sym_signals)} signals")
            continue

        # Load market data
        c1m = pd.read_parquet(sym_dir / "candles_1m.parquet")
        c15m = pd.read_parquet(sym_dir / "candles_15m.parquet")

        l1_path = sym_dir / "l1.parquet"
        l1 = pd.read_parquet(l1_path) if l1_path.exists() else pd.DataFrame()

        tape_path = sym_dir / "exchange_trades.parquet"
        tape = pd.read_parquet(tape_path, columns=["ts", "price", "amount", "side"]) if tape_path.exists() else pd.DataFrame()

        # Numpy arrays for searchsorted
        c1m_ts = c1m["ts"].values
        c15m_ts = c15m["ts"].values
        l1_ts = l1["ts"].values if len(l1) > 0 else np.array([], dtype=np.int64)
        tape_ts = tape["ts"].values if len(tape) > 0 else np.array([], dtype=np.int64)
        tape_price = tape["price"].values if len(tape) > 0 else np.array([])
        tape_amount = tape["amount"].values if len(tape) > 0 else np.array([])
        tape_side = tape["side"].values if len(tape) > 0 else np.array([])

        labeled = 0
        for sig in sym_signals:
            ts_entry = _dt_to_ts(sig["timestamp"])
            ts_exit = ts_entry + HORIZON_S

            # Find exit price: last 1m candle with ts <= ts_exit
            exit_idx = np.searchsorted(c1m_ts, ts_exit, side="right") - 1
            if exit_idx < 0 or c1m_ts[exit_idx] <= ts_entry:
                continue  # no candle data after signal

            exit_price = float(c1m.iloc[exit_idx]["close"])
            entry_price = sig["entry_price"]
            side = sig["side"]

            if side == "buy":
                gross_return = (exit_price - entry_price) / entry_price
            else:
                gross_return = (entry_price - exit_price) / entry_price

            net_return = gross_return - fee_estimate
            win = 1 if net_return > 0 else 0

            row: dict = {
                "symbol": symbol,
                "ts_entry": ts_entry,
                "side": side,
                "entry_price": entry_price,
                "sl_price": sig["sl_price"],
                "tp_price": sig["tp_price"],
                "exit_price": exit_price,
                "hold_time_seconds": HORIZON_S,
                "exit_reason": "horizon_30m",
                "net_pnl": net_return * entry_price,  # approximate
                "net_return": net_return,
                "win": win,
            }

            # Market features from normalized data
            row.update(_l1_features(l1_ts, l1, ts_entry))
            row.update(_tape_features(tape_ts, tape_price, tape_amount, tape_side, ts_entry))
            row.update(_candles_1m_features(c1m_ts, c1m, ts_entry))
            row.update(_candles_15m_features(c15m_ts, c15m, ts_entry))

            # side_enc + hour_utc
            row["side_enc"] = 1.0 if side == "buy" else -1.0
            row["hour_utc"] = float(sig["timestamp"].hour)
            row["costs_mode"] = "provided_net"

            all_rows.append(row)
            labeled += 1

        logger.info(f"{symbol}: {len(sym_signals)} signals → {labeled} labeled")
        del tape, l1, c1m, c15m

    df = pd.DataFrame(all_rows)
    if len(df) > 0:
        df = df.sort_values("ts_entry").reset_index(drop=True)
        # Fill NaN features with 0
        feat_cols = [c for c in df.columns if c not in [
            "symbol", "ts_entry", "side", "entry_price", "sl_price", "tp_price",
            "exit_price", "hold_time_seconds", "exit_reason", "net_pnl", "net_return", "win",
            "costs_mode",
        ]]
        df[feat_cols] = df[feat_cols].fillna(0.0)

    return df


def build_features_and_labels_sl_tp(
    signals: list[dict],
    norm_dir: Path,
    fee_estimate: float,
    max_horizon_minutes: int = 180,
) -> pd.DataFrame:
    """Build features and SL/TP-simulated labels for each signal.

    Iterates 1m candles forward from entry. Checks TP/SL hit via high/low.
    If both hit in the same candle, SL wins (conservative).
    Slippage = rel_spread/2 from last L1 snapshot <= entry_ts.
    """
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for sig in signals:
        by_symbol[sig["symbol"]].append(sig)

    all_rows: list[dict] = []
    max_horizon_s = max_horizon_minutes * 60

    for symbol, sym_signals in by_symbol.items():
        sym_dir = norm_dir / symbol
        if not sym_dir.exists():
            logger.warning(f"{symbol}: no normalized data, skipping {len(sym_signals)} signals")
            continue

        # Load market data
        c1m = pd.read_parquet(sym_dir / "candles_1m.parquet")
        c15m = pd.read_parquet(sym_dir / "candles_15m.parquet")

        l1_path = sym_dir / "l1.parquet"
        l1 = pd.read_parquet(l1_path) if l1_path.exists() else pd.DataFrame()

        tape_path = sym_dir / "exchange_trades.parquet"
        tape = pd.read_parquet(tape_path, columns=["ts", "price", "amount", "side"]) if tape_path.exists() else pd.DataFrame()

        # Numpy arrays
        c1m_ts = c1m["ts"].values
        c1m_high = c1m["high"].values
        c1m_low = c1m["low"].values
        c1m_close = c1m["close"].values
        c15m_ts = c15m["ts"].values
        l1_ts = l1["ts"].values if len(l1) > 0 else np.array([], dtype=np.int64)
        l1_spread = l1["spread"].values if len(l1) > 0 else np.array([])
        l1_mid = l1["mid_price"].values if len(l1) > 0 else np.array([])
        tape_ts = tape["ts"].values if len(tape) > 0 else np.array([], dtype=np.int64)
        tape_price = tape["price"].values if len(tape) > 0 else np.array([])
        tape_amount = tape["amount"].values if len(tape) > 0 else np.array([])
        tape_side = tape["side"].values if len(tape) > 0 else np.array([])

        labeled = 0
        no_spread = 0
        for sig in sym_signals:
            ts_entry = _dt_to_ts(sig["timestamp"])
            entry_price = sig["entry_price"]
            sl_price = sig["sl_price"]
            tp_price = sig["tp_price"]
            side = sig["side"]

            if sl_price is None or tp_price is None:
                continue

            # Slippage estimate from L1 spread at entry
            slippage_est = 0.0
            has_spread = False
            if len(l1_ts) > 0:
                l1_idx = np.searchsorted(l1_ts, ts_entry, side="right") - 1
                if l1_idx >= 0:
                    spread = float(l1_spread[l1_idx])
                    mid = float(l1_mid[l1_idx])
                    if mid > 0:
                        slippage_est = (spread / mid) / 2
                        has_spread = True
            if not has_spread:
                no_spread += 1

            # Iterate 1m candles after entry
            start_idx = int(np.searchsorted(c1m_ts, ts_entry, side="right"))
            ts_deadline = ts_entry + max_horizon_s

            exit_reason = None
            exit_price_val = None
            exit_ts = None

            for i in range(start_idx, len(c1m_ts)):
                if c1m_ts[i] > ts_deadline:
                    break

                high = float(c1m_high[i])
                low = float(c1m_low[i])

                if side == "buy":
                    sl_hit = low <= sl_price
                    tp_hit = high >= tp_price
                else:
                    sl_hit = high >= sl_price
                    tp_hit = low <= tp_price

                if sl_hit and tp_hit:
                    exit_reason = "sl"
                    exit_price_val = sl_price
                    exit_ts = float(c1m_ts[i])
                    break
                elif sl_hit:
                    exit_reason = "sl"
                    exit_price_val = sl_price
                    exit_ts = float(c1m_ts[i])
                    break
                elif tp_hit:
                    exit_reason = "tp"
                    exit_price_val = tp_price
                    exit_ts = float(c1m_ts[i])
                    break

            if exit_reason is None:
                # Time exit: last candle at or before deadline
                end_idx = int(np.searchsorted(c1m_ts, ts_deadline, side="right")) - 1
                if end_idx < start_idx:
                    continue
                exit_reason = "time_exit"
                exit_price_val = float(c1m_close[end_idx])
                exit_ts = float(c1m_ts[end_idx])

            hold_seconds = int(exit_ts - ts_entry)

            if side == "buy":
                gross_return = (exit_price_val - entry_price) / entry_price
            else:
                gross_return = (entry_price - exit_price_val) / entry_price

            net_return = gross_return - fee_estimate - slippage_est
            win = 1 if net_return > 0 else 0

            row: dict = {
                "symbol": symbol,
                "ts_entry": ts_entry,
                "side": side,
                "entry_price": entry_price,
                "sl_price": sl_price,
                "tp_price": tp_price,
                "exit_price": exit_price_val,
                "exit_reason": exit_reason,
                "hold_time_seconds": hold_seconds,
                "hold_minutes": round(hold_seconds / 60, 1),
                "net_pnl": net_return * entry_price,
                "net_return": net_return,
                "win": win,
                "has_spread": int(has_spread),
            }

            # Market features
            row.update(_l1_features(l1_ts, l1, ts_entry))
            row.update(_tape_features(tape_ts, tape_price, tape_amount, tape_side, ts_entry))
            row.update(_candles_1m_features(c1m_ts, c1m, ts_entry))
            row.update(_candles_15m_features(c15m_ts, c15m, ts_entry))
            row["side_enc"] = 1.0 if side == "buy" else -1.0
            row["hour_utc"] = float(sig["timestamp"].hour)
            row["costs_mode"] = "provided_net"

            all_rows.append(row)
            labeled += 1

        if no_spread > 0:
            logger.warning(f"{symbol}: {no_spread}/{len(sym_signals)} signals without L1 spread data")
        logger.info(f"{symbol}: {len(sym_signals)} signals → {labeled} labeled (sl_tp_sim)")
        del tape, l1, c1m, c15m

    df = pd.DataFrame(all_rows)
    if len(df) > 0:
        df = df.sort_values("ts_entry").reset_index(drop=True)
        feat_cols = [c for c in df.columns if c not in [
            "symbol", "ts_entry", "side", "entry_price", "sl_price", "tp_price",
            "exit_price", "hold_time_seconds", "hold_minutes", "exit_reason",
            "net_pnl", "net_return", "win", "has_spread", "costs_mode",
        ]]
        df[feat_cols] = df[feat_cols].fillna(0.0)

    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate signals dataset from strategy replay")
    parser.add_argument("--config", type=str, default="config.yaml")
    parser.add_argument("--norm-dir", type=str, default="data/normalized")
    parser.add_argument("--out", type=str, default=None, help="Output path (auto-set per label mode if omitted)")
    parser.add_argument("--start", type=str, default=None, help="Override start_date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default=None, help="Override end_date (YYYY-MM-DD)")
    parser.add_argument("--label-mode", type=str, default="sl_tp_sim", choices=["horizon", "sl_tp_sim"])
    parser.add_argument("--max-horizon", type=int, default=180, help="Max horizon minutes for sl_tp_sim (default 180)")
    args = parser.parse_args()

    if args.out is None:
        args.out = {
            "horizon": "data/datasets/signals_labeled.parquet",
            "sl_tp_sim": "data/datasets/signals_sl_tp_labeled.parquet",
        }[args.label_mode]

    # Phase 1: collect signals
    signals = asyncio.run(collect_signals(args.config, args.start, args.end))

    if not signals:
        logger.warning("No signals collected, exiting")
        return

    # Fee estimate from config
    from app.config import Config
    config = Config.from_yaml(args.config)
    fee_estimate = float((config.costs.fee_entry_bps + config.costs.fee_exit_bps) / Decimal("10000"))
    logger.info(f"Fee estimate (fees only): {fee_estimate:.4f} (entry {config.costs.fee_entry_bps} + exit {config.costs.fee_exit_bps} bps)")

    # Phase 2+3: features + labels
    if args.label_mode == "sl_tp_sim":
        logger.info(f"Label mode: sl_tp_sim (max_horizon={args.max_horizon}m, slippage from L1)")
        df = build_features_and_labels_sl_tp(
            signals, Path(args.norm_dir), fee_estimate, args.max_horizon,
        )
    else:
        df = build_features_and_labels(signals, Path(args.norm_dir), fee_estimate)

    # Save
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)

    wr = df["win"].mean() if len(df) > 0 else 0.0
    logger.info(f"Saved {len(df)} labeled signals to {out_path}")
    logger.info(f"  WR={wr:.3f}, mean_ret={df['net_return'].mean():.5f}, sum_ret={df['net_return'].sum():.5f}")
    logger.info(f"  Symbols: {df['symbol'].value_counts().to_dict()}")
    if "exit_reason" in df.columns:
        logger.info(f"  Exit reasons: {df['exit_reason'].value_counts().to_dict()}")
    if "hold_minutes" in df.columns:
        logger.info(f"  Hold time: median={df['hold_minutes'].median():.1f}m, mean={df['hold_minutes'].mean():.1f}m")


if __name__ == "__main__":
    main()
