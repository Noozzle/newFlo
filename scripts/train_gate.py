"""Walk-forward training for AI gate model.

Dataset spec
============
- Candidate = moment when strategy emits entry signal (option A)
- Features: 10 floats from FeatureSnapshot (data <= t, closed candles only)
- Label: net_R = net_pnl / (risk_distance * size), win = net_R > 0
- Costs included in net_R (fees + slippage from trades.csv)
- Walk-forward: time-sorted, NO shuffle, first train_ratio for train

Usage:
    1. Run backtest with ai_gate.log_signals=true (no model needed)
       → produces gate_signals.csv (features) + reports/<run>/trades.csv (outcomes)
    2. python scripts/train_gate.py --signals gate_signals.csv --trades reports/<run>/trades.csv
       → trains model, saves to models/gate.joblib

Anti-leakage:
    - Features from gate_signals.csv (logged at signal time, only data <= t)
    - Labels from trades.csv (separate file, joined by timestamp+symbol)
    - Walk-forward split only, no shuffle, no cross-validation
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

# Must match FeatureSnapshot field order in ai_gate.py
FEATURE_COLS = [
    "volume_imbalance",
    "atr_pct",
    "spread_pct",
    "ob_imbalance",
    "ob_delta",
    "m1_ret_3",
    "m1_ret_10",
    "m1_volatility",
    "side_enc",
    "trend_strength",
]


def load_and_join(signals_path: str, trades_path: str) -> pd.DataFrame:
    """Join gate signals with trade outcomes.

    Match by symbol + side + closest entry_time (tolerance < 5s).
    Computes net_R and win label.

    Anti-leakage: features come from signals (logged at t),
    labels come from trades (separate source, future outcome).
    """
    sig = pd.read_csv(signals_path)
    trades = pd.read_csv(trades_path)

    sig["timestamp"] = pd.to_datetime(sig["timestamp"])
    trades["entry_time"] = pd.to_datetime(trades["entry_time"])

    rows = []
    used_trade_ids = set()

    for _, s in sig.iterrows():
        mask = (trades["symbol"] == s["symbol"]) & (trades["side"] == s["side"])
        candidates = trades[mask].copy()
        if candidates.empty:
            continue

        # Exclude already-matched trades (1:1 mapping)
        candidates = candidates[~candidates["trade_id"].isin(used_trade_ids)]
        if candidates.empty:
            continue

        candidates["dt"] = (candidates["entry_time"] - s["timestamp"]).abs()
        best_idx = candidates["dt"].idxmin()
        best = candidates.loc[best_idx]

        if best["dt"].total_seconds() > 5:
            continue

        used_trade_ids.add(best["trade_id"])

        # Features (from signal, data <= t)
        row = {col: float(s[col]) for col in FEATURE_COLS}

        # Meta
        row["timestamp"] = s["timestamp"]
        row["symbol"] = s["symbol"]
        row["side"] = s["side"]
        row["entry_price"] = float(s["entry_price"])
        row["sl_price"] = float(s["sl_price"])
        row["tp_price"] = float(s["tp_price"])

        # Risk distance
        risk_dist = abs(float(s["entry_price"]) - float(s["sl_price"]))
        row["risk_distance"] = risk_dist

        # Trade outcome (from trades.csv — separate source)
        row["exit_price"] = float(best["exit_price"])
        row["exit_reason"] = best["exit_reason"]
        size = float(best["size"])
        row["size"] = size
        row["gross_pnl"] = float(best["gross_pnl"])
        row["fees"] = float(best["fees"])
        row["slippage_est"] = float(best["slippage_estimate"])
        row["net_pnl"] = float(best["net_pnl"])

        # Label: net_R = net_pnl / (risk_distance * size)
        risk_amount = risk_dist * size
        row["net_R"] = float(best["net_pnl"]) / risk_amount if risk_amount > 0 else 0.0
        row["win"] = 1 if row["net_R"] > 0 else 0

        rows.append(row)

    df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)

    n_win = df["win"].sum()
    n_loss = len(df) - n_win
    avg_r_win = df.loc[df["win"] == 1, "net_R"].mean() if n_win else 0
    avg_r_loss = df.loc[df["win"] == 0, "net_R"].mean() if n_loss else 0

    logger.info(
        f"Dataset: {len(df)} samples, {n_win}W/{n_loss}L "
        f"(WR={n_win/len(df):.1%}), avg_R win={avg_r_win:+.2f} loss={avg_r_loss:+.2f}"
    )
    return df


def save_dataset(df: pd.DataFrame, path: str) -> None:
    """Save full labeled dataset to CSV for inspection."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    logger.info(f"Dataset saved to {out} ({len(df)} rows)")


