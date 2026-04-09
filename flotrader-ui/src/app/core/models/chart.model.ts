export interface Kline {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface TradeChartResponse {
  symbol: string;
  interval: string;
  klines: Kline[];
  take_profit: number | null;
  stop_loss: number | null;
}
