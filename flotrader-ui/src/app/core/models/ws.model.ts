import { OpenPosition } from './position.model';

export interface WsPositionUpdate {
  type: 'update';
  balance: {
    total_equity: string;
    available: string;
  };
  positions: OpenPosition[];
}
