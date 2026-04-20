import { Component, inject, signal, computed, effect, OnInit } from '@angular/core';
import { Router, ActivatedRoute } from '@angular/router';
import { NgClass } from '@angular/common';
import { ApiService, TimezoneService, WebSocketService } from '../../core/services';
import { CalendarResponse, CalendarDay } from '../../core/models';
import { PnlColorPipe, PnlSignPipe } from '../../shared/pnl-color.pipe';

@Component({
  selector: 'app-calendar',
  standalone: true,
  imports: [NgClass, PnlColorPipe, PnlSignPipe],
  template: `
    <!-- Account Banner -->
    @if (data()) {
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <!-- Balance -->
        <div class="card">
          <div class="card-label">Balance</div>
          <div class="card-value font-mono">{{ formatUsd(data()!.balance) }}</div>
        </div>
        <!-- Month PnL -->
        <div class="card">
          <div class="card-label">{{ data()!.month_data.month_name }} P&L</div>
          <div class="card-value font-mono" [ngClass]="data()!.month_data.month_pnl | pnlColor">
            {{ data()!.month_data.month_pnl | pnlSign }} USDT
          </div>
          <div class="text-xs text-[var(--text-secondary)] mt-1">
            {{ data()!.month_data.month_trades }} trades
          </div>
        </div>
        <!-- Win Rate -->
        <div class="card">
          <div class="card-label">Win Rate</div>
          <div class="card-value font-mono" [ngClass]="winRateClass()">
            {{ winRate() }}%
          </div>
          <div class="text-xs text-[var(--text-secondary)] mt-1">
            {{ winCount() }}W / {{ lossCount() }}L
          </div>
        </div>
        <!-- Unrealized -->
        <div class="card">
          <div class="card-label">Unrealized P&L</div>
          <div class="card-value font-mono" [ngClass]="unrealizedPnl() | pnlColor">
            {{ unrealizedPnl() | pnlSign }} USDT
          </div>
          <div class="text-xs font-mono mt-1" [ngClass]="unrealizedPct() | pnlColor">
            {{ unrealizedPct() | pnlSign }}%
          </div>
        </div>
      </div>

      <!-- Open Positions -->
      @if (livePositions().length > 0) {
        <div class="mb-6">
          <h3 class="text-sm font-medium text-[var(--text-secondary)] mb-3">Open Positions</h3>
          <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            @for (pos of livePositions(); track pos.symbol) {
              <div class="card !p-3 flex items-center justify-between">
                <div>
                  <span class="font-medium text-sm">{{ pos.symbol }}</span>
                  <span class="ml-2 text-xs px-1.5 py-0.5 rounded"
                        [ngClass]="pos.side === 'Buy' ? 'bg-profit/20 text-profit' : 'bg-loss/20 text-loss'">
                    {{ pos.side === 'Buy' ? 'LONG' : 'SHORT' }}
                  </span>
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

      <!-- Month Navigation -->
      <div class="flex items-center justify-between mb-4">
        <button (click)="goMonth(data()!.prev_year, data()!.prev_month)"
                class="nav-btn">&larr; Prev</button>
        <h2 class="text-xl font-semibold">
          {{ data()!.month_data.month_name }} {{ data()!.month_data.year }}
        </h2>
        <button (click)="goMonth(data()!.next_year, data()!.next_month)"
                class="nav-btn">Next &rarr;</button>
      </div>

      <!-- Calendar Grid -->
      <div class="grid grid-cols-7 gap-1">
        <!-- Header -->
        @for (d of weekdays; track d) {
          <div class="text-center text-xs font-medium text-[var(--text-secondary)] py-2">{{ d }}</div>
        }
        <!-- Days -->
        @for (day of data()!.calendar_days; track $index) {
          @if (day.number === '') {
            <div class="h-20"></div>
          } @else {
            <div
              class="h-20 rounded-lg border border-transparent p-2 cursor-pointer transition-all duration-150 hover:border-[var(--border)] hover:bg-surface-700/50"
              [ngClass]="{
                'bg-profit/10 border-profit/30': day.stats && +day.stats.total_pnl > 0,
                'bg-loss/10 border-loss/30': day.stats && +day.stats.total_pnl < 0,
                'ring-1 ring-accent/50': isToday(day)
              }"
              (click)="goDay(day)">
              <div class="text-xs text-[var(--text-secondary)]">{{ day.number }}</div>
              @if (day.stats) {
                <div class="mt-1 font-mono text-sm font-medium"
                     [ngClass]="day.stats.total_pnl | pnlColor">
                  {{ day.stats.total_pnl | pnlSign }}
                </div>
                <div class="text-[10px] text-[var(--text-secondary)] mt-0.5">
                  {{ day.stats.trade_count }}t
                  &middot; {{ day.stats.win_rate }}%
                </div>
              }
            </div>
          }
        }
      </div>
    } @else {
      <!-- Loading -->
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
      font-size: 1.25rem;
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
    }
    .nav-btn:hover {
      background: var(--bg-hover);
    }
  `],
})
export class CalendarComponent implements OnInit {
  private api = inject(ApiService);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private tzService = inject(TimezoneService);
  private ws = inject(WebSocketService);

