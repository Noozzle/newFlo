"""Telegram notification service."""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any

from loguru import logger

from app.config import TelegramConfig
from app.trading.signals import OpenPosition, Trade

try:
    from telegram import Bot
    from telegram.error import TelegramError
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    Bot = None
    TelegramError = Exception


class TelegramNotifier:
    """
    Telegram notification service.

    Sends notifications for:
    - Entry signals
    - Exit signals (SL/TP/Signal)
    - Errors and reconnects
    - Daily summaries
    """

    def __init__(self, config: TelegramConfig) -> None:
        """
        Initialize Telegram notifier.

        Args:
            config: Telegram configuration
        """
        self._config = config
        self._enabled = config.enabled and TELEGRAM_AVAILABLE
        self._bot: Any = None

        if not TELEGRAM_AVAILABLE and config.enabled:
            logger.warning("Telegram notifications enabled but python-telegram-bot not installed")

    async def start(self) -> None:
        """Start the notifier."""
        if not self._enabled:
            logger.info("Telegram notifications disabled in config")
            return

        if not TELEGRAM_AVAILABLE:
            logger.error("Telegram library not installed: pip install python-telegram-bot")
            self._enabled = False
            return

        if not self._config.token or not self._config.chat_id:
            logger.warning("Telegram token or chat_id not configured")
            self._enabled = False
            return

        try:
            self._bot = Bot(token=self._config.token)
            # Test connection
            me = await self._bot.get_me()
            logger.info(f"Telegram bot connected: @{me.username}")
        except Exception as e:
            logger.error(f"Failed to connect Telegram bot: {e}")
            self._enabled = False

    async def stop(self) -> None:
        """Stop the notifier."""
        pass

    async def _send(self, message: str, parse_mode: str = "HTML") -> bool:
        """
        Send a message.

        Args:
            message: Message text
            parse_mode: Parse mode (HTML/Markdown)

        Returns:
            True if sent successfully
        """
        if not self._enabled:
            logger.warning("Telegram send skipped: not enabled")
            return False

        if not self._bot:
            logger.warning("Telegram send skipped: bot not initialized")
            return False

        try:
            await self._bot.send_message(
                chat_id=self._config.chat_id,
                text=message,
                parse_mode=parse_mode,
            )
            logger.debug("Telegram message sent successfully")
            return True
        except TelegramError as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error sending Telegram message: {e}")
            return False

    async def notify_entry(
        self,
        position: OpenPosition,
        extra_info: dict[str, Any] | None = None,
    ) -> None:
        """
        Notify about trade entry.

        Args:
            position: Opened position
            extra_info: Additional info to include
        """
        logger.debug(f"notify_entry called for {position.symbol}")
        if not self._config.notify_on_entry:
            logger.debug("Entry notifications disabled in config")
            return

        emoji = "🟢" if position.side.value == "buy" else "🔴"
        direction = "LONG" if position.side.value == "buy" else "SHORT"
        notional = position.entry_price * position.size

        message = f"""
{emoji} <b>Entry: {position.symbol}</b>

<b>Direction:</b> {direction}
<b>Entry Price:</b> ${position.entry_price:.4f}
<b>Size:</b> {position.size}
<b>Notional:</b> ${notional:.2f}
<b>Stop Loss:</b> ${position.sl_price:.4f}
<b>Take Profit:</b> ${position.tp_price:.4f}
<b>Risk:</b> ${position.initial_risk:.2f}
<b>Time:</b> {position.entry_time.strftime('%Y-%m-%d %H:%M:%S')} UTC
"""

        if extra_info:
            message += "\n<b>Info:</b>\n"
            for k, v in extra_info.items():
                message += f"  • {k}: {v}\n"

        await self._send(message.strip())

    async def notify_exit(
        self,
        trade: Trade,
        extra_info: dict[str, Any] | None = None,
    ) -> None:
        """
        Notify about trade exit.

        Args:
            trade: Completed trade
            extra_info: Additional info to include
        """
        if not self._config.notify_on_exit:
            return

        # Determine emoji based on result and exit reason
        if trade.exit_reason == "sl":
            emoji = "🛑"
            reason_text = "Stop Loss"
        elif trade.exit_reason == "tp":
            emoji = "🎯"
            reason_text = "Take Profit"
        elif trade.is_winner:
            emoji = "✅"
            reason_text = "Signal Exit"
        else:
            emoji = "❌"
            reason_text = "Signal Exit"

        pnl_emoji = "📈" if trade.is_winner else "📉"

        message = f"""
{emoji} <b>Exit: {trade.symbol}</b>

<b>Reason:</b> {reason_text}
<b>Entry:</b> {trade.entry_price}
<b>Exit:</b> {trade.exit_price}
<b>Size:</b> {trade.size}

{pnl_emoji} <b>P&L:</b> ${trade.net_pnl:.2f}
<b>Gross:</b> ${trade.gross_pnl:.2f}
<b>Fees:</b> ${trade.fees:.2f}
<b>Hold Time:</b> {trade.hold_time_seconds / 60:.1f} min
"""

        await self._send(message.strip())

    async def notify_sl_tp_set(
        self,
        symbol: str,
        sl_price: Decimal | None,
        tp_price: Decimal | None,
    ) -> None:
        """Notify that SL/TP has been set."""
        if not self._config.notify_on_sl_tp:
            return

        message = f"""
⚙️ <b>SL/TP Set: {symbol}</b>

<b>Stop Loss:</b> {sl_price or 'None'}
<b>Take Profit:</b> {tp_price or 'None'}
"""

        await self._send(message.strip())

    async def notify_error(self, error: str, context: str = "") -> None:
        """
        Notify about an error.

        Args:
            error: Error message
            context: Context where error occurred
        """
        if not self._config.notify_on_error:
            return

        message = f"""
⚠️ <b>Error</b>

{f'<b>Context:</b> {context}' if context else ''}
<b>Error:</b> {error}
<b>Time:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC
"""

        await self._send(message.strip())

    async def notify_reconnect(self, service: str, attempt: int) -> None:
        """
        Notify about reconnection attempt.

        Args:
            service: Service being reconnected
            attempt: Attempt number
        """
        message = f"""
🔄 <b>Reconnecting</b>

<b>Service:</b> {service}
<b>Attempt:</b> {attempt}
<b>Time:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC
"""

        await self._send(message.strip())

    async def notify_daily_summary(
        self,
        date: datetime,
        trades_count: int,
        winners: int,
        losers: int,
        net_pnl: Decimal,
        equity: Decimal,
    ) -> None:
        """
        Send daily trading summary.

        Args:
            date: Date of summary
            trades_count: Total trades
            winners: Winning trades
            losers: Losing trades
            net_pnl: Net P&L for the day
            equity: Current equity
        """
        if not self._config.notify_daily_summary:
            return

        pnl_emoji = "📈" if net_pnl >= 0 else "📉"
        win_rate = winners / trades_count * 100 if trades_count > 0 else 0

        message = f"""
📊 <b>Daily Summary - {date.strftime('%Y-%m-%d')}</b>

<b>Trades:</b> {trades_count}
<b>Winners:</b> {winners}
<b>Losers:</b> {losers}
<b>Win Rate:</b> {win_rate:.1f}%

{pnl_emoji} <b>Day P&L:</b> ${net_pnl:.2f}
<b>Equity:</b> ${equity:.2f}
"""

        await self._send(message.strip())

    async def send_custom(self, message: str) -> None:
        """Send a custom message."""
        await self._send(message)
