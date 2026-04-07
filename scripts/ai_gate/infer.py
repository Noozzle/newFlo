"""AI gate inference and backtest evaluation.

Gate logic (2-action, no SKIP):
    p_win < t2  → HALF  (risk × 0.5)
    p_win >= t2 → FULL  (risk × 1.0)

Single-row inference:
    python -m scripts.ai_gate.infer predict --model model --features '{"rel_spread":0.0001,...}'

Backtest evaluation on labeled dataset:
    python -m scripts.ai_gate.infer evaluate --model model --data data/datasets/mytrades_labeled.parquet
"""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger


def load_gate(model_dir: Path) -> tuple[object, dict, object | None]:
    """Load classifier + config + optional regressor."""
    with open(model_dir / "ai_gate_model.pkl", "rb") as f:
        model = pickle.load(f)

    with open(model_dir / "config.json") as f:
        config = json.load(f)

    reg_path = model_dir / "ai_gate_regressor.pkl"
    regressor = None
    if reg_path.exists():
        with open(reg_path, "rb") as f:
            regressor = pickle.load(f)

    t2 = config["thresholds"]["t2_full"]
    topk = config["thresholds"].get("topk_pct")
    mode = config.get("gate_mode", "classifier")
    extra = f", topk={topk}%" if topk else ""
    reg_str = ", regressor=yes" if regressor else ""
    logger.info(f"Loaded {config['model_type']} (t2={t2}{extra}, mode={mode}{reg_str})")
    return model, config, regressor


def decide(p_win: float, t2: float) -> str:
    """Gate decision: HALF or FULL (no SKIP)."""
    return "FULL" if p_win >= t2 else "HALF"


def risk_scale(action: str) -> float:
    """Action → multiplier."""
    return {"HALF": 0.5, "FULL": 1.0}[action]


# ── Single prediction ───────────────────────────────────────────


def predict_single(
    model, config: dict, features: dict[str, float], regressor=None,
) -> dict:
    """Predict on a single feature dict."""
    feat_list = config["feature_list"]
    t2 = config["thresholds"]["t2_full"]
    mode = config.get("gate_mode", "classifier")

    X = np.array([[features.get(f, 0.0) for f in feat_list]])
    p_win = float(model.predict_proba(X)[0, 1])

    if mode == "regression" and regressor is not None:
        pred_ret = float(regressor.predict(X)[0])
        action = "FULL" if pred_ret >= 0 else "HALF"
        return {"p_win": p_win, "pred_ret": pred_ret, "action": action, "risk_scale": risk_scale(action)}

    action = decide(p_win, t2)
    return {"p_win": p_win, "action": action, "risk_scale": risk_scale(action)}


# ── Backtest evaluation ─────────────────────────────────────────


def _prepare_df(df: pd.DataFrame, feat_list: list[str]) -> pd.DataFrame:
    """Encode side and fill missing features."""
    df = df.copy()
    if "side_enc" in feat_list and "side_enc" not in df.columns:
        df["side_enc"] = df["side"].map({"buy": 1.0, "sell": -1.0}).fillna(0.0)
    for f in feat_list:
        if f not in df.columns:
            df[f] = 0.0
    return df