  data = signal<CalendarResponse | null>(null);

  year = signal(new Date().getFullYear());
  month = signal(new Date().getMonth() + 1);

  weekdays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  // Use WebSocket positions if available, otherwise fall back to API data
  livePositions = computed(() => {
    const wsPositions = this.ws.positions();
    if (wsPositions.length > 0) return wsPositions;
    return this.data()?.open_positions ?? [];
  });

  unrealizedPnl = computed(() => {
    const wsBalance = this.ws.lastUpdate();
    if (wsBalance) {
      const positions = wsBalance.positions;
      const total = positions.reduce((sum, p) => sum + parseFloat(p.unrealized_pnl), 0);
      return total.toFixed(4);
    }
    return this.data()?.total_unrealized ?? '0';
  });

  unrealizedPct = computed(() => {
    const wsBalance = this.ws.lastUpdate();
    if (wsBalance) {
      const positions = wsBalance.positions;
      const total = positions.reduce((sum, p) => sum + parseFloat(p.pnl_pct || '0'), 0);
      return total.toFixed(2);
    }
    return this.data()?.total_unrealized_pct ?? '0';
  });

  winCount = computed(() =>
    (this.data()?.calendar_days ?? []).reduce(
      (sum, d) => sum + (d.stats?.winning_trades ?? 0),
      0,
    ),
  );

  lossCount = computed(() =>
    (this.data()?.calendar_days ?? []).reduce(
      (sum, d) => sum + (d.stats?.losing_trades ?? 0),
      0,
    ),
  );

  winRate = computed(() => {
    const wins = this.winCount();
    const losses = this.lossCount();
    const total = wins + losses;
    if (total === 0) return '—';
    return ((wins / total) * 100).toFixed(1);
  });

  winRateClass = computed(() => {
    const wins = this.winCount();
    const losses = this.lossCount();
    const total = wins + losses;
    if (total === 0) return 'text-[var(--text-secondary)]';
    const rate = (wins / total) * 100;
    if (rate >= 60) return 'text-profit';
    if (rate >= 40) return 'text-[var(--text-primary)]';
    return 'text-loss';
  });

  constructor() {
    // Reload when tz changes
    effect(() => {
      const tz = this.tzService.tz();
      this.loadData();
    });
  }

  ngOnInit(): void {
    const params = this.route.snapshot.queryParams;
    if (params['year']) this.year.set(+params['year']);
    if (params['month']) this.month.set(+params['month']);
  }

  loadData(): void {
    this.api.getCalendar(this.year(), this.month(), this.tzService.tz()).subscribe(data => {
      this.data.set(data);
    });
  }

  goMonth(year: number, month: number): void {
    this.year.set(year);
    this.month.set(month);
    this.router.navigate([], { queryParams: { year, month } });
    this.loadData();
  }

  goDay(day: CalendarDay): void {
    if (day.number === '' || !day.stats) return;
    this.router.navigate(['/day', this.year(), this.month(), day.number]);
  }

  isToday(day: CalendarDay): boolean {
    const today = this.data()?.today;
    if (!today || day.number === '') return false;
    const t = new Date(today);
    return t.getFullYear() === this.year() && t.getMonth() + 1 === this.month() && t.getDate() === +day.number;
  }

  formatUsd(value: string): string {
    return parseFloat(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' USDT';
  }
}
