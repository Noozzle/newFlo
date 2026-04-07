"""Train AI gate models on labeled dataset.

Modes:
    single  — 70/30 time split (original)
    wf      — walk-forward expanding-window rolling folds (default)

Walk-forward: n_folds expanding windows, TOPK K grid search,
select K by median delta across folds.

Usage:
    python -m scripts.ai_gate.train --data data/datasets/signals_sl_tp_labeled.parquet --out model
    python -m scripts.ai_gate.train --data data/datasets/signals_sl_tp_labeled.parquet --mode single
"""

from __future__ import annotations

import argparse
import json
import pickle
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

# Non-feature columns (meta/label). Superset across all dataset variants.
META_COLS = [
    "symbol", "ts_entry", "side", "entry_price", "exit_price",
    "hold_time_seconds", "hold_minutes", "exit_reason",
    "net_pnl", "net_return", "win",
    "sl_price", "tp_price", "has_spread", "costs_mode",
]


def _get_feature_cols(df: pd.DataFrame) -> list[str]:
    """All columns that are not meta/label."""
    return [c for c in df.columns if c not in META_COLS]


def _encode_side(df: pd.DataFrame) -> pd.DataFrame:
    """Encode 'side' as numeric feature if present in feature cols."""
    if "side_enc" not in df.columns and "side" in df.columns:
        df = df.copy()
        df["side_enc"] = df["side"].map({"buy": 1.0, "sell": -1.0}).fillna(0.0)
    return df


def _topk_masks(probs: np.ndarray, k_pct: int) -> tuple[np.ndarray, float]:
    """Top K% by p_win -> FULL mask + cutoff value."""
    n_full = max(1, int(len(probs) * k_pct / 100))
    cutoff = np.sort(probs)[::-1][min(n_full, len(probs)) - 1]
    full_mask = probs >= cutoff
    return full_mask, float(cutoff)


def _max_drawdown(returns: np.ndarray) -> float:
    """Max drawdown from cumulative return series."""
    if len(returns) == 0:
        return 0.0
    cum = np.cumsum(returns)
    peak = np.maximum.accumulate(cum)
    dd = cum - peak
    return float(dd.min())


# ---------------------------------------------------------------------------
# Walk-forward rolling fold training
# ---------------------------------------------------------------------------

