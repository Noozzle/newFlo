import { OpenPosition } from './position.model';

export interface DayStats {
  trade_count: number;
  total_pnl: string;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
}

export interface CalendarDay {
  number: number | string;
  css_class: string;
  date_str: string;
  stats: DayStats | null;
}

export interface MonthData {
  year: number;
  month: number;
  month_name: string;
  month_pnl: string;
  month_trades: number;
}

export interface CalendarResponse {
  month_data: MonthData;
  calendar_days: CalendarDay[];
  balance: string;
  open_positions: OpenPosition[];
  total_unrealized: string;
  total_unrealized_pct: string;
  testnet: boolean;
  today: string;
  prev_year: number;
  prev_month: number;
  next_year: number;
  next_month: number;
}
