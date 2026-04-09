export interface ClosedTrade {
  symbol: string;
  side: string;
  closed_pnl: string;
  pnl_pct: string;
  entry_time: string;
  exit_time: string;
  avg_entry_price: string;
  avg_exit_price: string;
  closed_size: string;
  leverage: number;
  order_type: string;
}
