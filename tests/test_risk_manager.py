"""Tests for RiskManager: per-symbol multipliers + fee-aware entry gate."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.config import CostsConfig, RiskConfig
from app.core.events import Side
from app.trading.portfolio import Portfolio
from app.trading.risk_manager import RiskManager
from app.trading.signals import EntrySignal


# ── Helpers ──────────────────────────────────────────────────────


def _make_risk_manager(
    per_symbol: dict[str, Decimal] | None = None,
    initial_balance: Decimal = Decimal("1000"),
    fee_entry_bps: Decimal = Decimal("2"),
    fee_exit_bps: Decimal = Decimal("5.5"),
    slippage_bps: Decimal = Decimal("2"),
) -> tuple[RiskManager, Portfolio]:
    """Build a RiskManager + Portfolio with deterministic config."""
    portfolio = Portfolio(initial_balance=initial_balance)
    risk_cfg = RiskConfig(
        max_position_pct=Decimal("2.0"),
        max_daily_sl_count=10,
        max_concurrent_trades=5,
        per_symbol=per_symbol or {},
    )
    costs_cfg = CostsConfig(
        fee_entry_bps=fee_entry_bps,
        fee_exit_bps=fee_exit_bps,
        slippage_bps=slippage_bps,
    )
    rm = RiskManager(config=risk_cfg, costs=costs_cfg, portfolio=portfolio)
    return rm, portfolio


def _make_signal(
    symbol: str = "SOLUSDT",
    entry: Decimal = Decimal("100"),
    sl: Decimal = Decimal("99"),
    tp: Decimal = Decimal("103"),
    side: Side = Side.BUY,
    metadata: dict | None = None,
) -> EntrySignal:
    ts = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    return EntrySignal(
        timestamp=ts,
        symbol=symbol,
        side=side,
        entry_price=entry,
        sl_price=sl,
        tp_price=tp,
        metadata=dict(metadata or {}),
    )


# ── Per-symbol multiplier tests ─────────────────────────────────


@pytest.mark.parametrize(
    ("symbol", "mult", "expected_ratio"),
    [
        ("SOLUSDT", Decimal("1.3"), Decimal("1.3")),
        ("DOGEUSDT", Decimal("0.5"), Decimal("0.5")),
        ("UNKNOWN", None, Decimal("1.0")),  # no entry → multiplier = 1.0
    ],
)
def test_per_symbol_multiplier(
    symbol: str, mult: Decimal | None, expected_ratio: Decimal
) -> None:
    """Per-symbol risk multiplier scales risk_amount relative to baseline."""
    per_symbol: dict[str, Decimal] = {}
    if mult is not None:
        per_symbol[symbol] = mult

    # Baseline (no multipliers) — large SL to avoid fee-aware gate rejections.
    rm_base, _ = _make_risk_manager(per_symbol={})
    sig_base = _make_signal(symbol=symbol, entry=Decimal("100"),
                            sl=Decimal("95"), tp=Decimal("115"))
    base = rm_base.calculate_position_size(sig_base)
    assert base.approved, f"baseline sizing rejected: {base.reason}"

    # With multiplier
    rm, _ = _make_risk_manager(per_symbol=per_symbol)
    sig = _make_signal(symbol=symbol, entry=Decimal("100"),
                       sl=Decimal("95"), tp=Decimal("115"))
    result = rm.calculate_position_size(sig)
    assert result.approved, f"sized rejected: {result.reason}"

    ratio = result.risk_amount / base.risk_amount
    # Allow tiny rounding tolerance (Decimal quantize to 1e-8)
    assert abs(ratio - expected_ratio) < Decimal("0.001"), (
        f"{symbol}: ratio={ratio}, expected={expected_ratio}"
    )


def test_per_symbol_composes_with_ai_gate_half() -> None:
    """Per-symbol 1.3x × AI gate HALF (0.5) → 0.65x baseline."""
    rm, _ = _make_risk_manager(per_symbol={"SOLUSDT": Decimal("1.3")})

    sig_full = _make_signal(symbol="SOLUSDT", entry=Decimal("100"),
                            sl=Decimal("95"), tp=Decimal("115"))
    r_full = rm.calculate_position_size(sig_full)
    assert r_full.approved

    sig_half = _make_signal(
        symbol="SOLUSDT",
        entry=Decimal("100"), sl=Decimal("95"), tp=Decimal("115"),
        metadata={"risk_scale": 0.5},
    )
    r_half = rm.calculate_position_size(sig_half)
    assert r_half.approved
    assert abs(r_half.risk_amount / r_full.risk_amount - Decimal("0.5")) < Decimal("0.001")


# ── Fee-aware entry gate tests ──────────────────────────────────


def test_fee_aware_gate_wide_atr_accepted() -> None:
    """Wide SL (>= ~50 bps) with default fees → expected_net_R >> 1.5 → approve."""
    rm, _ = _make_risk_manager()
    # SL = 100 bps (1%), RR=3 → net_R after ~11 bps costs ≈ 3 - 0.11 ≈ 2.89 → pass
    sig = _make_signal(entry=Decimal("100"), sl=Decimal("99"),
                       tp=Decimal("103"))
    result = rm.calculate_position_size(sig)
    assert result.approved, f"wide-ATR setup should pass: {result.reason}"


def test_fee_aware_gate_tight_atr_rejected() -> None:
    """Very tight SL (~5 bps) makes cost drag > 1.5R → reject."""
    rm, _ = _make_risk_manager()
    # SL = 5 bps: 100 -> 99.95. TP = 15 bps above (RR=3).
    sig = _make_signal(
        entry=Decimal("100"),
        sl=Decimal("99.95"),
        tp=Decimal("100.15"),
    )
    result = rm.calculate_position_size(sig)
    assert not result.approved
    # Accept either fee-aware reason or the cost_vs_risk reject (depending on
    # which guard is wired — both encode the same "costs kill the edge" idea).
    assert (
        "Fee-aware reject" in result.reason
        or "Costs too high" in result.reason
    )


def test_fee_aware_gate_moderate_atr_accepted() -> None:
    """Moderate SL (~30 bps): cost ~11 bps → expected_net_R ≈ 3 - 0.37 ≈ 2.63 → pass."""
    rm, _ = _make_risk_manager()
    sig = _make_signal(
        entry=Decimal("100"),
        sl=Decimal("99.7"),   # 30 bps
        tp=Decimal("100.9"),  # 90 bps above entry → RR=3
    )
    result = rm.calculate_position_size(sig)
    assert result.approved, f"moderate-ATR should pass: {result.reason}"
