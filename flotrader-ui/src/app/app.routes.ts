import { Routes } from '@angular/router';

export const routes: Routes = [
  {
    path: '',
    loadComponent: () =>
      import('./features/calendar/calendar.component').then(m => m.CalendarComponent),
    title: 'FloTrader - Calendar',
  },
  {
    path: 'day/:year/:month/:day',
    loadComponent: () =>
      import('./features/day-detail/day-detail.component').then(m => m.DayDetailComponent),
    title: 'FloTrader - Day Detail',
  },
  { path: '**', redirectTo: '' },
];
