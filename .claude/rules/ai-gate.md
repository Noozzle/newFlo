Goal: AI gate filters existing strategy signals (or my historical trades) with actions:
- SKIP: do not open position
- HALF: risk *= 0.5
- FULL: risk *= 1.0

Labels:
- net_return = gross_return - fee_entry - fee_exit - slippage_est
- slippage_est default: rel_spread/2
- net_R = net_return / sl_dist_return (if SL is known); else use net_return as target.

Training:
- walk-forward time split only, report mean(net_R) and trade count.
- Export model artifact + config.json (feature list, thresholds).
