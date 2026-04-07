# FloTrader Documentation

FloTrader v1.0.0 -- event-driven algo-trading framework for Bybit (perpetual futures).
Python 3.11+, asyncio, backtest & live modes share the same engine.

## Contents

| Document | Description |
|----------|-------------|
| [architecture.md](architecture.md) | System architecture, event flow, module map |
| [configuration.md](configuration.md) | config.yaml reference, all parameters |
| [strategy.md](strategy.md) | OrderFlow 1m strategy logic, entry/exit rules |
| [risk-management.md](risk-management.md) | Position sizing, adaptive drawdown, cost guard |
| [ai-gate.md](ai-gate.md) | AI Gate pipeline: training, inference, labels |
| [backtest.md](backtest.md) | Running backtests, reports, performance tuning |
| [live-trading.md](live-trading.md) | Live mode, Bybit setup, Telegram alerts |
| [data-pipeline.md](data-pipeline.md) | Data normalization, parquet format, scripts |

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Backtest (uses config.yaml dates & symbols)
python -m app backtest

# Backtest specific symbol/dates
python -m app backtest -s SOLUSDT --from 2026-02-01 --to 2026-02-28

# Live trading
python -m app live

# Web UI
python -m app ui
```
