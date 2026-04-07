# AI Gate

**File:** `app/trading/ai_gate.py`

## Overview

The AI Gate is an ML filter that sits between strategy signals and order execution. For each entry signal, it predicts whether the trade is likely profitable and assigns one of three actions:

| Action | Risk Multiplier | Meaning |
|--------|----------------|---------|
| SKIP | 0.0 | Do not open position |
| HALF | 0.5 | Open with half risk |
| FULL | 1.0 | Open with full risk |

## How It Works

```
Strategy signal -> AI Gate -> SKIP/HALF/FULL -> OrderManager
                     |
              Model prediction (probability)
                     |
              >= full_threshold (0.5)  -> FULL
              >= half_threshold (0.3)  -> HALF
              < half_threshold         -> SKIP
```

When the gate is disabled or the model is unavailable, it uses `fallback_action` (default: `half`).

## Labels

Training labels are based on net return after costs:

```
net_return = gross_return - fee_entry - fee_exit - slippage_est
slippage_est = rel_spread / 2  (default, from L1 orderbook)
net_R = net_return / sl_dist_return  (if SL is known)
```

## Training Pipeline

### 1. Normalize Data

```bash
python -m scripts.ai_gate.normalize --data-dir live_unseen/SOLUSDT --out data/normalized/SOLUSDT
```

Converts raw CSV data into aligned parquet files (candles_1m, candles_15m, l1, tape).

### 2. Generate Signals & Features

```bash
python -m scripts.ai_gate.generate_signals \
  --config config.yaml \
  --norm-dir data/normalized/SOLUSDT \
  --symbol SOLUSDT \
  --start 2026-02-01 --end 2026-02-20 \
  --label-mode sl_tp_sim \
  --out data/datasets/signals_labeled.parquet
```

Replays the strategy on normalized data and computes features + labels for each signal.

### 3. Train Model

```bash
python -m scripts.ai_gate.train \
  --data data/datasets/signals_labeled.parquet \
  --out model/
```

Trains a LightGBM classifier with walk-forward time split. Outputs:
- `model/ai_gate_model.pkl` -- trained model
- `model/config.json` -- feature list, thresholds, metadata

### 4. Enable in Config

```yaml
ai_gate:
  enabled: true
  model_path: model/ai_gate_model.pkl
  full_threshold: 0.5
  half_threshold: 0.3
```

## Features

The gate receives features computed by the strategy at signal time:

| Category | Features |
|----------|----------|
| Orderbook | `rel_spread`, `ob_imbalance` |
| Tape (30s/60s/120s) | `buy_vol`, `sell_vol`, `delta`, `trade_count`, `avg_trade_size`, `delta_ratio` |
| 1m candle | `range_pct_1m`, `close_pos_1m`, `atr_14_1m` |
| 15m candle | `atr_14_15m`, `close_minus_ma20_15m`, `trend_slope_15m` |
| Time | `hour_utc` |

## Validation Rules

- Walk-forward time split only (no shuffle, no random split)
- Features use only data <= signal timestamp (no leakage)
- Report `mean(net_R)` and trade count per fold

## Signal Logging

When `log_signals: true`, every gate decision is logged to `gate_signals.csv` with features, prediction score, and action taken.
