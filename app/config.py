"""Configuration models using Pydantic."""

from __future__ import annotations

import os
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings


class Mode(str, Enum):
    """Trading mode."""
    LIVE = "live"
    BACKTEST = "backtest"


class DataFormat(str, Enum):
    """Data storage format."""
    CSV = "csv"
    PARQUET = "parquet"


class SymbolsConfig(BaseModel):
    """Symbols configuration."""
    trade: list[str] = Field(default_factory=list, description="Symbols to trade")
    record: list[str] = Field(default_factory=list, description="Symbols to record only")

    @property
    def all_symbols(self) -> list[str]:
        """Get all symbols (trade + record)."""
        return list(set(self.trade + self.record))


class DataConfig(BaseModel):
    """Data storage configuration."""
    base_dir: Path = Field(default=Path("live_data"), description="Base data directory")
    format: DataFormat = Field(default=DataFormat.CSV, description="Data format")
    rotation_hours: int = Field(default=1, description="File rotation interval in hours")
    write_buffer_size: int = Field(default=100, description="Write buffer size")


class StrategyParams(BaseModel):
    """Strategy parameters (extensible)."""
    # Orderflow strategy defaults
    trend_period: int = Field(default=15, description="Trend period in minutes")
    imbalance_threshold: Decimal = Field(default=Decimal("0.6"), description="Imbalance threshold (0-1)")
    delta_threshold: Decimal = Field(default=Decimal("0.3"), description="Delta threshold")
    rr_ratio: Decimal = Field(default=Decimal("3.0"), description="Risk/Reward ratio")
    min_wr: Decimal = Field(default=Decimal("0.35"), description="Minimum win rate target")
    atr_period: int = Field(default=14, description="ATR period for SL calculation")
    atr_multiplier: Decimal = Field(default=Decimal("1.5"), description="ATR multiplier for SL")
    lookback_trades: int = Field(default=100, description="Number of trades to look back")
    cooldown_seconds: int = Field(default=60, description="Cooldown between trades")
    # Volume estimation from kline
    use_kline_volume_when_no_trades: bool = Field(
        default=True,
        description="Use kline volume estimation when no trade events received"
    )
    no_trades_timeout_seconds: int = Field(
        default=5,
        description="Seconds without trades before falling back to kline volume estimation"
    )
    # Orderbook delta calculation
    use_time_based_delta: bool = Field(
        default=True,
        description="Use time-based delta calculation instead of tick-based"
    )
    ob_window_ms: int = Field(
        default=500,
        description="Time window in ms for recent orderbook snapshots"
    )
    ob_compare_gap_ms: int = Field(
        default=0,
        description="Gap between windows in ms (0 = same as ob_window_ms)"
    )
    # Performance optimization
    fast_orderbook_mode: bool = Field(
        default=True,
        description="Use float parsing for orderbook prices in backtest (faster, epsilon precision trade-off)"
    )
    orderbook_bucket_ms: int = Field(
        default=50,
        description="Downsample orderbook to bucket_ms intervals (0 = disabled, keeps all events)"
    )


class StrategyConfig(BaseModel):
    """Strategy configuration."""
    name: str = Field(default="orderflow_1m", description="Strategy name")
    params: StrategyParams = Field(default_factory=StrategyParams)


class RiskConfig(BaseModel):
    """Risk management configuration."""
    max_position_pct: Decimal = Field(default=Decimal("2.0"), description="Max position size % of equity")
    max_daily_loss_pct: Decimal = Field(default=Decimal("5.0"), description="Max daily loss %")
    max_concurrent_trades: int = Field(default=3, description="Max concurrent open trades")
    max_drawdown_pct: Decimal = Field(default=Decimal("15.0"), description="Max drawdown %")
    use_trailing_stop: bool = Field(default=False, description="Use trailing stop")
    trailing_stop_pct: Decimal = Field(default=Decimal("1.0"), description="Trailing stop %")


