# Data Pipeline

## Data Sources

### Live Recording

Live mode records raw Bybit WebSocket data to CSV:

```
live_data/<SYMBOL>/
  kline_1_1m/       # 1m candles: timestamp,open,high,low,close,volume,turnover,confirm
  kline_1_15m/      # 15m candles (same format)
  publicTrade/      # Ticks: timestamp,price,amount,side
  orderbook_1/      # L1 orderbook: timestamp,bid_price,bid_size,ask_price,ask_size
```

Files rotate hourly: `YYYY-MM-DD_HH.csv`.

### Historical Data for Backtest

Place recorded data in `data.base_dir` (default: `live_unseen/`). The historical data feed loads all CSV files within the date range, merges events from all channels via k-way merge (time-sorted).

## Parquet Normalization

For faster backtests and ML pipelines, convert CSV to parquet:

### Step 1: Convert to Parquet

```bash
python -m scripts.convert_to_parquet \
  --input live_unseen/SOLUSDT \
  --output data/normalized/SOLUSDT
```

### Step 2: Normalize for ML

```bash
python -m scripts.ai_gate.normalize \
  --data-dir live_unseen/SOLUSDT \
  --out data/normalized/SOLUSDT
```

Output structure:
```
data/normalized/SOLUSDT/
  candles_1m.parquet     # timestamp, open, high, low, close, volume
  candles_15m.parquet    # same schema
  l1.parquet             # timestamp, bid_price, bid_size, ask_price, ask_size
  tape.parquet           # timestamp, price, amount, side
```

## Orderbook Downsampling

Raw orderbook data can have 5-20 events/second. The `orderbook_bucket_ms` config controls downsampling in backtest:

- `1000` = 1 OB event per second (default)
- `3000` = 1 OB event per 3 seconds (3x faster backtest)
- `5000` = 1 OB event per 5 seconds (5x faster)

Uses LOCF (Last Observation Carried Forward) -- keeps the last snapshot per bucket.

## ML Datasets

### Signal Dataset

```bash
python -m scripts.ai_gate.generate_signals \
  --config config.yaml \
  --norm-dir data/normalized/SOLUSDT \
  --symbol SOLUSDT \
  --start 2026-02-01 --end 2026-02-20 \
  --label-mode sl_tp_sim \
  --out data/datasets/signals_labeled.parquet
```

Columns: all gate features + `net_return`, `net_R`, `label`, `sl_dist`, `tp_dist`, `exit_reason`.

### My Trades Dataset

Build dataset from actual live trades:

```bash
python -m scripts.ai_gate.build_mytrades_dataset \
  --trades live_trades.db \
  --norm-dir data/normalized \
  --out data/datasets/mytrades_labeled.parquet
```

## Utility Scripts

| Script | Purpose |
|--------|---------|
| `scripts/convert_to_parquet.py` | CSV -> Parquet batch conversion |
| `scripts/aggregate_orderbook.py` | Aggregate OB ticks to OHLCV-like |
| `scripts/aggregate_all_data.py` | Aggregate all channels |
| `scripts/check_atr.py` | ATR analysis across symbols |
| `scripts/regression_test.py` | Compare two backtest runs for consistency |
| `scripts/walkforward_eval.py` | Walk-forward evaluation of AI gate |
