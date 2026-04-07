# Configuration Reference

All configuration lives in `config.yaml`. The Pydantic model is in `app/config.py`.

## Top-Level Structure

```yaml
mode: backtest          # backtest | live (overridden by CLI)
symbols:
  trade: [SOLUSDT]      # Symbols to trade
  record: []            # Symbols to record data only (no trading)
data:
  base_dir: live_unseen # Data directory for backtest
  format: csv           # csv | parquet
  rotation_hours: 1
  write_buffer_size: 100
strategy: ...
ai_gate: ...
risk: ...
costs: ...
telegram: ...
bybit: ...
backtest: ...
```

## Strategy Parameters

```yaml
strategy:
  name: orderflow_1m
  params:
    # Core thresholds
    imbalance_threshold: '0.75'   # Volume imbalance threshold (-1 to 1)
    delta_threshold: '0.4'        # Orderbook delta threshold
    rr_ratio: '3.0'              # Risk:Reward ratio (TP = RR * SL distance)

    # ATR / SL calculation
    atr_period: 14                # ATR lookback (1m candles)
    atr_multiplier: '3.5'        # SL = ATR * multiplier

    # Trend filter (15m candles)
    trend_period: 15              # (unused, kept for compat)
    trend_candles: 2              # N consecutive 15m candles for trend confirmation

    # Volume
    lookback_trades: 100          # Trade buffer size
    volume_spike_mult: '2.0'     # Last 1m volume must exceed N * avg

    # Cooldowns
    cooldown_seconds: 300         # Per-symbol cooldown after signal
    global_sl_cooldown_minutes: 15 # Cross-symbol cooldown after any SL

    # Session filter
    session_start_utc: 12         # Trading window start (UTC hour)
    session_end_utc: 22           # Trading window end (UTC hour, 24=no filter)

    # Breakeven
    be_trigger_rr: '1.5'         # Move SL to entry when price reaches N * RR

    # Orderbook delta mode
    fast_orderbook_mode: true
    ob_window_ms: 2000            # Recent OB window for delta
    ob_compare_gap_ms: 2000       # Earlier OB window offset
    orderbook_bucket_ms: 3000     # OB downsampling interval (backtest)
```

## Risk Management

```yaml
risk:
  max_position_pct: '2.0'       # Risk % of equity per trade
  max_daily_sl_count: 5          # Max SL exits per day
  max_concurrent_trades: 1       # Max open positions
  max_drawdown_pct: '15.0'      # (legacy, kept for compat)
  use_trailing_stop: false
  trailing_stop_pct: '1.0'

  # Adaptive drawdown (replaces binary kill switch)
  dd_soft_pct: '10.0'           # DD where risk scaling begins (0.5x)
  dd_hard_pct: '25.0'           # DD where trading pauses
  dd_cooldown_minutes: 60        # Pause duration after hard DD hit
```

See [risk-management.md](risk-management.md) for details on adaptive drawdown.

## Trading Costs

```yaml
costs:
  fee_entry_bps: '2'            # Entry fee in bps (maker)
  fee_exit_bps: '5.5'           # Exit fee in bps (taker)
  slippage_bps: '2'             # Expected slippage in bps
```

Round-trip cost = `fee_entry + fee_exit + 2 * slippage` = 11.5 bps.

The cost guard in `risk_manager.py` rejects trades where round-trip costs exceed 30% of SL distance.

## AI Gate

```yaml
ai_gate:
  enabled: false                 # Enable/disable ML filter
  model_path: model/ai_gate_model.pkl
  full_threshold: 0.5            # Score >= full -> FULL risk (1.0x)
  half_threshold: 0.3            # Score >= half -> HALF risk (0.5x)
  fallback_action: half          # Default when model unavailable
  log_signals: true
  log_path: gate_signals.csv
```

Actions: `SKIP` (don't trade), `HALF` (risk * 0.5), `FULL` (risk * 1.0).

## Telegram Notifications

```yaml
telegram:
  enabled: true
  token: '<bot_token>'
  chat_id: '<chat_id>'
  notify_on_entry: true
  notify_on_exit: true
  notify_on_sl_tp: true
  notify_on_error: true
  notify_daily_summary: true
```

## Bybit Connection

```yaml
bybit:
  testnet: false
  category: linear               # linear (USDT perps)
  leverage: 10
  recv_window: 5000
  api_key: '<key>'
  api_secret: '<secret>'
```

## Backtest

```yaml
backtest:
  start_date: '2026-02-01'
  end_date: '2026-02-20'
  initial_capital: '50'
  symbols: null                  # null = use symbols.trade list
```
