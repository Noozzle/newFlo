import { Injectable, signal, computed } from '@angular/core';

@Injectable({ providedIn: 'root' })
export class TimezoneService {
  readonly tz = signal<'utc' | 'local'>(this.loadTz());

  readonly label = computed(() => this.tz() === 'utc' ? 'UTC' : 'Local');

  toggle(): void {
    const next = this.tz() === 'utc' ? 'local' : 'utc';
    this.tz.set(next);
    localStorage.setItem('flotrader_tz', next);
  }

  private loadTz(): 'utc' | 'local' {
    const saved = localStorage.getItem('flotrader_tz');
    return saved === 'local' ? 'local' : 'utc';
  }
}
