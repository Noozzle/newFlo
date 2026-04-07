# Backtest Guide

## Running a Backtest

```bash
# Default (uses config.yaml settings)
python -m app backtest

# Specific symbol
python -m app backtest -s SOLUSDT

# Custom date range
python -m app backtest --from 2026-02-01 --to 2026-02-28

# Debug logging
python -m app backtest --log-level DEBUG

# Custom config file
python -m app backtest -c config.working33.yaml
```

## Data Requirements

Backtest reads from `data.base_dir` (default: `live_unseen/`). Expected structure:

```
live_unseen/
  SOLUSDT/
    kline_1_1m/
      2026-02-01_00.csv    # Hourly rotation
      2026-02-01_01.csv
      ...
    kline_1_15m/
      ...
    publicTrade/
      ...
    orderbook_1/
      ...
  ETHUSDT/
    ...
```

Also supports parquet format from `data/normalized/`:
```
data/normalized/
  SOLUSDT/
    candles_1m.parquet
    candles_15m.parquet
    l1.parquet              # L1 orderbook
    tape.parquet            # Trade ticks
```

## Reports

After each backtest, a report is generated in `reports/<timestamp>/`:

| File | Content |
|------|---------|
| `summary.md` | Human-readable performance summary |
| `metrics.json` | Machine-readable metrics |
| `trades.csv` | All trades with entry/exit details |
| `equity_curve.csv` | Balance/equity over time |
| `config.yaml` | Config snapshot for reproducibility |

### Key Metrics

| Metric | Description |
|--------|-------------|
| Win Rate | Winning trades / total trades |
| Profit Factor | Gross wins / gross losses |
| Net P&L | After fees and slippage |
| Max Drawdown | Peak-to-trough equity decline |
| Risk/Reward | Average win / average loss |
| Expectancy | Average net P&L per trade |

## Regenerate Report

```bash
python -m app report -r reports/20260407_175944
```

## Performance Profile

The engine prints a timing breakdown after each backtest:

```
BACKTEST PROFILE  (2,500,000 events in 45.2s = 55,310 ev/s)
  Data load/parse       12.50s  (27.7%)
  Event dispatch tot    30.20s  (66.8%)
    Strategy handlers   18.30s  (40.5%)
    Simulated exch       5.10s  (11.3%)
    Signal (gate+OM)     0.02s  ( 0.0%)
    Bus overhead         6.78s  (15.0%)
  Other (setup/log)      2.50s  ( 5.5%)
```

## Performance Tuning

### Quick Wins

| Setting | Default | Faster | Impact |
|---------|---------|--------|--------|
| `orderbook_bucket_ms` | 1000 | 3000-5000 | -60-80% OB events |
| `data.format` | csv | parquet | Faster parsing, column pruning |

### What Affects Speed

1. **Number of OB events** -- dominates event count. `orderbook_bucket_ms` controls downsampling
2. **Number of symbols** -- linear scaling (each adds its own events)
3. **Date range** -- linear scaling
4. **Strategy complexity** -- `_check_signal` runs on every OB event

### Code-Level Optimizations (already applied)

- Fast-path dispatch bypasses EventBus for data events
- `@dataclass(slots=True)` on hot-path objects
- Cached avg volume (avoids `list()` conversion per OB event)
- `reversed()` + early break in delta calculation
- Reduced deque sizes (orderbook_history: 50, trades: 1000)
