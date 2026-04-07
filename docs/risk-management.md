# Risk Management

**File:** `app/trading/risk_manager.py`

## Position Sizing

Fixed fractional sizing: risk `max_position_pct` (default: 2%) of equity per trade.

```
risk_amount = equity * max_position_pct / 100 * dd_scale
size = risk_amount / risk_per_unit
```

Where `risk_per_unit` = |entry_price - sl_price|.

## Cost Guard

Before approving a trade, the cost guard checks:

```
round_trip_cost = fee_entry + fee_exit + 2 * slippage
cost_vs_risk = round_trip_cost * entry_price / risk_distance
```

If `cost_vs_risk > 30%` -- trade is rejected. This prevents trades where fees would eat too much of the potential profit.

With current Bybit fees (entry 2 bps + exit 5.5 bps + slippage 2*2 bps = 11.5 bps round-trip), the minimum SL distance for a trade to be accepted is approximately `11.5 bps / 0.30 = 38.3 bps` (0.383%) of entry price.

## Adaptive Drawdown

Replaces the old binary kill switch (`max_drawdown_pct >= 15% -> stop forever`).

### Risk Scaling Levels

```
Equity Peak
  |
  |  DD = 0%      risk_scale = 1.0   (full risk)
  |  ...
  |  DD = soft%   risk_scale = 0.5   (half risk)
  |  ...
  |  DD = mid%    risk_scale = 0.25  (quarter risk)
  |  ...
  |  DD = hard%   risk_scale = 0.0   (trading paused)
  v
```

Where `mid = (soft + hard) / 2`.

### Configuration

```yaml
risk:
  dd_soft_pct: '10.0'          # Start scaling down risk
  dd_hard_pct: '25.0'          # Full stop
  dd_cooldown_minutes: 60       # Cooldown after hard limit
```

With defaults: soft=10%, mid=17.5%, hard=25%.

| DD Range | risk_scale | Position Size |
|----------|-----------|---------------|
| 0 -- 10% | 1.0 | Normal |
| 10 -- 17.5% | 0.5 | Half |
| 17.5 -- 25% | 0.25 | Quarter |
| > 25% | 0.0 | Paused |

### Auto-Recovery

When DD hits the hard limit:
1. Trading pauses immediately
2. A cooldown timer starts (`dd_cooldown_minutes`)
3. After cooldown expires AND equity recovers below hard limit:
   - Trading resumes automatically
   - Risk remains scaled based on current DD level
4. If equity recovers above soft limit, full risk is restored

This means the strategy **never permanently stops** -- it slows down during drawdowns and speeds up during recovery.

### Implementation

Method `_dd_risk_scale(event_time)` in `RiskManager`:

```python
def _dd_risk_scale(self, event_time=None) -> Decimal:
    dd = portfolio.drawdown
    if dd >= hard:  # Full stop + start cooldown
        return Decimal("0")
    if dd_hard_hit_time and elapsed < cooldown:  # Still in cooldown
        return Decimal("0")
    if dd >= mid:   return Decimal("0.25")
    if dd >= soft:  return Decimal("0.5")
    return Decimal("1")
```

The scale is applied as a multiplier on `risk_amount` in `calculate_position_size()`.

## Daily SL Limit

After `max_daily_sl_count` (default: 5) stop-loss exits in one day, trading pauses until the next UTC day. Resets at midnight UTC.

## Concurrent Trades

`max_concurrent_trades` (default: 1) limits how many positions can be open simultaneously.

## Notional Cap

Maximum position notional = 10x equity. Prevents outsized positions on low-priced assets.
