"""AI gate for pre-trade filtering.

API contract
============

Input:  EntrySignal with enriched metadata (features from strategy)
Output: GateDecision — action + diagnostics (p_win, expected_R, reason)

Fallback: configurable via `fallback_action` (FULL or HALF).
Applied when model is missing, features incomplete, or inference fails.

Feature ordering is driven by model/config.json `feature_list` to ensure
training ↔ runtime alignment.
"""

from __future__ import annotations

import csv
import json
import math
import pickle
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from app.trading.signals import EntrySignal


# ── Contract types ──────────────────────────────────────────────


class GateAction(str, Enum):
    """Gate decision."""
    SKIP = "skip"    # risk × 0.0
    HALF = "half"    # risk × 0.5
    FULL = "full"    # risk × 1.0

    @property
    def risk_scale(self) -> float:
        return {"skip": 0.0, "half": 0.5, "full": 1.0}[self.value]


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Gate output — decision + diagnostics."""
    action: GateAction
    p_win: float | None = None          # predicted P(win), None if no model
    expected_r: float | None = None     # p_win * RR - (1-p_win), None if no model
    model_version: str | None = None    # model file name or version tag
    reason: str = ""                    # human-readable explanation

    @property
    def risk_scale(self) -> float:
        return self.action.risk_scale


# ── Feature extraction ──────────────────────────────────────────


def extract_feature_vector(
    signal: "EntrySignal",
    feature_list: list[str],
) -> list[float]:
    """Build feature vector from signal metadata in config.json order.

    Strategy enriches metadata in _emit_entry(). This function reads
    those values into a list matching the model's expected feature order.
    """
    m = signal.metadata
    vec = []
    for name in feature_list:
        if name == "side_enc":
            vec.append(1.0 if signal.side.value == "buy" else -1.0)
        else:
            vec.append(float(m.get(name, 0.0)))
    return vec


def _is_valid_vector(vec: list[float]) -> bool:
    """Check no NaN/Inf values in feature vector."""
    return all(math.isfinite(v) for v in vec)


# ── Gate implementation ─────────────────────────────────────────


class AIGate:
    """Pre-trade AI filter.

    Predicts win probability and decides: FULL / HALF.
    Uses TOPK mode: p_win >= topk_cutoff → FULL, else HALF.
    No model loaded → fallback action (configurable, default FULL).
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        full_threshold: float = 0.5,
        half_threshold: float = 0.3,
        fallback_action: str = "full",
        log_path: str | Path | None = None,
        enabled: bool = True,
    ) -> None:
        self._model = None
        self._model_version: str | None = None
        self._enabled = enabled
        self._full_threshold = full_threshold
        self._half_threshold = half_threshold
        self._fallback = GateAction(fallback_action)
        self._log_path = Path(log_path) if log_path else None
        self._log_initialized = False

        # Config-driven features (loaded from model/config.json)
        self._feature_list: list[str] | None = None
        self._gate_mode: str = "threshold"  # "threshold" or "TOPK"
        self._topk_cutoff: float = 0.5

        if model_path:
            p = Path(model_path)
            if p.exists():
                self._load_model(p)
                # Load config.json from same directory
                config_path = p.parent / "config.json"
                if config_path.exists():
                    self._load_config(config_path)
            else:
                logger.info(f"AI gate: no model at {p}, fallback={self._fallback.value}")

    def _load_model(self, path: Path) -> None:
        """Load trained model (pickle or joblib)."""
        try:
            if path.suffix == ".pkl":
                with open(path, "rb") as f:
                    self._model = pickle.load(f)
            else:
                import joblib
                self._model = joblib.load(path)
            self._model_version = path.name
            logger.info(f"AI gate model loaded: {path}")
        except Exception as e:
            logger.warning(f"AI gate model load failed: {e}")
            self._model = None

    def _load_config(self, path: Path) -> None:
        """Load model config.json for feature ordering and gate mode."""
        try:
            with open(path) as f:
                cfg = json.load(f)
            self._feature_list = cfg.get("feature_list")
            self._gate_mode = cfg.get("gate_mode", "threshold")
            thresholds = cfg.get("thresholds", {})
            if "topk_cutoff" in thresholds:
                self._topk_cutoff = float(thresholds["topk_cutoff"])
            logger.info(
                f"AI gate config loaded: {len(self._feature_list or [])} features, "
                f"mode={self._gate_mode}, topk_cutoff={self._topk_cutoff:.4f}"
            )
        except Exception as e:
            logger.warning(f"AI gate config load failed: {e}")

    def _predict(self, feature_vec: list[float]) -> tuple[float, float | None]:
        """Run model inference. Returns (p_win, expected_r)."""
        import numpy as np
        X = [feature_vec]
        p_win = float(self._model.predict_proba(X)[0, 1])
        # expected_R = p_win * RR - (1-p_win); with RR=3.0 by convention
        expected_r = p_win * 3.0 - (1.0 - p_win)
        return p_win, expected_r

    def _make_fallback(self, reason: str, signal: "EntrySignal | None" = None) -> GateDecision:
        """Build a fallback decision."""
        decision = GateDecision(
            action=self._fallback,
            reason=f"fallback: {reason}",
        )
        if signal:
            self._log(signal, decision)
        return decision

    def _decide_action(self, p_win: float) -> GateAction:
        """Decide action based on gate mode."""
        if self._gate_mode == "TOPK":
            # TOPK: above cutoff → FULL, below → HALF (no SKIP)
            return GateAction.FULL if p_win >= self._topk_cutoff else GateAction.HALF
        else:
            # Legacy threshold mode
            if p_win >= self._full_threshold:
                return GateAction.FULL
            if p_win >= self._half_threshold:
                return GateAction.HALF
            return GateAction.SKIP

    # ── Logging ──

    def _log(
        self,
        signal: "EntrySignal",
        decision: GateDecision,
    ) -> None:
        """Append to CSV for training data collection."""
        if not self._log_path:
            return

        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self._log_initialized and not self._log_path.exists()

        row: dict[str, str] = {}
        row["timestamp"] = signal.timestamp.isoformat()
        row["symbol"] = signal.symbol
        row["side"] = signal.side.value
        row["entry_price"] = str(signal.entry_price)
        row["sl_price"] = str(signal.sl_price)
        row["tp_price"] = str(signal.tp_price)

        # Log all features from metadata
        if self._feature_list:
            for name in self._feature_list:
                if name == "side_enc":
                    row[name] = "1.0" if signal.side.value == "buy" else "-1.0"
                else:
                    row[name] = f"{float(signal.metadata.get(name, 0)):.8f}"

        row["action"] = decision.action.value
        row["p_win"] = f"{decision.p_win:.4f}" if decision.p_win is not None else ""
        row["expected_r"] = f"{decision.expected_r:.4f}" if decision.expected_r is not None else ""
        row["reason"] = decision.reason

        with open(self._log_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if write_header:
                writer.writeheader()
            writer.writerow(row)

        self._log_initialized = True

    # ── Public API ──

    async def decide(self, signal: "EntrySignal") -> GateDecision:
        """Decide: FULL / HALF / SKIP.

        Args:
            signal: EntrySignal with enriched metadata (features).

        Returns:
            GateDecision with action, p_win, expected_r, model_version, reason.
        """
        if not self._enabled:
            return GateDecision(action=GateAction.FULL, reason="gate disabled")

        # No model → fallback (still logs for training data)
        if self._model is None:
            return self._make_fallback("no model", signal)

        # Build feature vector from config.json feature_list
        if not self._feature_list:
            return self._make_fallback("no feature_list in config", signal)

        feature_vec = extract_feature_vector(signal, self._feature_list)

        # Incomplete features → fallback
        if not _is_valid_vector(feature_vec):
            return self._make_fallback("incomplete features", signal)

        # Inference
        try:
            p_win, expected_r = self._predict(feature_vec)
        except Exception as e:
            logger.warning(f"AI gate inference error: {e}")
            return self._make_fallback(f"inference error: {e}", signal)

        action = self._decide_action(p_win)
        decision = GateDecision(
            action=action,
            p_win=p_win,
            expected_r=expected_r,
            model_version=self._model_version,
            reason=f"p_win={p_win:.3f}",
        )

        logger.info(
            f"AI gate: {signal.symbol} {signal.side.value} "
            f"p(win)={p_win:.3f} E[R]={expected_r:+.2f} → {action.value}"
        )
        self._log(signal, decision)
        return decision
