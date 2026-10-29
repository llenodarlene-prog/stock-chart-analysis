import type { Candle } from './api';

// Same canonical form as backend sessions.data_hash(). If the hashes match,
// the chart is drawing exactly the candles the calculations used.
export function canonical(candles: Candle[]): string {
  return JSON.stringify(candles.map(c => [c.time, c.open.toFixed(6), c.high.toFixed(6), c.low.toFixed(6), c.close.toFixed(6), c.volume, c.complete]));
}

export async function candleHash(candles: Candle[]): Promise<string> {
  const bytes = new TextEncoder().encode(canonical(candles));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}