def train_walk_forward(
    df: pd.DataFrame,
    train_ratio: float = 0.7,
    output_path: str = "models/gate.joblib",
    full_threshold: float = 0.5,
    half_threshold: float = 0.3,
) -> None:
    """Walk-forward train: first train_ratio for train, rest for test.

    Anti-leakage: time-sorted, no shuffle, train strictly before test.
    """
    from lightgbm import LGBMClassifier

    n_train = int(len(df) * train_ratio)
    if n_train < 10:
        logger.error(f"Too few training samples: {n_train}")
        sys.exit(1)

    train = df.iloc[:n_train]
    test = df.iloc[n_train:]

    X_train = train[FEATURE_COLS].values
    y_train = train["win"].values
    X_test = test[FEATURE_COLS].values
    y_test = test["win"].values
    net_r_test = test["net_R"].values

    logger.info(
        f"Walk-forward split: train={len(train)} "
        f"[{train['timestamp'].iloc[0]} → {train['timestamp'].iloc[-1]}], "
        f"test={len(test)} "
        f"[{test['timestamp'].iloc[0]} → {test['timestamp'].iloc[-1]}]"
    )

    # Conservative hyperparams for small dataset (~100 samples)
    model = LGBMClassifier(
        n_estimators=100,
        max_depth=3,
        num_leaves=8,
        min_child_samples=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=1.0,
        reg_lambda=1.0,
        random_state=42,
        verbose=-1,
    )
    model.fit(X_train, y_train)

    # ── Evaluation ──
    train_acc = (model.predict(X_train) == y_train).mean()
    test_acc = (model.predict(X_test) == y_test).mean()

    probs_test = model.predict_proba(X_test)[:, 1]

    # Gate decisions on test set
    full_mask = probs_test >= full_threshold
    half_mask = (probs_test >= half_threshold) & (probs_test < full_threshold)
    skip_mask = probs_test < half_threshold

    logger.info(f"Train acc: {train_acc:.3f}, Test acc: {test_acc:.3f}")
    logger.info(
        f"Test gate decisions: FULL={full_mask.sum()}, "
        f"HALF={half_mask.sum()}, SKIP={skip_mask.sum()}"
    )

    # WR and E[R] by gate action
    for name, mask in [("FULL", full_mask), ("HALF", half_mask), ("SKIP", skip_mask)]:
        if not mask.any():
            continue
        wr = y_test[mask].mean()
        avg_r = net_r_test[mask].mean()
        logger.info(f"  {name}: WR={wr:.3f}, avg_net_R={avg_r:+.3f} (n={mask.sum()})")

    # Simulated P&L: FULL at 1.0, HALF at 0.5, SKIP at 0.0
    sim_r = np.where(full_mask, net_r_test, np.where(half_mask, net_r_test * 0.5, 0.0))
    baseline_r = net_r_test.sum()
    gated_r = sim_r.sum()
    logger.info(
        f"Test sum(net_R): baseline={baseline_r:+.3f}, "
        f"gated={gated_r:+.3f} (delta={gated_r - baseline_r:+.3f})"
    )

    # Feature importance
    importances = sorted(
        zip(FEATURE_COLS, model.feature_importances_),
        key=lambda x: -x[1],
    )
    logger.info("Feature importance:")
    for name, imp in importances:
        logger.info(f"  {name}: {imp}")

    # Save model
    import joblib
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, out)
    logger.info(f"Model saved to {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Train AI gate model (walk-forward)")
    parser.add_argument("--signals", required=True, help="gate_signals.csv from backtest")
    parser.add_argument("--trades", required=True, help="trades.csv from same backtest report")
    parser.add_argument("--output", default="models/gate.joblib", help="Model output path")
    parser.add_argument("--train-ratio", type=float, default=0.7, help="Train split ratio")
    parser.add_argument("--full-threshold", type=float, default=0.5, help="P(win) >= this → FULL")
    parser.add_argument("--half-threshold", type=float, default=0.3, help="P(win) >= this → HALF")
    parser.add_argument("--save-dataset", default=None, help="Save joined dataset CSV (optional)")
    args = parser.parse_args()

    df = load_and_join(args.signals, args.trades)

    if args.save_dataset:
        save_dataset(df, args.save_dataset)

    if len(df) < 15:
        logger.error(f"Only {len(df)} labeled samples — need >=15 for walk-forward")
        sys.exit(1)

    train_walk_forward(
        df,
        train_ratio=args.train_ratio,
        output_path=args.output,
        full_threshold=args.full_threshold,
        half_threshold=args.half_threshold,
    )


if __name__ == "__main__":
    main()
