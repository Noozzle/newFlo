"""Data and exchange adapters."""

from app.adapters.data_feed import DataFeed
from app.adapters.exchange_adapter import ExchangeAdapter
from app.adapters.historical_data_feed import HistoricalDataFeed
from app.adapters.simulated_adapter import SimulatedExchangeAdapter

# Live adapters - import conditionally to avoid pybit requirement for backtest
try:
    from app.adapters.live_data_feed import LiveDataFeed
    from app.adapters.bybit_adapter import BybitAdapter
except ImportError:
    LiveDataFeed = None  # type: ignore
    BybitAdapter = None  # type: ignore

__all__ = [
    "DataFeed",
    "ExchangeAdapter",
    "HistoricalDataFeed",
    "SimulatedExchangeAdapter",
    "LiveDataFeed",
    "BybitAdapter",
]
