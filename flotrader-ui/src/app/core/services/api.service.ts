import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { CalendarResponse, DayResponse, TradeChartResponse } from '../models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);

  getCalendar(year: number, month: number, tz: string = 'utc'): Observable<CalendarResponse> {
    const params = new HttpParams().set('tz', tz);
    return this.http.get<CalendarResponse>(`/api/calendar/${year}/${month}`, { params });
  }

  getDayDetail(year: number, month: number, day: number, tz: string = 'utc'): Observable<DayResponse> {
    const params = new HttpParams().set('tz', tz);
    return this.http.get<DayResponse>(`/api/day/${year}/${month}/${day}`, { params });
  }

  getTradeChart(symbol: string, entryTime: number, exitTime: number, interval: string = '1'): Observable<TradeChartResponse> {
    const params = new HttpParams()
      .set('entry_time', entryTime.toString())
      .set('exit_time', exitTime.toString())
      .set('interval', interval);
    return this.http.get<TradeChartResponse>(`/api/trade-chart/${symbol}`, { params });
  }
}
