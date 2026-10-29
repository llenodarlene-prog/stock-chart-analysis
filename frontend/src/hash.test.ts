import { describe, expect, it } from 'vitest';
import { candleHash } from './hash';

// The expected value is produced by backend sessions.data_hash() for the same candles
// (see backend/tests/test_api.py). Matching proves both sides use one canonical form.
const candles = [
  { time: 1761485400, end: 1761485700, open: 13.5, high: 13.62, low: 13.41, close: 13.6, volume: 1200, session: 'regular', session_date: '2026-10-26', slot: 0, minutes: 5, complete: true },
  { time: 1761485700, end: 1761486000, open: 13.6, high: 13.7, low: 13.55, close: 13.65, volume: 800, session: 'regular', session_date: '2026-10-26', slot: 1, minutes: 3, complete: false }
];

describe('candleHash', () => {
  it('matches the backend hash', async () => {
    expect(await candleHash(candles)).toBe('c840bb3d3807615289352961e73c785e0460a4ae53167c260dd1ed9d3f9a7593');
  });
});
