"""Tests for rules-based AI gate layer (runs BEFORE ML predict)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.config import GateRulesConfig
from app.core.events import Side
from app.trading.ai_gate import AIGate, GateAction
from app.trading.signals import EntrySignal


# ── Helpers ──────────────────────────────────────────────────────


def _make_gate(**overrides) -> AIGate:
    """Construct AIGate with rules enabled and optional overrides."""
    cfg = GateRulesConfig(enabled=True, **overrides)
    # Keep ai_gate.enabled=False to prove rules are independent of ML.
    return AIGate(enabled=False, rules_config=cfg)


def _make_signal(
    symbol: str = "SOLUSDT",
    hour_utc: int = 10,
    metadata: dict | None = None,
) -> EntrySignal:
    """Build a minimal EntrySignal for testing."""
    ts = datetime(2026, 4, 17, hour_utc, 30, 0, tzinfo=timezone.utc)
    meta = dict(metadata or {})
    meta.setdefault("hour_utc", float(hour_utc))
    return EntrySignal(
        timestamp=ts,
        symbol=symbol,
        side=Side.BUY,
        entry_price=Decimal("100"),
        sl_price=Decimal("99"),
        tp_price=Decimal("103"),
        metadata=meta,
    )


# ── Tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kill_zone_range_compression_skips():
    """range_compression in kill-zone [0.0018, 0.0022] → SKIP."""
    gate = _make_gate()
    signal = _make_signal(metadata={"range_compression_24h": 0.0020})
    decision = await gate.decide(signal)
    assert decision.action == GateAction.SKIP
    assert "kill_zone_range_compression" in decision.reason


@pytest.mark.asyncio
async def test_kill_zone_boundary_inclusive():
    """Boundaries of kill-zone are inclusive."""
    gate = _make_gate()
    for rc in (0.0018, 0.0022):
        signal = _make_signal(metadata={"range_compression_24h": rc})
        decision = await gate.decide(signal)
        assert decision.action == GateAction.SKIP, f"rc={rc} should SKIP"


@pytest.mark.asyncio
async def test_kill_zone_outside_passes():
    """range_compression outside kill-zone → no SKIP from rule 1."""
    gate = _make_gate()
    signal = _make_signal(
        symbol="SUIUSDT",  # whitelisted to avoid rule 2
        metadata={"range_compression_24h": 0.0015},
    )
    decision = await gate.decide(signal)
    # Rules don't match → fallback path (gate disabled → FULL)
    assert decision.action == GateAction.FULL


@pytest.mark.asyncio
async def test_non_whitelist_weak_trend_skips():
    """Non-whitelisted symbol + trend_strength < min → SKIP."""
    gate = _make_gate()
    signal = _make_signal(
        symbol="SOLUSDT",  # not in whitelist
        metadata={"trend_strength_norm": 0.4},
    )
    decision = await gate.decide(signal)
    assert decision.action == GateAction.SKIP
    assert "non_whitelist_weak_trend" in decision.reason


@pytest.mark.asyncio
async def test_whitelist_weak_trend_not_blocked():
    """Whitelisted symbol + weak trend → rule 2 does NOT fire."""
    gate = _make_gate()
    signal = _make_signal(
        symbol="SUIUSDT",  # in whitelist
        metadata={"trend_strength_norm": 0.4},
    )
    decision = await gate.decide(signal)
    # Rules don't match (whitelist skipped) → gate disabled → FULL
    assert decision.action == GateAction.FULL


@pytest.mark.asyncio
async def test_cost_drag_skips():
    """cost_ratio > threshold → SKIP."""
    gate = _make_gate()
    signal = _make_signal(
        symbol="SUIUSDT",
        metadata={"cost_ratio": 0.30},
    )
    decision = await gate.decide(signal)
    assert decision.action == GateAction.SKIP
    assert "cost_drag" in decision.reason


@pytest.mark.asyncio
async def test_bad_hour_halves():
    """hour_utc in half_hours_utc → HALF."""
    gate = _make_gate()
    signal = _make_signal(symbol="SUIUSDT", hour_utc=15)
    decision = await gate.decide(signal)
    assert decision.action == GateAction.HALF
    assert "bad_hour_utc" in decision.reason


@pytest.mark.asyncio
async def test_hivol_weak_trend_halves():
    """atr_rank >= 0.6 AND trend_strength < 0.3 → HALF."""
    gate = _make_gate()
    signal = _make_signal(
        symbol="SUIUSDT",
        hour_utc=10,  # not a bad hour
        metadata={"atr_rank_7d": 0.7, "trend_strength_norm": 0.2},
    )
    decision = await gate.decide(signal)
    assert decision.action == GateAction.HALF
    assert "hivol_weak_trend" in decision.reason


@pytest.mark.asyncio
async def test_none_features_do_not_trigger_rules():
    """All rules that depend on None/missing features are silently skipped."""
    gate = _make_gate()
    signal = _make_signal(
        symbol="SUIUSDT",
        hour_utc=10,
        metadata={
            "range_compression_24h": None,
            "trend_strength_norm": None,
            "cost_ratio": None,
            "atr_rank_7d": None,
        },
    )
    decision = await gate.decide(signal)
    # No rule fires → fallback to gate disabled → FULL
    assert decision.action == GateAction.FULL


@pytest.mark.asyncio
async def test_kill_zone_priority_over_cost_drag():
    """kill_zone (rule 1) must take precedence over cost_drag (rule 3)."""
    gate = _make_gate()
    signal = _make_signal(
        metadata={
            "range_compression_24h": 0.0020,  # kill-zone
            "cost_ratio": 0.50,  # would also trip cost_drag
        },
    )
    decision = await gate.decide(signal)
    assert decision.action == GateAction.SKIP
    assert decision.reason == "kill_zone_range_compression"


@pytest.mark.asyncio
async def test_rules_disabled_short_circuits():
    """When rules.enabled=False, rules layer is bypassed entirely."""
    cfg = GateRulesConfig(enabled=False)
    gate = AIGate(enabled=False, rules_config=cfg)
    signal = _make_signal(metadata={"range_compression_24h": 0.0020})
    decision = await gate.decide(signal)
    # No rule fires → gate disabled fallback → FULL
    assert decision.action == GateAction.FULL


@pytest.mark.asyncio
async def test_nan_feature_treated_as_missing():
    """NaN/Inf feature values are treated as None (safe default)."""
    import math

    gate = _make_gate()
    signal = _make_signal(
        symbol="SUIUSDT",
        hour_utc=10,
        metadata={"range_compression_24h": math.nan, "cost_ratio": math.inf},
    )
    decision = await gate.decide(signal)
    assert decision.action == GateAction.FULL
