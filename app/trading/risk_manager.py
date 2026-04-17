"""Risk management for position sizing and trade validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from loguru import logger

from app.config import CostsConfig, RiskConfig
from app.core.events import Side
from app.trading.signals import EntrySignal

if TYPE_CHECKING:
    from app.trading.portfolio import Portfolio


@dataclass
class SizeResult:
    """Position sizing result."""
    approved: bool
    size: Decimal
    risk_amount: Decimal
    reason: str = ""


class RiskManager:
    """
    Risk manager for position sizing and trade validation.

    Responsibilities:
    - Calculate position size based on risk percentage
    - Validate trades against risk limits
    - Fee-aware sizing guard (skip if costs > X% of SL distance)
    - Track daily loss and drawdown limits
    """

    def __init__(
        self,
        config: RiskConfig,
        costs: CostsConfig,
        portfolio: "Portfolio",
    ) -> None:
        """
        Initialize risk manager.

        Args:
            config: Risk configuration
            costs: Trading costs configuration
            portfolio: Portfolio for balance/position info
        """
        self._config = config
        self._costs = costs
        self._portfolio = portfolio
        self._daily_sl_count = 0
        self._current_day: date | None = None
        # Adaptive DD: timestamp when hard DD was hit (for cooldown)
        self._dd_hard_hit_time: datetime | None = None

    def calculate_position_size(
        self,
        signal: EntrySignal,
        current_price: Decimal | None = None,
    ) -> SizeResult:
        """
        Calculate position size based on risk parameters.

        Uses fixed fractional sizing: risk X% of equity per trade.

        Args:
            signal: Entry signal with SL/TP prices
            current_price: Current market price (uses signal.entry_price if None)

        Returns:
            SizeResult with approved flag and size
        """
        # Validate signal
        if not signal.is_valid():
            return SizeResult(
                approved=False,
                size=Decimal("0"),
                risk_amount=Decimal("0"),
                reason="Invalid signal: SL/TP prices don't make sense",
            )

        # Check max concurrent trades
        if self._portfolio.num_open_positions >= self._config.max_concurrent_trades:
            return SizeResult(
                approved=False,
                size=Decimal("0"),
                risk_amount=Decimal("0"),
                reason=f"Max concurrent trades ({self._config.max_concurrent_trades}) reached",
            )

        # Check if we already have a position in this symbol
        if self._portfolio.has_position(signal.symbol):
            return SizeResult(
                approved=False,
                size=Decimal("0"),
                risk_amount=Decimal("0"),
                reason=f"Already have position in {signal.symbol}",
            )

        # Check daily SL count limit
        if self._daily_sl_count >= self._config.max_daily_sl_count:
            return SizeResult(
                approved=False,
                size=Decimal("0"),
                risk_amount=Decimal("0"),
                reason=f"Daily SL limit ({self._config.max_daily_sl_count}) reached ({self._daily_sl_count} SLs today)",
            )

        # Adaptive drawdown: scale risk instead of binary stop
        dd_scale = self._dd_risk_scale(event_time=signal.timestamp)
        if dd_scale <= Decimal("0"):
            return SizeResult(
                approved=False,
                size=Decimal("0"),
                risk_amount=Decimal("0"),
                reason=f"Hard DD limit ({self._config.dd_hard_pct}%) reached, "
                       f"current DD={self._portfolio.drawdown:.1f}%",
            )

        # Calculate risk per unit
        entry_price = current_price or signal.entry_price
        risk_per_unit = signal.risk_distance

        if risk_per_unit <= 0:
            return SizeResult(
                approved=False,
                size=Decimal("0"),
                risk_amount=Decimal("0"),
                reason="Invalid risk distance (SL too close or wrong side)",
            )

        # Calculate position value and check costs
        # Total round-trip cost = entry fee + exit fee + 2 * slippage
        round_trip_cost_pct = self._costs.round_trip_cost_pct

        # Check if costs are acceptable relative to risk
        # Cost relative to risk = cost_pct * entry_price / risk_distance
        cost_vs_risk = (round_trip_cost_pct * entry_price) / risk_per_unit
        max_cost_vs_risk = Decimal("0.3")  # Max 30% of risk as costs

        if cost_vs_risk > max_cost_vs_risk:
            return SizeResult(
                approved=False,
                size=Decimal("0"),
                risk_amount=Decimal("0"),
                reason=f"Costs too high relative to SL distance ({cost_vs_risk:.1%} > {max_cost_vs_risk:.1%})",
            )

        # Calculate position size based on risk percentage, scaled by DD level
        equity = self._portfolio.equity
        risk_amount = equity * (self._config.max_position_pct / 100) * dd_scale
        if dd_scale < Decimal("1"):
            logger.info(f"DD risk scaling: {dd_scale} (DD={self._portfolio.drawdown:.1f}%)")

        # Apply AI-gate risk_scale (SKIP=0.0, HALF=0.5, FULL=1.0) from signal metadata.
        try:
            gate_scale = Decimal(str(signal.metadata.get("risk_scale", 1.0)))
        except (TypeError, ValueError, ArithmeticError):
            gate_scale = Decimal("1")
        if gate_scale <= Decimal("0"):
            return SizeResult(
                approved=False,
                size=Decimal("0"),
                risk_amount=Decimal("0"),
                reason="AI gate: risk_scale=0 (SKIP)",
            )
        if gate_scale < Decimal("1"):
            logger.info(f"AI gate risk scaling: {gate_scale}")
        risk_amount = risk_amount * gate_scale

        # Size = risk_amount / risk_per_unit
        size = risk_amount / risk_per_unit

        # Round to reasonable precision (8 decimal places)
        size = size.quantize(Decimal("0.00000001"))

        # Check notional value isn't too large
        notional = size * entry_price
        max_notional = equity * Decimal("10")  # Max 10x equity per position

        if notional > max_notional:
            size = max_notional / entry_price
            size = size.quantize(Decimal("0.00000001"))
            risk_amount = size * risk_per_unit

        logger.debug(
            f"Position sizing: equity={equity}, risk_pct={self._config.max_position_pct}%, "
            f"risk_amount={risk_amount}, risk_per_unit={risk_per_unit}, size={size}"
        )

        return SizeResult(
            approved=True,
            size=size,
            risk_amount=risk_amount,
            reason="OK",
        )

    def _dd_risk_scale(self, event_time: datetime | None = None) -> Decimal:
        """Return risk multiplier based on current drawdown level.

        0-soft: 1.0, soft-mid: 0.5, mid-hard: 0.25, >hard: 0.0 (stop).
        After hitting hard limit, a cooldown period must pass before resuming.
        """
        dd = self._portfolio.drawdown
        soft = self._config.dd_soft_pct
        hard = self._config.dd_hard_pct
        mid = (soft + hard) / 2  # midpoint between soft and hard

        if dd >= hard:
            # Record the moment hard DD was first hit
            if self._dd_hard_hit_time is None and event_time:
                self._dd_hard_hit_time = event_time
                logger.warning(f"Hard DD limit hit: {dd:.1f}% >= {hard}%")
            return Decimal("0")

        # If we previously hit hard DD, enforce cooldown
        if self._dd_hard_hit_time is not None and event_time:
            elapsed = (event_time - self._dd_hard_hit_time).total_seconds()
            cooldown = self._config.dd_cooldown_minutes * 60
            if elapsed < cooldown:
                return Decimal("0")
            # Cooldown passed and DD recovered below hard — reset
            self._dd_hard_hit_time = None
            logger.info(f"DD cooldown expired, resuming with reduced risk (DD={dd:.1f}%)")

        if dd >= mid:
            return Decimal("0.25")
        if dd >= soft:
            return Decimal("0.5")
        return Decimal("1")

    def validate_signal(self, signal: EntrySignal) -> tuple[bool, str]:
        """
        Validate a trading signal against risk rules.

        Args:
            signal: Entry signal to validate

        Returns:
            Tuple of (valid, reason)
        """
        # Basic validation
        if not signal.is_valid():
            return False, "Invalid SL/TP configuration"

        # Check R:R ratio (minimum 1:1)
        if signal.risk_reward_ratio < Decimal("1.0"):
            return False, f"R:R too low: {signal.risk_reward_ratio:.2f}"

        # Check concurrent trades
        if self._portfolio.num_open_positions >= self._config.max_concurrent_trades:
            return False, "Max concurrent trades reached"

        # Check existing position
        if self._portfolio.has_position(signal.symbol):
            return False, "Already have position"

        # Check daily SL count
        if self._daily_sl_count >= self._config.max_daily_sl_count:
            logger.warning(
                f"Daily SL limit reached: {self._daily_sl_count} >= {self._config.max_daily_sl_count} "
                f"(day={self._current_day})"
            )
            return False, f"Daily SL limit ({self._config.max_daily_sl_count}) reached"

        # Adaptive drawdown check
        if self._dd_risk_scale() <= Decimal("0"):
            return False, f"Hard DD limit ({self._config.dd_hard_pct}%) reached"

        return True, "OK"

    def record_sl(self) -> None:
        """Record a stop-loss exit for daily SL counting."""
        self._daily_sl_count += 1
        logger.info(
            f"Daily SL count: {self._daily_sl_count}/{self._config.max_daily_sl_count} "
            f"(day={self._current_day})"
        )

    def reset_daily(self) -> None:
        """Reset daily tracking (call at start of new day)."""
        self._daily_sl_count = 0

    def reconstruct_daily_sl_count(self, sl_count: int, current_date: date) -> None:
        """
        Reconstruct daily SL count from persisted trade data (called after process restart).

        Args:
            sl_count: Number of SL exits today
            current_date: Today's date
        """
        self._current_day = current_date
        self._daily_sl_count = sl_count
        logger.info(
            f"Reconstructed daily SL count for {current_date}: "
            f"{sl_count}/{self._config.max_daily_sl_count}"
        )

    def check_new_day(self, event_time: datetime | None = None, use_realtime: bool = False) -> None:
        """
        Check if we've moved to a new trading day and reset daily loss.

        Args:
            event_time: Timestamp of current event (used in backtest mode)
            use_realtime: If True, use current UTC time (for live mode)
        """
        from datetime import timezone

        # Use realtime for live mode, event_time for backtest
        if use_realtime or event_time is None:
            now_utc = datetime.now(timezone.utc)
            current_date = now_utc.date()
        else:
            # For backtest - use event time
            if event_time.tzinfo is None:
                current_date = event_time.date()
            else:
                current_date = event_time.astimezone(timezone.utc).date()

        if self._current_day is None:
            logger.info(
                f"Initializing trading day: {current_date}, daily_sl_count={self._daily_sl_count}"
            )
            self._current_day = current_date
        elif current_date > self._current_day:
            logger.info(
                f"New trading day: {current_date} (was: {self._current_day}), "
                f"resetting daily SL count (was: {self._daily_sl_count})"
            )
            self._daily_sl_count = 0
            self._current_day = current_date

    def should_close_for_time(
        self,
        symbol: str,
        current_time_seconds: float,
        max_hold_seconds: float,
    ) -> bool:
        """
        Check if position should be closed due to time limit.

        Args:
            symbol: Trading pair
            current_time_seconds: Current timestamp
            max_hold_seconds: Maximum hold time

        Returns:
            True if position should be closed
        """
        position = self._portfolio.get_position(symbol)
        if position is None:
            return False

        hold_time = current_time_seconds - position.entry_time.timestamp()
        return hold_time >= max_hold_seconds

    def adjust_sl_for_trailing(
        self,
        symbol: str,
        current_price: Decimal,
    ) -> Decimal | None:
        """
        Calculate new SL for trailing stop.

        Args:
            symbol: Trading pair
            current_price: Current market price

        Returns:
            New SL price if should be adjusted, None otherwise
        """
        if not self._config.use_trailing_stop:
            return None

        position = self._portfolio.get_position(symbol)
        if position is None:
            return None

        trail_distance = current_price * (self._config.trailing_stop_pct / 100)

        if position.side == Side.BUY:
            new_sl = current_price - trail_distance
            if new_sl > position.sl_price:
                return new_sl
        else:
            new_sl = current_price + trail_distance
            if new_sl < position.sl_price:
                return new_sl

        return None

    @property
    def can_trade(self) -> bool:
        """Check if trading is allowed based on risk limits."""
        if self._portfolio.num_open_positions >= self._config.max_concurrent_trades:
            return False

        if self._daily_sl_count >= self._config.max_daily_sl_count:
            return False

        if self._dd_risk_scale() <= Decimal("0"):
            return False

        return True
