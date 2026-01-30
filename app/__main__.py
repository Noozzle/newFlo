"""CLI entry point for FloTrader."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import click
from loguru import logger

from app import __version__
from app.config import Config, Mode


def setup_logging(level: str = "INFO") -> None:
    """Configure logging."""
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=level,
        colorize=True,
    )
    logger.add(
        "logs/flotrader_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="30 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    )


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """FloTrader - Bybit Algo-Trading Framework."""
    pass


@cli.command()
@click.option(
    "--config", "-c",
    type=click.Path(exists=True),
    default="config.yaml",
    help="Path to configuration file",
)
@click.option("--log-level", default="INFO", help="Logging level")
def live(config: str, log_level: str) -> None:
    """Start live trading."""
    setup_logging(log_level)
    logger.info(f"Starting FloTrader v{__version__} in LIVE mode")

    try:
        cfg = Config.from_yaml(config)
        cfg.mode = Mode.LIVE

        asyncio.run(_run_live(cfg))

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.exception(f"Error in live mode: {e}")
        sys.exit(1)


@cli.command()
@click.option(
    "--config", "-c",
    type=click.Path(exists=True),
    default="config.yaml",
    help="Path to configuration file",
)
@click.option("--symbol", "-s", help="Symbol to backtest (overrides config)")
@click.option("--from", "start_date", help="Start date (YYYY-MM-DD)")
@click.option("--to", "end_date", help="End date (YYYY-MM-DD)")
@click.option("--log-level", default="INFO", help="Logging level")
def backtest(
    config: str,
    symbol: str | None,
    start_date: str | None,
    end_date: str | None,
    log_level: str,
) -> None:
    """Run backtest."""
    setup_logging(log_level)
    logger.info(f"Starting FloTrader v{__version__} in BACKTEST mode")

    try:
        cfg = Config.from_yaml(config)
        cfg.mode = Mode.BACKTEST

        # Override with CLI args
        if symbol:
            cfg.backtest.symbols = [symbol]
        if start_date:
            cfg.backtest.start_date = start_date
        if end_date:
            cfg.backtest.end_date = end_date

        asyncio.run(_run_backtest(cfg))

    except KeyboardInterrupt:
        logger.info("Backtest interrupted")
    except Exception as e:
        logger.exception(f"Error in backtest: {e}")
        sys.exit(1)


@cli.command()
@click.option("--run", "-r", required=True, help="Path to report directory")
def report(run: str) -> None:
    """Regenerate report from existing backtest run."""
    setup_logging("INFO")

    report_dir = Path(run)
    if not report_dir.exists():
        logger.error(f"Report directory not found: {report_dir}")
        sys.exit(1)

    logger.info(f"Regenerating report from {report_dir}")
    # TODO: Implement report regeneration from trades.csv


async def _run_live(config: Config) -> None:
    """Run live trading mode."""
    from app.adapters.bybit_adapter import BybitAdapter
    from app.adapters.live_data_feed import LiveDataFeed
    from app.core.engine import Engine
    from app.core.events import KlineEvent, MarketTradeEvent, OrderBookEvent
    from app.notifications.telegram import TelegramNotifier
    from app.storage.data_recorder import DataRecorder
    from app.storage.trade_store import TradeStore
    from app.strategies import OrderflowStrategy

    # Create event bus
    from app.core.event_bus import EventBus
    event_bus = EventBus()

    # Create components
    data_feed = LiveDataFeed(event_bus, config.bybit)
    exchange = BybitAdapter(event_bus, config.bybit)
    strategy = OrderflowStrategy()
    telegram = TelegramNotifier(config.telegram)
    recorder = DataRecorder(config.data)
    trade_store = TradeStore(db_path="live_trades.db", csv_dir="live_trades")

    # Subscribe recorder to market data events for live data recording
    # This records all klines, trades, and orderbook updates to CSV files
    event_bus.subscribe(KlineEvent, recorder.record_kline)
    event_bus.subscribe(MarketTradeEvent, recorder.record_trade)
    event_bus.subscribe(OrderBookEvent, recorder.record_orderbook)

    # Create engine (share same event_bus with adapters)
    engine = Engine(
        config=config,
        data_feed=data_feed,
        exchange=exchange,
        strategy=strategy,
        event_bus=event_bus,
        telegram=telegram,
        trade_store=trade_store,
    )

    # Start services
    await telegram.start()
    await recorder.start()
    await trade_store.initialize()

    try:
        await engine.run_live()
    finally:
        await recorder.stop()
        await telegram.stop()
        await trade_store.close()


async def _run_backtest(config: Config) -> None:
    """Run backtest mode."""
    from decimal import Decimal

    from app.adapters.historical_data_feed import HistoricalDataFeed
    from app.adapters.simulated_adapter import SimulatedExchangeAdapter
    from app.core.engine import Engine
    from app.core.event_bus import EventBus
    from app.reporting.reporter import Reporter
    from app.strategies import OrderflowStrategy

    # Parse dates
    start_date = None
    end_date = None

    if config.backtest.start_date:
        start_date = datetime.strptime(config.backtest.start_date, "%Y-%m-%d")
    if config.backtest.end_date:
        end_date = datetime.strptime(config.backtest.end_date, "%Y-%m-%d")
        end_date = end_date.replace(hour=23, minute=59, second=59)

    # Create event bus
    event_bus = EventBus()

    # Create components
    data_feed = HistoricalDataFeed(
        event_bus=event_bus,
        config=config,
        start_date=start_date,
        end_date=end_date,
    )

    exchange = SimulatedExchangeAdapter(
        event_bus=event_bus,
        costs=config.costs,
        initial_balance=config.backtest.initial_capital,
        leverage=config.bybit.leverage,
    )

    strategy = OrderflowStrategy()

    # Create engine (share event_bus with adapters)
    engine = Engine(
        config=config,
        data_feed=data_feed,
        exchange=exchange,
        strategy=strategy,
        event_bus=event_bus,
    )

    # Run backtest
    await engine.run_backtest()

    # Generate report
    reporter = Reporter()
    report_dir = reporter.generate_report(config, engine.portfolio)

    # Print summary
    portfolio = engine.portfolio
    summary = portfolio.get_summary()

    click.echo("\n" + "=" * 50)
    click.echo("BACKTEST COMPLETE")
    click.echo("=" * 50)
    click.echo(f"Trades: {summary['num_trades']}")
    click.echo(f"Net P&L: ${Decimal(summary['total_pnl']):.2f}")
    click.echo(f"Return: {Decimal(summary['total_pnl_pct']):.2f}%")
    click.echo(f"Max Drawdown: {Decimal(summary['max_drawdown_pct']):.2f}%")
    click.echo(f"Report: {report_dir}")
    click.echo("=" * 50)


if __name__ == "__main__":
    cli()
