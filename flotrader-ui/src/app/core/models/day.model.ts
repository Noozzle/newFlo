import { ClosedTrade } from './trade.model';
import { OpenPosition } from './position.model';

export interface DayResponse {
  date: string;
  trades: ClosedTrade[];
  open_positions: OpenPosition[];
  total_pnl: string;
  total_pnl_pct: string;
  unrealized_pnl: string;
  unrealized_pnl_pct: string;
  trade_count: number;
  winning_count: number;
  losing_count: number;
  win_rate: number;
  balance: string;
  testnet: boolean;
}
