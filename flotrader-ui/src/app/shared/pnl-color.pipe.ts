import { Pipe, PipeTransform } from '@angular/core';

@Pipe({ name: 'pnlColor', standalone: true })
export class PnlColorPipe implements PipeTransform {
  transform(value: string | number): string {
    const n = typeof value === 'string' ? parseFloat(value) : value;
    if (n > 0) return 'text-profit';
    if (n < 0) return 'text-loss';
    return 'text-[var(--text-secondary)]';
  }
}

@Pipe({ name: 'pnlSign', standalone: true })
export class PnlSignPipe implements PipeTransform {
  transform(value: string | number, decimals: number = 2): string {
    const n = typeof value === 'string' ? parseFloat(value) : value;
    const prefix = n > 0 ? '+' : '';
    return `${prefix}${n.toFixed(decimals)}`;
  }
}