def evaluate(
    model,
    config: dict,
    data_path: Path,
    t2_override: float | None = None,
    regressor=None,
) -> pd.DataFrame:
    """Evaluate gate on labeled dataset: baseline (all FULL) vs gated (HALF/FULL)."""
    df = pd.read_parquet(data_path)
    df = df.sort_values("ts_entry").reset_index(drop=True)

    feat_list = config["feature_list"]
    t2 = t2_override if t2_override is not None else config["thresholds"]["t2_full"]

    df = _prepare_df(df, feat_list)

    X = df[feat_list].values.astype(np.float64)
    probs = model.predict_proba(X)[:, 1]

    df["p_win"] = probs
    df["action"] = [decide(p, t2) for p in probs]
    df["gate_scale"] = df["action"].map(risk_scale)
    df["gated_return"] = df["net_return"] * df["gate_scale"]

    # ── Baseline stats (all FULL) ──
    base_ret = df["net_return"].values
    base_sum = base_ret.sum()
    base_cum = np.cumsum(base_ret)
    base_dd = (base_cum - np.maximum.accumulate(base_cum)).min()

    # ── Gated stats ──
    gated_ret = df["gated_return"].values
    gated_sum = gated_ret.sum()
    gated_cum = np.cumsum(gated_ret)
    gated_dd = (gated_cum - np.maximum.accumulate(gated_cum)).min()

    n_full = (df["action"] == "FULL").sum()
    n_half = (df["action"] == "HALF").sum()

    logger.info(f"\nThreshold: t2={t2}")
    logger.info(f"{'':>20} {'Baseline':>12} {'Gated':>12}")
    logger.info(f"{'trades':>20} {len(df):>12} {len(df):>12}")
    logger.info(f"{'FULL / HALF':>20} {len(df):>12} {f'{n_full}F / {n_half}H':>12}")
    logger.info(f"{'win_rate':>20} {df['win'].mean():>12.3f} {df['win'].mean():>12.3f}")
    logger.info(f"{'sum(return)':>20} {base_sum:>12.5f} {gated_sum:>12.5f}")
    logger.info(f"{'mean(return)':>20} {base_ret.mean():>12.5f} {gated_ret.mean():>12.5f}")
    logger.info(f"{'max_drawdown':>20} {base_dd:>12.5f} {gated_dd:>12.5f}")

    # Action breakdown
    logger.info(f"\nAction breakdown:")
    for action in ["FULL", "HALF"]:
        sub = df[df["action"] == action]
        if len(sub) == 0:
            continue
        wr = sub["win"].mean()
        avg_r = sub["net_return"].mean()
        logger.info(f"  {action}: n={len(sub)}, WR={wr:.3f}, avg_ret={avg_r:+.5f}")

    # t2 sweep
    logger.info(f"\nt2 sweep:")
    for t in np.arange(0.30, 0.71, 0.05):
        actions = np.where(probs >= t, "FULL", "HALF")
        scales = np.where(probs >= t, 1.0, 0.5)
        g_ret = base_ret * scales
        nf = (actions == "FULL").sum()
        pct_full = nf / len(df)
        logger.info(
            f"  t2={t:.2f}: FULL={nf:>3} ({pct_full:.0%}), "
            f"sum_ret={g_ret.sum():+.5f}, mean_ret={g_ret.mean():+.5f}"
        )

    # ── TOPK evaluation ──
    topk_pct = config["thresholds"].get("topk_pct")
    if topk_pct is not None:
        logger.info(f"\n── TOPK mode (K={topk_pct}%) ──")
        n_full_topk = max(1, int(len(df) * topk_pct / 100))
        topk_cutoff = np.sort(probs)[::-1][min(n_full_topk, len(df)) - 1]
        topk_full = probs >= topk_cutoff

        topk_scales = np.where(topk_full, 1.0, 0.5)
        topk_ret = base_ret * topk_scales
        topk_sum = topk_ret.sum()
        topk_cum = np.cumsum(topk_ret)
        topk_dd = (topk_cum - np.maximum.accumulate(topk_cum)).min()

        nf_topk = topk_full.sum()
        nh_topk = (~topk_full).sum()

        logger.info(f"{'':>20} {'Baseline':>12} {'TOPK':>12}")
        logger.info(f"{'trades':>20} {len(df):>12} {len(df):>12}")
        logger.info(f"{'FULL / HALF':>20} {len(df):>12} {f'{nf_topk}F / {nh_topk}H':>12}")
        logger.info(f"{'sum(return)':>20} {base_sum:>12.5f} {topk_sum:>12.5f}")
        logger.info(f"{'mean(return)':>20} {base_ret.mean():>12.5f} {topk_ret.mean():>12.5f}")
        logger.info(f"{'max_drawdown':>20} {base_dd:>12.5f} {topk_dd:>12.5f}")

        # TOPK action breakdown
        for name, mask in [("FULL", topk_full), ("HALF", ~topk_full)]:
            if not mask.any():
                continue
            wr = df.loc[mask, "win"].mean()
            avg_r = df.loc[mask, "net_return"].mean()
            logger.info(f"  {name}: n={mask.sum()}, WR={wr:.3f}, avg_ret={avg_r:+.5f}")

        # K sweep
        logger.info(f"\nTOPK sweep:")
        for k in [10, 20, 30, 40, 50]:
            nf_k = max(1, int(len(df) * k / 100))
            cutoff_k = np.sort(probs)[::-1][min(nf_k, len(df)) - 1]
            fm = probs >= cutoff_k
            sc = np.where(fm, 1.0, 0.5)
            gr = base_ret * sc
            logger.info(
                f"  K={k:>2}%: FULL={fm.sum():>3}, "
                f"sum_ret={gr.sum():+.5f}, mean_ret={gr.mean():+.5f}"
            )

    # ── Regression gate evaluation ──
    if regressor is not None:
        pred_ret = regressor.predict(X)
        reg_full = pred_ret >= 0
        reg_half = ~reg_full

        reg_scales = np.where(reg_full, 1.0, 0.5)
        reg_ret = base_ret * reg_scales
        reg_sum = reg_ret.sum()
        reg_cum = np.cumsum(reg_ret)
        reg_dd = (reg_cum - np.maximum.accumulate(reg_cum)).min()

        nf_reg = reg_full.sum()
        nh_reg = reg_half.sum()

        logger.info(f"\n── Regression gate (pred_ret >= 0 → FULL) ──")
        logger.info(f"{'':>20} {'Baseline':>12} {'Regression':>12}")
        logger.info(f"{'trades':>20} {len(df):>12} {len(df):>12}")
        logger.info(f"{'FULL / HALF':>20} {len(df):>12} {f'{nf_reg}F / {nh_reg}H':>12}")
        logger.info(f"{'sum(return)':>20} {base_sum:>12.5f} {reg_sum:>12.5f}")
        logger.info(f"{'mean(return)':>20} {base_ret.mean():>12.5f} {reg_ret.mean():>12.5f}")
        logger.info(f"{'max_drawdown':>20} {base_dd:>12.5f} {reg_dd:>12.5f}")

        for name, mask in [("FULL", reg_full), ("HALF", reg_half)]:
            if not mask.any():
                continue
            wr = df.loc[mask, "win"].mean()
            avg_r = df.loc[mask, "net_return"].mean()
            logger.info(f"  {name}: n={mask.sum()}, WR={wr:.3f}, avg_ret={avg_r:+.5f}")

        logger.info(f"  pred_ret dist: min={pred_ret.min():.5f}, median={np.median(pred_ret):.5f}, max={pred_ret.max():.5f}")

    return df


# ── CLI ──────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="AI gate inference / evaluation")
    sub = parser.add_subparsers(dest="cmd")

    # predict
    p_pred = sub.add_parser("predict", help="Single-row prediction")
    p_pred.add_argument("--model", type=str, default="model")
    p_pred.add_argument("--features", type=str, required=True, help="JSON dict of features")

    # evaluate
    p_eval = sub.add_parser("evaluate", help="Backtest evaluation on labeled dataset")
    p_eval.add_argument("--model", type=str, default="model")
    p_eval.add_argument("--data", type=str, default="data/datasets/mytrades_labeled.parquet")
    p_eval.add_argument("--t2", type=float, default=None, help="Override t2 threshold")

    args = parser.parse_args()

    if args.cmd == "predict":
        model, config, regressor = load_gate(Path(args.model))
        features = json.loads(args.features)
        result = predict_single(model, config, features, regressor=regressor)
        print(json.dumps(result, indent=2))

    elif args.cmd == "evaluate":
        model, config, regressor = load_gate(Path(args.model))
        evaluate(model, config, Path(args.data), t2_override=args.t2, regressor=regressor)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