class CostsConfig(BaseModel):
    """Trading costs configuration."""
    fees_bps: Decimal = Field(default=Decimal("6"), description="Trading fees in bps (6 = 0.06%)")
    slippage_bps: Decimal = Field(default=Decimal("2"), description="Expected slippage in bps")

    @property
    def fees_pct(self) -> Decimal:
        """Get fees as percentage."""
        return self.fees_bps / Decimal("10000")

    @property
    def slippage_pct(self) -> Decimal:
        """Get slippage as percentage."""
        return self.slippage_bps / Decimal("10000")

    @property
    def total_cost_pct(self) -> Decimal:
        """Get total cost (fees + slippage) per side as percentage."""
        return self.fees_pct + self.slippage_pct


class TelegramConfig(BaseModel):
    """Telegram notification configuration."""
    enabled: bool = Field(default=False, description="Enable Telegram notifications")
    token: str = Field(default="", description="Bot token")
    chat_id: str = Field(default="", description="Chat ID")
    notify_on_entry: bool = Field(default=True)
    notify_on_exit: bool = Field(default=True)
    notify_on_sl_tp: bool = Field(default=True)
    notify_on_error: bool = Field(default=True)
    notify_daily_summary: bool = Field(default=True)

    @field_validator("token", "chat_id", mode="before")
    @classmethod
    def resolve_env_var(cls, v: str) -> str:
        """Resolve environment variable references like ${VAR_NAME}."""
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            env_var = v[2:-1]
            return os.environ.get(env_var, "")
        return v


class BybitConfig(BaseModel):
    """Bybit exchange configuration."""
    testnet: bool = Field(default=True, description="Use testnet")
    category: str = Field(default="linear", description="Product category (linear/inverse)")
    leverage: int = Field(default=10, description="Default leverage")
    recv_window: int = Field(default=5000, description="Recv window in ms")
    # Keys loaded from environment
    api_key: str = Field(default="", description="API key (from env)")
    api_secret: str = Field(default="", description="API secret (from env)")

    @field_validator("api_key", "api_secret", mode="before")
    @classmethod
    def resolve_env_var(cls, v: str) -> str:
        """Resolve environment variable references."""
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            env_var = v[2:-1]
            return os.environ.get(env_var, "")
        return v

    def model_post_init(self, __context: Any) -> None:
        """Load API keys from environment if not set."""
        if not self.api_key:
            self.api_key = os.environ.get("BYBIT_API_KEY", "")
        if not self.api_secret:
            self.api_secret = os.environ.get("BYBIT_API_SECRET", "")


class BacktestConfig(BaseModel):
    """Backtest-specific configuration."""
    start_date: str | None = Field(default=None, description="Backtest start date (YYYY-MM-DD)")
    end_date: str | None = Field(default=None, description="Backtest end date (YYYY-MM-DD)")
    initial_capital: Decimal = Field(default=Decimal("10000"), description="Initial capital")
    symbols: list[str] | None = Field(default=None, description="Override symbols for backtest")


class Config(BaseModel):
    """Main configuration model."""
    mode: Mode = Field(default=Mode.BACKTEST, description="Trading mode")
    symbols: SymbolsConfig = Field(default_factory=SymbolsConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    costs: CostsConfig = Field(default_factory=CostsConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    bybit: BybitConfig = Field(default_factory=BybitConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> Config:
        """Load configuration from YAML file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path) as f:
            data = yaml.safe_load(f)

        return cls.model_validate(data)

    def to_yaml(self, path: str | Path) -> None:
        """Save configuration to YAML file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Convert to dict, handling Decimals
        data = self.model_dump(mode="json")

        with open(path, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)


class EnvSettings(BaseSettings):
    """Environment-based settings."""
    bybit_api_key: str = Field(default="", alias="BYBIT_API_KEY")
    bybit_api_secret: str = Field(default="", alias="BYBIT_API_SECRET")
    bybit_testnet: bool = Field(default=True, alias="BYBIT_TESTNET")
    telegram_token: str = Field(default="", alias="TELEGRAM_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
