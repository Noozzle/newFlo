export interface OpenPosition {
  symbol: string;
  side: string;
  size: string;
  entry_price: string;
  mark_price: string;
  unrealized_pnl: string;
  pnl_pct: string;
  leverage: number;
  created_time: string;
  liq_price: string | null;
  position_value: string | null;
}
