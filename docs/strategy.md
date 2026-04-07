# OrderFlow 1m Strategy

**File:** `app/strategies/orderflow_1m.py`

## Overview

Scalping strategy on 1-minute timeframe that combines:
1. Trade flow volume imbalance (buy vs sell pressure)
2. Orderbook delta (bid/ask pressure change over time)
3. 15m trend filter (direction confirmation)
4. Volume spike filter (activity confirmation)

## Entry Logic

A signal fires when ALL conditions are met:

```
1. Trend filter:   N consecutive 15m candles closing in same direction
2. Volume imbalance: |buy_vol - sell_vol| / total_vol >= threshold (0.75)
3. Orderbook delta:  recent_imbalance - earlier_imbalance >= threshold (0.4)
4. Volume spike:    last 1m candle volume >= avg_vol * multiplier (2.0x)
5. No cooldowns:    per-symbol (300s), global SL (15min), direction block (20min)
6. Session window:  current hour within session_start..session_end UTC
7. ATR > 0:         sufficient warm-up data
```

**Long signal**: trend=bullish + positive imbalance + positive delta
**Short signal**: trend=bearish + negative imbalance + negative delta

## Exit Logic

| Exit Type | Condition | Implementation |
|-----------|-----------|----------------|
| Stop Loss | Price hits SL = entry +/- ATR * multiplier | SimulatedExchange / Bybit SL order |
| Take Profit | Price hits TP = entry +/- SL_dist * RR | SimulatedExchange / Bybit TP order |
| Breakeven | Price reaches BE trigger (1.5x RR) -> move SL to entry | Strategy emits ModifySignal |
| Time Exit | Hold time > max_hold_seconds (24h) | Strategy emits ExitSignal |

## Key Components

### Trend Filter (`_update_trend`)

Uses last `trend_candles` (default: 2) 15m candles. All consecutive closes must be increasing (bullish) or decreasing (bearish). Otherwise: neutral (no trading).

### Volume Imbalance

Accumulated from real trade ticks (`on_trade`) or estimated from 1m kline close/open ratio when tick data is unavailable. Reset every 1m candle.

### Orderbook Delta (`_calculate_delta_time_based`)

Compares average orderbook imbalance in two time windows:
- **Recent**: `[now - ob_window_ms, now]`
- **Earlier**: `[now - ob_window_ms - ob_compare_gap_ms, now - ob_compare_gap_ms]`

Delta = recent_avg_imbalance - earlier_avg_imbalance. Positive delta = buying pressure increasing.

### ATR Calculation (`_update_atr`)

Standard ATR from 1m candles using True Range. Period: 14 candles. Updated only on closed candles to ensure backtest/live consistency.

SL distance = ATR * `atr_multiplier` (default: 3.5)

### Cooldowns

| Cooldown | Duration | Scope |
|----------|----------|-------|
| Per-symbol signal | 300s (5min) | Same symbol only |
| Global SL | 15min | All symbols |
| Direction block | 20min | Same symbol + same direction after SL |

### Gate Features

When emitting a signal, the strategy computes features for the AI Gate:
- `rel_spread`, `ob_imbalance` -- current orderbook state
- `buy_vol_30s/60s/120s`, `sell_vol_30s/60s/120s` -- tape features
- `delta_30s/60s/120s`, `delta_ratio_30s/60s/120s` -- volume delta
- `range_pct_1m`, `close_pos_1m` -- 1m candle structure
- `atr_14_1m`, `atr_14_15m` -- volatility
- `close_minus_ma20_15m`, `trend_slope_15m` -- trend metrics
- `hour_utc` -- time of day

## Per-Symbol State (`SymbolState`)

Each symbol maintains independent state:
- Trade buffer (deque, maxlen=1000)
- Orderbook history (deque, maxlen=50)
- 1m and 15m kline buffers
- ATR values
- Trend direction
- Cooldown timestamps
- Breakeven flag
- Cached avg volume (performance optimization)

## Performance Optimizations

- `@dataclass(slots=True)` on TradeInfo and OrderbookSnapshot
- Cached avg volume computed once per 1m candle (not per OB event)
- `reversed()` iteration with early `break` in delta calculation
- Reduced orderbook history maxlen (100 -> 50)
