import { Injectable, signal, computed, OnDestroy } from '@angular/core';
import { WsPositionUpdate, OpenPosition } from '../models';

@Injectable({ providedIn: 'root' })
export class WebSocketService implements OnDestroy {
  private ws: WebSocket | null = null;
  private reconnectTimer: any = null;

  readonly status = signal<'connecting' | 'connected' | 'disconnected'>('disconnected');
  readonly lastUpdate = signal<WsPositionUpdate | null>(null);

  readonly positions = computed(() => this.lastUpdate()?.positions ?? []);
  readonly balance = computed(() => this.lastUpdate()?.balance ?? null);

  connect(): void {
    if (this.ws?.readyState === WebSocket.OPEN) return;

    this.status.set('connecting');
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${location.host}/ws/positions`;

    this.ws = new WebSocket(wsUrl);

    this.ws.onopen = () => {
      this.status.set('connected');
    };

    this.ws.onmessage = (event) => {
      const data: WsPositionUpdate = JSON.parse(event.data);
      this.lastUpdate.set(data);
    };

    this.ws.onclose = () => {
      this.status.set('disconnected');
      this.scheduleReconnect();
    };

    this.ws.onerror = () => {
      this.ws?.close();
    };
  }

  disconnect(): void {
    clearTimeout(this.reconnectTimer);
    this.ws?.close();
    this.ws = null;
    this.status.set('disconnected');
  }

  private scheduleReconnect(): void {
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => this.connect(), 3000);
  }

  ngOnDestroy(): void {
    this.disconnect();
  }
}
