"""Check ATR values and cost requirements."""
import pandas as pd

# Read SOL 1m data
df = pd.read_csv('live_data/SOLUSDT/1m.csv')
df['timestamp'] = pd.to_datetime(df['timestamp'])

# Calculate True Range
df['prev_close'] = df['close'].shift(1)
df['tr'] = df.apply(lambda r: max(
    r['high'] - r['low'],
    abs(r['high'] - r['prev_close']) if pd.notna(r['prev_close']) else r['high'] - r['low'],
    abs(r['low'] - r['prev_close']) if pd.notna(r['prev_close']) else r['high'] - r['low']
), axis=1)

# Calculate ATR(14)
df['atr'] = df['tr'].rolling(14).mean()

print('SOLUSDT ATR Statistics:')
print('='*50)

# December 2025
dec = df[(df['timestamp'] >= '2025-12-16') & (df['timestamp'] < '2026-01-01')]
print(f"Dec 2025: ATR mean={dec['atr'].mean():.4f}, min={dec['atr'].min():.4f}, max={dec['atr'].max():.4f}")

# January 2026
jan = df[(df['timestamp'] >= '2026-01-01') & (df['timestamp'] < '2026-02-01')]
print(f"Jan 2026: ATR mean={jan['atr'].mean():.4f}, min={jan['atr'].min():.4f}, max={jan['atr'].max():.4f}")

# Feb 2026
feb = df[df['timestamp'] >= '2026-02-01']
print(f"Feb 2026: ATR mean={feb['atr'].mean():.4f}, min={feb['atr'].min():.4f}, max={feb['atr'].max():.4f}")

print()
print('Cost analysis:')
fees_bps = 10
slippage_bps = 2
round_trip = 2 * (fees_bps + slippage_bps) / 10000
print(f'Round-trip cost: {round_trip*100:.2f}%')

# For SOL ~$120
price = 120
min_sl_pct = round_trip / 0.30  # max 30% of risk as costs
min_sl = price * min_sl_pct
print(f'Min SL distance for SOL@{price}: ${min_sl:.2f} ({min_sl_pct*100:.2f}%)')

# With atr_multiplier 2.5
atr_needed = min_sl / 2.5
print(f'ATR needed with multiplier 2.5: ${atr_needed:.4f}')
print(f"Current Jan avg ATR: ${jan['atr'].mean():.4f}")