def train_walk_forward(
    data_path: Path,
    out_dir: Path,
    n_folds: int = 5,
    k_values: list[int] | None = None,
) -> None:
    """Walk-forward expanding-window training with TOPK gate evaluation."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score

    if k_values is None:
        k_values = [20, 30, 40, 50]

    df = pd.read_parquet(data_path)
    df = df.sort_values("ts_entry").reset_index(drop=True)
    df = _encode_side(df)

    # Cost accounting check
    if "costs_mode" in df.columns:
        logger.info(f"costs_mode: {df['costs_mode'].value_counts().to_dict()}")
    else:
        logger.info("No costs_mode column -- net_return assumed to include costs already")

    feature_cols = _get_feature_cols(df)
    logger.info(f"Dataset: {len(df)} rows, {len(feature_cols)} features")
    logger.info(f"Features: {feature_cols}")

    n = len(df)
    block_size = n // (n_folds + 1)
    logger.info(f"Walk-forward: {n_folds} folds, block_size~={block_size}")

    fold_results: list[dict] = []

    for fold in range(n_folds):
        train_end = block_size * (fold + 1)
        test_start = train_end
        test_end = train_end + block_size if fold < n_folds - 1 else n

        train_df = df.iloc[:train_end]
        test_df = df.iloc[test_start:test_end]

        X_train = train_df[feature_cols].values.astype(np.float64)
        y_train = train_df["win"].values.astype(int)
        X_test = test_df[feature_cols].values.astype(np.float64)
        y_test = test_df["win"].values.astype(int)
        ret_test = test_df["net_return"].values

        # Class balancing
        n_pos = int(y_train.sum())
        n_neg = len(y_train) - n_pos
        pos_weight = n_neg / max(n_pos, 1)
        sw = np.where(y_train == 1, pos_weight, 1.0)

        hgb = HistGradientBoostingClassifier(
            max_iter=200, max_depth=3, max_leaf_nodes=8,
            min_samples_leaf=5, learning_rate=0.05,
            l2_regularization=1.0, random_state=42,
        )
        hgb.fit(X_train, y_train, sample_weight=sw)

        probs = hgb.predict_proba(X_test)[:, 1]
        try:
            auc = roc_auc_score(y_test, probs)
        except Exception:
            auc = float("nan")

        baseline_sum = float(ret_test.sum())
        baseline_dd = _max_drawdown(ret_test)

        logger.info(
            f"\nFold {fold+1}/{n_folds}: train={len(train_df)}, test={len(test_df)}, "
            f"WR_train={y_train.mean():.3f}, WR_test={y_test.mean():.3f}, AUC={auc:.3f}"
        )
        logger.info(f"  Baseline: sum_ret={baseline_sum:+.4f}, max_dd={baseline_dd:+.4f}")

        for k in k_values:
            full_mask, cutoff = _topk_masks(probs, k)
            scales = np.where(full_mask, 1.0, 0.5)
            gated_ret = ret_test * scales
            gated_sum = float(gated_ret.sum())
            gated_dd = _max_drawdown(gated_ret)
            full_n = int(full_mask.sum())
            full_wr = float(y_test[full_mask].mean()) if full_mask.any() else 0.0
            delta = gated_sum - baseline_sum

            logger.info(
                f"    K={k:>2}%: FULL={full_n:>4} WR={full_wr:.3f}, "
                f"sum={gated_sum:+.4f}, dd={gated_dd:+.4f}, delta={delta:+.4f}"
            )

            fold_results.append({
                "fold": fold + 1, "k": k,
                "baseline_sum_ret": baseline_sum, "baseline_max_dd": baseline_dd,
                "gated_sum_ret": gated_sum, "gated_max_dd": gated_dd,
                "full_count": full_n, "full_wr": full_wr,
                "delta": delta, "auc": auc,
            })

    # ── Aggregate ──
    res = pd.DataFrame(fold_results)

    logger.info(f"\n{'='*60}")
    logger.info("AGGREGATE (median across folds):")
    logger.info(f"  {'K':>4} | {'med_delta':>10} | {'med_FULL_WR':>11} | {'med_sum':>10} | {'med_dd':>10}")
    logger.info(f"  {'-'*55}")

    # Also log per-fold baseline for context
    baseline_sums = res.groupby("fold")["baseline_sum_ret"].first()
    logger.info(f"  Baseline median sum_ret: {baseline_sums.median():+.4f}")

    k_median_delta: dict[int, float] = {}
    for k in k_values:
        kdf = res[res["k"] == k]
        md = float(kdf["delta"].median())
        mwr = float(kdf["full_wr"].median())
        ms = float(kdf["gated_sum_ret"].median())
        mdd = float(kdf["gated_max_dd"].median())
        k_median_delta[k] = md
        logger.info(f"  {k:>3}% | {md:>+10.4f} | {mwr:>10.3f} | {ms:>+10.4f} | {mdd:>+10.4f}")

    best_k = max(k_median_delta, key=k_median_delta.get)
    logger.info(f"\n  -> Best K = {best_k}%  (median delta = {k_median_delta[best_k]:+.4f})")

    # ── Per-fold detail table for best K ──
    logger.info(f"\nPer-fold detail (K={best_k}%):")
    best_rows = res[res["k"] == best_k]
    for _, r in best_rows.iterrows():
        logger.info(
            f"  Fold {int(r['fold'])}: baseline={r['baseline_sum_ret']:+.4f} dd={r['baseline_max_dd']:+.4f} | "
            f"gated={r['gated_sum_ret']:+.4f} dd={r['gated_max_dd']:+.4f} | "
            f"FULL={int(r['full_count'])} WR={r['full_wr']:.3f} | delta={r['delta']:+.4f}"
        )

    # ── Train final model on full data ──
    logger.info(f"\nTraining final model on all {n} samples ...")
    X_all = df[feature_cols].values.astype(np.float64)
    y_all = df["win"].values.astype(int)
    n_pos = int(y_all.sum())
    n_neg = len(y_all) - n_pos
    sw_all = np.where(y_all == 1, n_neg / max(n_pos, 1), 1.0)

    final = HistGradientBoostingClassifier(
        max_iter=200, max_depth=3, max_leaf_nodes=8,
        min_samples_leaf=5, learning_rate=0.05,
        l2_regularization=1.0, random_state=42,
    )
    final.fit(X_all, y_all, sample_weight=sw_all)

    probs_all = final.predict_proba(X_all)[:, 1]
    _, topk_cutoff = _topk_masks(probs_all, best_k)
    logger.info(f"Final model TOPK cutoff (K={best_k}%): {topk_cutoff:.4f}")

    # ── Export ──
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / "ai_gate_model.pkl", "wb") as f:
        pickle.dump(final, f)
    logger.info(f"Model saved: {out_dir / 'ai_gate_model.pkl'}")

    config = {
        "feature_list": feature_cols,
        "model_type": "HistGradientBoostingClassifier",
        "gate_mode": "TOPK",
        "thresholds": {
            "topk_pct": best_k,
            "topk_cutoff": float(topk_cutoff),
        },
        "walk_forward": {
            "n_folds": n_folds,
            "k_values": k_values,
            "best_k": best_k,
            "median_delta": float(k_median_delta[best_k]),
            "per_k_median_delta": {str(k): float(v) for k, v in k_median_delta.items()},
        },
        "costs": {
            "fee_estimate": 0.00075,
            "slippage_mode": "per_signal_l1_spread",
        },
        "train_samples": n,
        "train_wr": float(y_all.mean()),
        "version": datetime.now(timezone.utc).isoformat(),
    }

    with open(out_dir / "config.json", "w") as f:
        json.dump(config, f, indent=2)
    logger.info(f"Config saved: {out_dir / 'config.json'}")


# ---------------------------------------------------------------------------
# Single-split training (original)
# ---------------------------------------------------------------------------

def _report_gate_sim(
    y_true: np.ndarray,
    net_ret: np.ndarray,
    probs: np.ndarray,
    t2: float,
    label: str,
) -> None:
    """Simulate 2-action gate (HALF/FULL) and report stats."""
    full_mask = probs >= t2
    half_mask = ~full_mask

    gated_ret = np.where(full_mask, net_ret, net_ret * 0.5)

    logger.info(f"  Gate sim ({label}, t2={t2:.2f}): HALF={half_mask.sum()}, FULL={full_mask.sum()}")

    for name, mask in [("FULL", full_mask), ("HALF", half_mask)]:
        if not mask.any():
            continue
        wr = y_true[mask].mean()
        avg_r = net_ret[mask].mean()
        logger.info(f"    {name}: n={mask.sum()}, WR={wr:.3f}, avg_ret={avg_r:+.5f}")

    baseline_sum = net_ret.sum()
    gated_sum = gated_ret.sum()
    logger.info(f"  sum(ret): baseline={baseline_sum:+.5f}, gated={gated_sum:+.5f}, delta={gated_sum - baseline_sum:+.5f}")


def _grid_search_t2(
    net_ret: np.ndarray,
    probs: np.ndarray,
) -> float:
    """Grid search t2 in [0.40..0.70] to maximize mean(gated_return).

    Constraint: FULL trades >= 50% of total.
    """
    best_t2 = 0.40
    best_mean = -np.inf
    any_valid = False

    for t2_100 in range(40, 71):
        t2 = t2_100 / 100.0
        full_mask = probs >= t2
        n_full = full_mask.sum()

        # constraint: at least 50% FULL
        if n_full < len(probs) * 0.5:
            continue

        scales = np.where(full_mask, 1.0, 0.5)
        gated_ret = net_ret * scales
        mean_ret = gated_ret.mean()

        if mean_ret > best_mean:
            best_mean = mean_ret
            best_t2 = t2
            any_valid = True

    if not any_valid:
        best_t2 = 0.40
        scales = np.where(probs >= best_t2, 1.0, 0.5)
        best_mean = (net_ret * scales).mean()
        logger.info(f"  Grid search: no t2 met 50% FULL constraint, defaulting to t2={best_t2:.2f}")

    logger.info(f"  Grid search best: t2={best_t2:.2f}, mean_gated_ret={best_mean:+.5f}")
    return best_t2


def _grid_search_topk(
    net_ret: np.ndarray,
    probs: np.ndarray,
) -> tuple[int, float]:
    """Evaluate K in {10,20,30,40,50}, pick best by mean(gated_return)."""
    best_k = 30
    best_mean = -np.inf
    best_cutoff = 0.5

    for k in [10, 20, 30, 40, 50]:
        full_mask, cutoff = _topk_masks(probs, k)
        scales = np.where(full_mask, 1.0, 0.5)
        gated_ret = net_ret * scales
        mean_ret = gated_ret.mean()
        n_full = full_mask.sum()
        logger.info(
            f"    K={k:>2}%: FULL={n_full:>3}, cutoff={cutoff:.3f}, "
            f"mean_ret={mean_ret:+.5f}"
        )
        if mean_ret > best_mean:
            best_mean = mean_ret
            best_k = k
            best_cutoff = cutoff

    logger.info(f"  TOPK best: K={best_k}%, mean_gated_ret={best_mean:+.5f}")
    return best_k, best_cutoff


def _report_topk(
    y_true: np.ndarray,
    net_ret: np.ndarray,
    probs: np.ndarray,
    k_pct: int,
    label: str,
) -> None:
    """Report TOPK gate stats."""
    full_mask, cutoff = _topk_masks(probs, k_pct)
    half_mask = ~full_mask
    gated_ret = np.where(full_mask, net_ret, net_ret * 0.5)

    logger.info(f"  TOPK ({label}, K={k_pct}%, cutoff={cutoff:.3f}): HALF={half_mask.sum()}, FULL={full_mask.sum()}")
    for name, mask in [("FULL", full_mask), ("HALF", half_mask)]:
        if not mask.any():
            continue
        wr = y_true[mask].mean()
        avg_r = net_ret[mask].mean()
        logger.info(f"    {name}: n={mask.sum()}, WR={wr:.3f}, avg_ret={avg_r:+.5f}")

    baseline_sum = net_ret.sum()
    gated_sum = gated_ret.sum()
    logger.info(f"  sum(ret): baseline={baseline_sum:+.5f}, gated={gated_sum:+.5f}, delta={gated_sum - baseline_sum:+.5f}")


def _report_regression_gate(
    y_true: np.ndarray,
    net_ret: np.ndarray,
    pred_ret: np.ndarray,
    label: str,
) -> None:
    """Report regression-driven gate: pred >= 0 -> FULL, else HALF."""
    full_mask = pred_ret >= 0
    half_mask = ~full_mask
    gated_ret = np.where(full_mask, net_ret, net_ret * 0.5)

    logger.info(f"  Regression gate ({label}): HALF={half_mask.sum()}, FULL={full_mask.sum()}")
    for name, mask in [("FULL", full_mask), ("HALF", half_mask)]:
        if not mask.any():
            continue
        wr = y_true[mask].mean()
        avg_r = net_ret[mask].mean()
        logger.info(f"    {name}: n={mask.sum()}, WR={wr:.3f}, avg_ret={avg_r:+.5f}")

    baseline_sum = net_ret.sum()
    gated_sum = gated_ret.sum()
    logger.info(f"  sum(ret): baseline={baseline_sum:+.5f}, gated={gated_sum:+.5f}, delta={gated_sum - baseline_sum:+.5f}")
    logger.info(f"  pred_ret dist: min={pred_ret.min():.5f}, median={np.median(pred_ret):.5f}, max={pred_ret.max():.5f}")


def train(
    data_path: Path,
    out_dir: Path,
    train_ratio: float = 0.7,
) -> None:
    """Single-split train: 70/30 time split, LR + HGB + regressor."""
    df = pd.read_parquet(data_path)
    df = df.sort_values("ts_entry").reset_index(drop=True)
    df = _encode_side(df)

    feature_cols = _get_feature_cols(df)
    logger.info(f"Dataset: {len(df)} rows, {len(feature_cols)} features")
    logger.info(f"Features: {feature_cols}")

    # Walk-forward split
    n_train = int(len(df) * train_ratio)
    train_df = df.iloc[:n_train]
    test_df = df.iloc[n_train:]

    X_train = train_df[feature_cols].values.astype(np.float64)
    y_train = train_df["win"].values.astype(int)
    ret_train = train_df["net_return"].values

    X_test = test_df[feature_cols].values.astype(np.float64)
    y_test = test_df["win"].values.astype(int)
    ret_test = test_df["net_return"].values

    logger.info(
        f"Split: train={n_train} [{train_df['ts_entry'].iloc[0]} -> {train_df['ts_entry'].iloc[-1]}], "
        f"test={len(test_df)} [{test_df['ts_entry'].iloc[0]} -> {test_df['ts_entry'].iloc[-1]}]"
    )
    logger.info(f"Train WR={y_train.mean():.3f}, Test WR={y_test.mean():.3f}")

    # ── 1) Baseline: LogisticRegression ──
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    lr = LogisticRegression(max_iter=1000, C=0.1, class_weight="balanced", random_state=42)
    lr.fit(X_train_s, y_train)

    lr_probs_test = lr.predict_proba(X_test_s)[:, 1]
    lr_acc = (lr.predict(X_test_s) == y_test).mean()
    logger.info(f"\n[Baseline] LogisticRegression (balanced) -- test acc={lr_acc:.3f}")
    logger.info(f"  p_win dist: min={lr_probs_test.min():.3f}, median={np.median(lr_probs_test):.3f}, max={lr_probs_test.max():.3f}")

    try:
        from sklearn.metrics import roc_auc_score
        lr_auc = roc_auc_score(y_test, lr_probs_test)
        logger.info(f"  LR AUC={lr_auc:.3f}")
    except Exception:
        lr_auc = None

    # ── 2) Main: HistGradientBoostingClassifier ──
    from sklearn.ensemble import HistGradientBoostingClassifier

    n_pos = y_train.sum()
    n_neg = len(y_train) - n_pos
    pos_weight = n_neg / max(n_pos, 1)
    sample_weight = np.where(y_train == 1, pos_weight, 1.0)
    logger.info(f"  Sample weights: n_pos={n_pos}, n_neg={n_neg}, pos_weight={pos_weight:.2f}")

    hgb = HistGradientBoostingClassifier(
        max_iter=200,
        max_depth=3,
        max_leaf_nodes=8,
        min_samples_leaf=5,
        learning_rate=0.05,
        l2_regularization=1.0,
        random_state=42,
    )
    hgb.fit(X_train, y_train, sample_weight=sample_weight)

    hgb_probs_test = hgb.predict_proba(X_test)[:, 1]
    hgb_acc = (hgb.predict(X_test) == y_test).mean()
    logger.info(f"\n[Main] HistGradientBoosting (weighted) -- test acc={hgb_acc:.3f}")
    logger.info(f"  p_win dist: min={hgb_probs_test.min():.3f}, median={np.median(hgb_probs_test):.3f}, max={hgb_probs_test.max():.3f}")

    try:
        hgb_auc = roc_auc_score(y_test, hgb_probs_test)
        logger.info(f"  HGB AUC={hgb_auc:.3f}")
    except Exception:
        hgb_auc = None

    # ── 3) Optional: regression on net_return ──
    from sklearn.ensemble import HistGradientBoostingRegressor

    hgb_reg = HistGradientBoostingRegressor(
        max_iter=200,
        max_depth=3,
        max_leaf_nodes=8,
        min_samples_leaf=5,
        learning_rate=0.05,
        l2_regularization=1.0,
        random_state=42,
    )
    hgb_reg.fit(X_train, ret_train)

    pred_ret_test = hgb_reg.predict(X_test)
    corr = np.corrcoef(pred_ret_test, ret_test)[0, 1] if len(ret_test) > 2 else 0
    logger.info(f"\n[Regressor] HistGradientBoosting -- pred/actual corr={corr:.3f}")

    # ── Regression gate evaluation on test ──
    logger.info(f"\n[Regression gate] test report:")
    _report_regression_gate(y_test, ret_test, pred_ret_test, "REG-test")

    # ── 4) Grid search t2 on validation (train tail ~20%) ──
    val_size = max(int(n_train * 0.2), 2)
    val_idx = slice(n_train - val_size, n_train)
    X_val = train_df.iloc[val_idx][feature_cols].values.astype(np.float64)
    ret_val = train_df.iloc[val_idx]["net_return"].values
    probs_val = hgb.predict_proba(X_val)[:, 1]

    logger.info(f"\n[Grid search] validation={val_size} (train tail)")
    t2 = _grid_search_t2(ret_val, probs_val)

    # Report on test with chosen t2
    logger.info(f"\n[Test report] t2={t2:.2f}")
    _report_gate_sim(y_test, ret_test, hgb_probs_test, t2, "HGB-test")
    _report_gate_sim(y_test, ret_test, lr_probs_test, t2, "LR-test")

    # ── 5) Grid search TOPK on validation ──
    logger.info(f"\n[TOPK grid search] validation={val_size}")
    best_k, topk_cutoff_val = _grid_search_topk(ret_val, probs_val)

    # Recompute cutoff on full training probs for live inference
    probs_train_all = hgb.predict_proba(X_train)[:, 1]
    _, topk_cutoff = _topk_masks(probs_train_all, best_k)

    logger.info(f"\n[Test report] TOPK K={best_k}%")
    _report_topk(y_test, ret_test, hgb_probs_test, best_k, "HGB-test")

    # ── Export ──
    out_dir.mkdir(parents=True, exist_ok=True)

    model_path = out_dir / "ai_gate_model.pkl"
    with open(model_path, "wb") as f:
        pickle.dump(hgb, f)
    logger.info(f"Model saved: {model_path}")

    reg_path = out_dir / "ai_gate_regressor.pkl"
    with open(reg_path, "wb") as f:
        pickle.dump(hgb_reg, f)
    logger.info(f"Regressor saved: {reg_path}")

    config = {
        "feature_list": feature_cols,
        "model_type": "HistGradientBoostingClassifier",
        "gate_mode": "classifier",
        "thresholds": {"t2_full": t2, "topk_pct": best_k, "topk_cutoff": topk_cutoff},
        "train_range": {
            "ts_start": int(train_df["ts_entry"].iloc[0]),
            "ts_end": int(train_df["ts_entry"].iloc[-1]),
            "n_samples": n_train,
        },
        "test_range": {
            "ts_start": int(test_df["ts_entry"].iloc[0]),
            "ts_end": int(test_df["ts_entry"].iloc[-1]),
            "n_samples": len(test_df),
        },
        "metrics": {
            "test_acc_lr": float(lr_acc),
            "test_acc_hgb": float(hgb_acc),
            "test_auc_lr": float(lr_auc) if lr_auc is not None else None,
            "test_auc_hgb": float(hgb_auc) if hgb_auc is not None else None,
            "test_wr": float(y_test.mean()),
            "test_mean_ret": float(ret_test.mean()),
            "regressor_corr": float(corr),
        },
        "version": datetime.now(timezone.utc).isoformat(),
    }

    config_path = out_dir / "config.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    logger.info(f"Config saved: {config_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Train AI gate model")
    parser.add_argument("--data", type=str, default="data/datasets/signals_sl_tp_labeled.parquet")
    parser.add_argument("--out", type=str, default="model")
    parser.add_argument("--mode", type=str, default="wf", choices=["wf", "single"],
                        help="wf = walk-forward rolling folds (default), single = 70/30 split")
    parser.add_argument("--n-folds", type=int, default=5)
    parser.add_argument("--k-values", type=str, default="20,30,40,50",
                        help="Comma-separated K values for TOPK grid search")
    parser.add_argument("--train-ratio", type=float, default=0.7, help="Train ratio (single mode only)")
    args = parser.parse_args()

    if args.mode == "wf":
        k_values = [int(x) for x in args.k_values.split(",")]
        train_walk_forward(
            data_path=Path(args.data),
            out_dir=Path(args.out),
            n_folds=args.n_folds,
            k_values=k_values,
        )
    else:
        train(
            data_path=Path(args.data),
            out_dir=Path(args.out),
            train_ratio=args.train_ratio,
        )


if __name__ == "__main__":
    main()
