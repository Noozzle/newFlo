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
    # SL protection
    sl_cooldown_minutes: int = Field(
        default=5,
        description="Per-symbol cooldown after SL in minutes (no signals on that symbol)"
    )
    sl_direction_block_minutes: int = Field(
        default=20,
        description="Block same-direction re-entry after SL in minutes"
    )
    # Trend filter
    trend_candles: int = Field(
        default=3,
        description="Number of consecutive 15m candles required for trend confirmation"
    )
    # Global SL cooldown (cross-symbol)
    global_sl_cooldown_minutes: int = Field(
        default=10,
        description="Global cooldown after ANY symbol SL in minutes (no entries on any symbol)"
    )
    # Trading session filter (UTC hours)
    session_start_utc: int = Field(
        default=0,
        description="Start of active trading session (UTC hour, 0-23)"
    )
    session_end_utc: int = Field(
        default=24,
        description="End of active trading session (UTC hour, 0-24; 24 = no filter)"
    )

    # Breakeven SL trigger
    be_trigger_rr: Decimal = Field(
        default=Decimal("1.5"),
        description="Move SL to breakeven when price reaches this R-multiple (0 = disabled)"
    )
    # Volume spike filter
    volume_spike_mult: Decimal = Field(
        default=Decimal("2.0"),
        description="Entry only when last 1m volume >= X * avg volume (0 = disabled)"
    )
    avg_volume_periods: int = Field(
        default=5,
        description="Number of 1m candles for average volume calculation"
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


class GateRulesConfig(BaseModel):
    """Rules-based gate configuration (runs BEFORE ML predict).

    Each rule is skipped silently if its dependent feature is None/missing
    (safe default — warm-up periods won't block trading).
    """
    enabled: bool = Field(default=False, description="Enable rules layer (independent from ai_gate.enabled)")
    range_compression_kill_min: float = Field(
        default=0.0018, description="Lower bound of range_compression kill-zone (SKIP)"
    )
    range_compression_kill_max: float = Field(
        default=0.0022, description="Upper bound of range_compression kill-zone (SKIP)"
    )
    whitelist_symbols: list[str] = Field(
        default_factory=lambda: ["SUIUSDT"],
        description="Symbols allowed without strict trend strength filter",
    )
    non_whitelist_min_trend: float = Field(
        default=0.6, description="Min trend_strength for non-whitelisted symbols"
    )
    cost_ratio_skip: float = Field(
        default=0.25, description="SKIP when cost_ratio exceeds this"
    )
    half_hours_utc: list[int] = Field(
        default_factory=lambda: [13, 15, 17, 18],
        description="Hours (UTC) where risk is halved",
    )
    atr_rank_hivol: float = Field(
        default=0.6, description="atr_rank threshold classifying hi-vol regime"
    )
    trend_strength_weak: float = Field(
        default=0.3, description="Weak trend_strength cutoff (used with hivol HALF rule)"
    )


class AIGateConfig(BaseModel):
    """AI gate configuration."""
    enabled: bool = Field(default=True, description="Enable AI gate")
    model_path: str = Field(default="models/gate.joblib", description="Path to trained model")
    full_threshold: float = Field(default=0.5, description="P(win) threshold for full size")
    half_threshold: float = Field(default=0.3, description="P(win) threshold for half size")
    fallback_action: str = Field(default="full", description="Action when model unavailable: full or half")
    log_signals: bool = Field(default=True, description="Log signals for training data")
    log_path: str = Field(default="gate_signals.csv", description="Signal log path")
    rules: GateRulesConfig = Field(default_factory=GateRulesConfig)


class StrategyConfig(BaseModel):
    """Strategy configuration."""
    name: str = Field(default="orderflow_1m", description="Strategy name")
    params: StrategyParams = Field(default_factory=StrategyParams)


class RiskConfig(BaseModel):
    """Risk management configuration."""
    max_position_pct: Decimal = Field(default=Decimal("2.0"), description="Max position size % of equity")
    max_daily_sl_count: int = Field(default=3, description="Max stop-loss exits per day before trading stops")
    max_concurrent_trades: int = Field(default=3, description="Max concurrent open trades")
    max_drawdown_pct: Decimal = Field(default=Decimal("15.0"), description="Max drawdown %")
    use_trailing_stop: bool = Field(default=False, description="Use trailing stop")
    trailing_stop_pct: Decimal = Field(default=Decimal("1.0"), description="Trailing stop %")
    dd_soft_pct: Decimal = Field(default=Decimal("10.0"), description="DD level where risk scaling begins (half risk)")
    dd_hard_pct: Decimal = Field(default=Decimal("20.0"), description="DD level where trading stops completely")
    dd_cooldown_minutes: int = Field(default=60, description="Pause after hitting hard DD limit before retrying at min risk")
    per_symbol: dict[str, Decimal] = Field(
        default_factory=dict,
        description="Per-symbol risk multipliers applied to base risk amount "
        "(e.g. {'SOLUSDT': 1.3} = SOL sized 1.3x; composes with AI gate risk_scale).",
    )


class CostsConfig(BaseModel):
    """Trading costs configuration."""
    fee_entry_bps: Decimal = Field(default=Decimal("2"), description="Entry fee in bps (maker, 2 = 0.02%)")
    fee_exit_bps: Decimal = Field(default=Decimal("5.5"), description="Exit fee in bps (taker, 5.5 = 0.055%)")
    slippage_bps: Decimal = Field(default=Decimal("2"), description="Expected slippage in bps")

    @property
    def fee_entry_pct(self) -> Decimal:
        return self.fee_entry_bps / Decimal("10000")

    @property
    def fee_exit_pct(self) -> Decimal:
        return self.fee_exit_bps / Decimal("10000")

    @property
    def slippage_pct(self) -> Decimal:
        return self.slippage_bps / Decimal("10000")

    @property
    def round_trip_fee_pct(self) -> Decimal:
        """Entry + exit fees as percentage."""
        return self.fee_entry_pct + self.fee_exit_pct

    @property
    def round_trip_cost_pct(self) -> Decimal:
        """Round-trip total: entry fee + exit fee + 2 * slippage."""
        return self.round_trip_fee_pct + 2 * self.slippage_pct


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
    ai_gate: AIGateConfig = Field(default_factory=AIGateConfig)
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
