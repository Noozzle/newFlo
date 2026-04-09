import { Component, inject, OnInit } from '@angular/core';
import { RouterOutlet, RouterLink } from '@angular/router';
import { TimezoneService } from './core/services';
import { WebSocketService } from './core/services';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet, RouterLink],
  template: `
    <div class="min-h-screen bg-surface-900">
      <!-- Navbar -->
      <nav class="border-b border-[var(--border)] bg-surface-800/80 backdrop-blur-sm sticky top-0 z-50">
        <div class="max-w-7xl mx-auto px-4 h-14 flex items-center justify-between">
          <a routerLink="/" class="flex items-center gap-2 no-underline">
            <span class="text-accent font-bold text-lg tracking-tight">FloTrader</span>
          </a>

          <div class="flex items-center gap-4">
            <!-- WS Status -->
            <div class="flex items-center gap-1.5 text-xs">
              @if (ws.status() === 'connected') {
                <span class="w-2 h-2 rounded-full bg-profit animate-pulse"></span>
                <span class="text-profit">LIVE</span>
              } @else if (ws.status() === 'connecting') {
                <span class="w-2 h-2 rounded-full bg-accent animate-pulse"></span>
                <span class="text-accent">CONNECTING</span>
              } @else {
                <span class="w-2 h-2 rounded-full bg-loss"></span>
                <span class="text-loss">OFFLINE</span>
              }
            </div>

            <!-- TZ Toggle -->
            <button
              (click)="tzService.toggle()"
              class="px-3 py-1.5 rounded-md bg-surface-700 hover:bg-surface-600 text-xs font-medium text-[var(--text-secondary)] transition-colors">
              {{ tzService.label() }}
            </button>
          </div>
        </div>
      </nav>

      <!-- Content -->
      <main class="max-w-7xl mx-auto px-4 py-6">
        <router-outlet />
      </main>
    </div>
  `,
  styles: [`
    :host { display: block; }
  `],
})
export class AppComponent implements OnInit {
  tzService = inject(TimezoneService);
  ws = inject(WebSocketService);

  ngOnInit() {
    this.ws.connect();
  }
}
