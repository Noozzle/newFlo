import { Component, inject, signal, computed, effect, OnInit, OnDestroy, ElementRef, ViewChild, ChangeDetectionStrategy } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { NgClass } from '@angular/common';
import { ApiService, TimezoneService, WebSocketService } from '../../core/services';
import { DayResponse, ClosedTrade, TradeChartResponse } from '../../core/models';
import { PnlColorPipe, PnlSignPipe } from '../../shared/pnl-color.pipe';
import { createChart, IChartApi, ISeriesApi, CandlestickData, Time, CandlestickSeries, createSeriesMarkers } from 'lightweight-charts';

@Component({
  selector: 'app-day-detail',
  standalone: true,
  imports: [NgClass, RouterLink, PnlColorPipe, PnlSignPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <!-- Back + Date Header -->
    <div class="flex items-center gap-4 mb-6">
      <a routerLink="/" class="nav-btn">&larr; Back</a>
      <h1 class="text-xl font-semibold">{{ formatDate() }}</h1>
    </div>

    @if (data()) {
      <!-- Stats Cards -->
      <div class="grid grid-cols-2 sm:grid-cols-5 gap-3 mb-6">
        <div class="card">
          <div class="card-label">Realized P&L</div>
          <div class="card-value font-mono" [ngClass]="data()!.total_pnl | pnlColor">
            {{ data()!.total_pnl | pnlSign }} USDT
          </div>
          <div class="text-xs font-mono mt-0.5" [ngClass]="data()!.total_pnl_pct | pnlColor">
            {{ data()!.total_pnl_pct | pnlSign }}%
          </div>
        </div>
        <div class="card">
          <div class="card-label">Unrealized</div>
          <div class="card-value font-mono" [ngClass]="data()!.unrealized_pnl | pnlColor">
            {{ data()!.unrealized_pnl | pnlSign }}
          </div>
        </div>
        <div class="card">
          <div class="card-label">Trades</div>
          <div class="card-value">{{ data()!.trade_count }}</div>
        </div>
        <div class="card">
          <div class="card-label">Win Rate</div>
          <div class="card-value">{{ data()!.win_rate }}%</div>
        </div>
        <div class="card">
          <div class="card-label">W / L</div>
          <div class="card-value">
            <span class="text-profit">{{ data()!.winning_count }}</span>
            /
            <span class="text-loss">{{ data()!.losing_count }}</span>
          </div>
        </div>
      </div>

      <!-- Trade Chart -->
      @if (selectedTrade()) {
        <div class="card mb-6">
          <div class="flex items-center justify-between mb-3">
            <div class="flex items-center gap-3">
              <span class="font-medium">{{ selectedTrade()!.symbol }}</span>
              <span class="text-xs px-1.5 py-0.5 rounded"
                    [ngClass]="selectedTrade()!.side === 'Sell' ? 'bg-profit/20 text-profit' : 'bg-loss/20 text-loss'">
                {{ selectedTrade()!.side === 'Sell' ? 'LONG' : 'SHORT' }}
              </span>
            </div>
            <!-- Interval selector -->
            <div class="flex gap-1">
              @for (iv of intervals; track iv) {
                <button
                  (click)="changeInterval(iv)"
                  class="px-2 py-1 text-xs rounded transition-colors"
                  [ngClass]="chartInterval() === iv ? 'bg-accent text-surface-900 font-medium' : 'bg-surface-700 text-[var(--text-secondary)] hover:bg-surface-600'">
                  {{ iv }}m
                </button>
              }
            </div>
          </div>
          <div #chartContainer class="w-full h-[400px] rounded-lg overflow-hidden"></div>
        </div>
      }

      <!-- Trades Table -->
      @if (data()!.trades.length > 0) {
        <div class="card mb-6 overflow-x-auto">
          <h3 class="text-sm font-medium text-[var(--text-secondary)] mb-3">Closed Trades</h3>
          <table class="w-full text-sm">
            <thead>
              <tr class="text-[var(--text-secondary)] text-xs border-b border-[var(--border)]">
                <th class="text-left py-2 px-2">Symbol</th>
                <th class="text-left py-2 px-2">Side</th>
                <th class="text-right py-2 px-2">Size</th>
                <th class="text-right py-2 px-2">Entry</th>
                <th class="text-right py-2 px-2">Exit</th>
                <th class="text-right py-2 px-2">P&L</th>
                <th class="text-right py-2 px-2">P&L %</th>
                <th class="text-right py-2 px-2">Lev</th>
                <th class="text-right py-2 px-2">Entry Time</th>
              </tr>
            </thead>
            <tbody>
              @for (trade of data()!.trades; track $index) {
                <tr
                  class="border-b border-[var(--border)]/50 hover:bg-surface-700/30 cursor-pointer transition-colors"
                  [ngClass]="{'bg-accent/5': selectedTrade() === trade}"
                  (click)="selectTrade(trade)">
                  <td class="py-2.5 px-2 font-medium">{{ trade.symbol }}</td>
                  <td class="py-2.5 px-2">
                    <span class="text-xs px-1.5 py-0.5 rounded"
                          [ngClass]="trade.side === 'Sell' ? 'bg-profit/20 text-profit' : 'bg-loss/20 text-loss'">
                      {{ trade.side === 'Sell' ? 'LONG' : 'SHORT' }}
                    </span>
                  </td>
                  <td class="py-2.5 px-2 text-right font-mono">{{ trade.closed_size }}</td>
                  <td class="py-2.5 px-2 text-right font-mono">{{ trade.avg_entry_price }}</td>
                  <td class="py-2.5 px-2 text-right font-mono">{{ trade.avg_exit_price }}</td>
                  <td class="py-2.5 px-2 text-right font-mono" [ngClass]="trade.closed_pnl | pnlColor">
                    {{ trade.closed_pnl | pnlSign }}
                  </td>
                  <td class="py-2.5 px-2 text-right font-mono" [ngClass]="trade.pnl_pct | pnlColor">
                    {{ trade.pnl_pct | pnlSign }}%
                  </td>
                  <td class="py-2.5 px-2 text-right">{{ trade.leverage }}x</td>
                  <td class="py-2.5 px-2 text-right text-xs text-[var(--text-secondary)]">
                    {{ formatTime(trade.entry_time) }}
                  </td>
                </tr>
              }
            </tbody>
          </table>
        </div>
      }

      <!-- Open Positions -->
      @if (livePositions().length > 0) {
        <div class="card">
          <h3 class="text-sm font-medium text-[var(--text-secondary)] mb-3">Open Positions</h3>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            @for (pos of livePositions(); track pos.symbol) {
              <div class="flex items-center justify-between p-3 rounded-lg bg-surface-700/30 border border-[var(--border)]/50">
                <div>
                  <span class="font-medium text-sm">{{ pos.symbol }}</span>
                  <span class="ml-2 text-xs px-1.5 py-0.5 rounded"
                        [ngClass]="pos.side === 'Buy' ? 'bg-profit/20 text-profit' : 'bg-loss/20 text-loss'">
                    {{ pos.side === 'Buy' ? 'LONG' : 'SHORT' }}
                  </span>
                  <div class="text-xs text-[var(--text-secondary)] mt-1">
                    {{ pos.size }} &#64; {{ pos.entry_price }} &rarr; {{ pos.mark_price }}
                  </div>
                </div>
                <div class="text-right">
                  <div class="font-mono text-sm" [ngClass]="pos.unrealized_pnl | pnlColor">
                    {{ pos.unrealized_pnl | pnlSign }} USDT
                  </div>
                  <div class="font-mono text-xs" [ngClass]="pos.pnl_pct | pnlColor">
                    {{ pos.pnl_pct | pnlSign }}%
                  </div>
                </div>
              </div>
            }
          </div>
        </div>
      }
    } @else {
      <div class="flex items-center justify-center h-64">
        <div class="animate-spin w-8 h-8 border-2 border-accent border-t-transparent rounded-full"></div>
      </div>
    }
  `,
  styles: [`
    .card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 0.75rem;
      padding: 1rem;
    }
    .card-label {
      font-size: 0.75rem;
      color: var(--text-secondary);
      margin-bottom: 0.25rem;
    }
    .card-value {
      font-size: 1.125rem;
      font-weight: 600;
    }
    .nav-btn {
      padding: 0.5rem 1rem;
      border-radius: 0.5rem;
      background: var(--bg-card);
      border: 1px solid var(--border);
      color: var(--text-primary);
      cursor: pointer;
      font-size: 0.875rem;
      transition: background 0.15s;
      text-decoration: none;
    }
    .nav-btn:hover {
      background: var(--bg-hover);
      text-decoration: none;
    }
    table { border-collapse: collapse; }
  `],
})
export class DayDetailComponent implements OnInit, OnDestroy {
  @ViewChild('chartContainer') chartContainer!: ElementRef<HTMLElement>;

  private api = inject(ApiService);
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private tzService = inject(TimezoneService);
  private ws = inject(WebSocketService);

  data = signal<DayResponse | null>(null);
  selectedTrade = signal<ClosedTrade | null>(null);
  chartInterval = signal('1');
  intervals = ['1', '5', '15', '60'];

  private chart: IChartApi | null = null;
  private candleSeries: ISeriesApi<'Candlestick', Time> | null = null;

  year = 0;
  month = 0;
  day = 0;

  livePositions = computed(() => {
    const wsPositions = this.ws.positions();
    if (wsPositions.length > 0) return wsPositions;
    return this.data()?.open_positions ?? [];
  });

  ngOnInit(): void {
    const params = this.route.snapshot.params;
    this.year = +params['year'];
    this.month = +params['month'];
    this.day = +params['day'];
    this.loadData();
  }

  loadData(): void {
    this.api.getDayDetail(this.year, this.month, this.day, this.tzService.tz()).subscribe(data => {
      this.data.set(data);
      // Auto-select first trade
      if (data.trades.length > 0 && !this.selectedTrade()) {
        this.selectTrade(data.trades[0]);
      }
    });
  }

  selectTrade(trade: ClosedTrade): void {
    this.selectedTrade.set(trade);
    this.loadChart(trade);
  }

  changeInterval(interval: string): void {
    this.chartInterval.set(interval);
    const trade = this.selectedTrade();
    if (trade) this.loadChart(trade);
  }

  private loadChart(trade: ClosedTrade): void {
    const entryTime = Math.floor(new Date(trade.entry_time).getTime() / 1000);
    const exitTime = Math.floor(new Date(trade.exit_time).getTime() / 1000);

    this.api.getTradeChart(trade.symbol, entryTime, exitTime, this.chartInterval()).subscribe(chartData => {
      // Wait for next tick so ViewChild is available
      setTimeout(() => this.renderChart(chartData, trade), 0);
    });
  }

  private renderChart(chartData: TradeChartResponse, trade: ClosedTrade): void {
    if (!this.chartContainer) return;
    const container = this.chartContainer.nativeElement;

    // Clean up existing chart
    if (this.chart) {
      this.chart.remove();
      this.chart = null;
    }

    this.chart = createChart(container, {
      layout: {
        background: { color: '#12121a' },
        textColor: '#9ca3af',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: '#1a1a25' },
        horzLines: { color: '#1a1a25' },
      },
      crosshair: {
        mode: 0,
      },
      rightPriceScale: {
        borderColor: '#2a2a3a',
      },
      timeScale: {
        borderColor: '#2a2a3a',
        timeVisible: true,
        secondsVisible: false,
      },
      width: container.clientWidth,
      height: 400,
    });

    this.candleSeries = this.chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderUpColor: '#22c55e',
      borderDownColor: '#ef4444',
      wickUpColor: '#22c55e80',
      wickDownColor: '#ef444480',
    });

    const candles: CandlestickData<Time>[] = chartData.klines.map(k => ({
      time: k.time as Time,
      open: k.open,
      high: k.high,
      low: k.low,
      close: k.close,
    }));

    const series = this.candleSeries!;
    series.setData(candles);

    // Entry price line
    const entryPrice = parseFloat(trade.avg_entry_price);
    series.createPriceLine({
      price: entryPrice,
      color: '#22c55e',
      lineWidth: 1,
      lineStyle: 2,
      axisLabelVisible: true,
      title: 'Entry',
    });

    // Exit price line
    const exitPrice = parseFloat(trade.avg_exit_price);
    series.createPriceLine({
      price: exitPrice,
      color: '#f59e0b',
      lineWidth: 1,
      lineStyle: 2,
      axisLabelVisible: true,
      title: 'Exit',
    });

    // TP line
    if (chartData.take_profit) {
      series.createPriceLine({
        price: chartData.take_profit,
        color: '#3b82f6',
        lineWidth: 1,
        lineStyle: 1,
        axisLabelVisible: true,
        title: 'TP',
      });
    }

    // SL line
    if (chartData.stop_loss) {
      series.createPriceLine({
        price: chartData.stop_loss,
        color: '#ef4444',
        lineWidth: 1,
        lineStyle: 1,
        axisLabelVisible: true,
        title: 'SL',
      });
    }

    // Markers via createSeriesMarkers (v5)
    const entryTimeSec = Math.floor(new Date(trade.entry_time).getTime() / 1000) as Time;
    const exitTimeSec = Math.floor(new Date(trade.exit_time).getTime() / 1000) as Time;
    const isLong = trade.side === 'Sell'; // Sell side = closing long

    const markers = createSeriesMarkers(series, [
      {
        time: entryTimeSec,
        position: isLong ? 'belowBar' : 'aboveBar',
        color: '#22c55e',
        shape: isLong ? 'arrowUp' : 'arrowDown',
        text: 'Entry',
      },
      {
        time: exitTimeSec,
        position: isLong ? 'aboveBar' : 'belowBar',
        color: '#f59e0b',
        shape: isLong ? 'arrowDown' : 'arrowUp',
        text: 'Exit',
      },
    ]);

    this.chart.timeScale().fitContent();

    // Resize observer
    const observer = new ResizeObserver(entries => {
      if (this.chart) {
        const { width } = entries[0].contentRect;
        this.chart.applyOptions({ width });
      }
    });
    observer.observe(container);
  }

  formatDate(): string {
    const d = new Date(this.year, this.month - 1, this.day);
    return d.toLocaleDateString('en-US', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
  }

  formatTime(isoString: string): string {
    const d = new Date(isoString);
    return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  }

  ngOnDestroy(): void {
    if (this.chart) {
      this.chart.remove();
      this.chart = null;
    }
  }
}
