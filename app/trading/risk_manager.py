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
        self._daily_loss = Decimal("0")
        self._current_day: date | None = None

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

        # Check daily loss limit (only check when we have actual losses, not profits)
        if self._daily_loss < 0:
            balance = self._portfolio.balance
            if balance > 0:
                daily_loss_pct = abs(self._daily_loss) / balance * 100
                if daily_loss_pct >= self._config.max_daily_loss_pct:
                    return SizeResult(
                        approved=False,
                        size=Decimal("0"),
                        risk_amount=Decimal("0"),
                        reason=f"Daily loss limit ({self._config.max_daily_loss_pct}%) reached",
                    )

        # Check drawdown limit
        if self._portfolio.drawdown >= self._config.max_drawdown_pct:
            return SizeResult(
                approved=False,
                size=Decimal("0"),
                risk_amount=Decimal("0"),
                reason=f"Max drawdown ({self._config.max_drawdown_pct}%) reached",
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
        # Total round-trip cost = 2 * (fees + slippage)
        round_trip_cost_pct = 2 * self._costs.total_cost_pct

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

        # Calculate position size based on risk percentage
        equity = self._portfolio.equity
        risk_amount = equity * (self._config.max_position_pct / 100)

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

        # Check daily loss (only when we have actual losses)
        if self._daily_loss < 0:
            balance = self._portfolio.balance
            if balance <= 0:
                logger.warning(f"Cannot check daily loss: balance={balance}")
                return False, "Invalid balance"
            daily_loss_pct = abs(self._daily_loss) / balance * 100
            logger.debug(
                f"Daily loss check: loss={self._daily_loss}, balance={balance}, "
                f"loss_pct={daily_loss_pct:.2f}%, limit={self._config.max_daily_loss_pct}%, "
                f"current_day={self._current_day}"
            )
            if daily_loss_pct >= self._config.max_daily_loss_pct:
                logger.warning(
                    f"Daily loss limit reached: {daily_loss_pct:.2f}% >= {self._config.max_daily_loss_pct}% "
                    f"(loss={self._daily_loss}, balance={balance}, day={self._current_day})"
                )
                return False, "Daily loss limit reached"

        # Check drawdown
        if self._portfolio.drawdown >= self._config.max_drawdown_pct:
            return False, "Max drawdown reached"

        return True, "OK"

    def update_daily_loss(self, pnl: Decimal) -> None:
        """Update daily loss tracking."""
        old_loss = self._daily_loss
        self._daily_loss += pnl
        logger.info(
            f"Daily PnL updated: {pnl:+.4f} (total: {old_loss:.4f} -> {self._daily_loss:.4f}, "
            f"day={self._current_day})"
        )

    def reset_daily(self) -> None:
        """Reset daily tracking (call at start of new day)."""
        self._daily_loss = Decimal("0")

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
                f"Initializing trading day: {current_date}, daily_loss={self._daily_loss}"
            )
            self._current_day = current_date
        elif current_date > self._current_day:
            logger.info(
                f"New trading day: {current_date} (was: {self._current_day}), "
                f"resetting daily loss (was: {self._daily_loss})"
            )
            self._daily_loss = Decimal("0")
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

        if self._daily_loss < 0:
            balance = self._portfolio.balance
            if balance > 0:
                daily_loss_pct = abs(self._daily_loss) / balance * 100
                if daily_loss_pct >= self._config.max_daily_loss_pct:
                    return False

        if self._portfolio.drawdown >= self._config.max_drawdown_pct:
            return False

        return True
