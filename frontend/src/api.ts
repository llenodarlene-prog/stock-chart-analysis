export type Candle = { time: number; end: number; open: number; high: number; low: number; close: number; volume: number; session: string; session_date: string; slot: number; minutes: number; complete: boolean };
export type Series = (number | null)[];
export type Evidence = { category: string; direction: string; text: string; value?: number | null };
export type Signal = { kind: string; variant: string; direction: string; time: number; index: number; level: number; rule: string; explanation: string; confirmation: string; confirmation_note: string; actionable: boolean; blocked_reasons: string[]; comparability_caveat: boolean; evidence: Record<string, unknown> };
export type Levels = { resistance: number | null; support: number | null; buffer: number; atr: number | null; close: number; swing_highs_above: number[]; swing_lows_below: number[]; measured_move_up: number | null; measured_move_down: number | null };
export type Scenario = { name: string; trigger: string; targets: { price: number | null; source: string }[]; invalidation: string; supporting_now: string[]; opposing_now: string[]; actionable: boolean };
export type Assessment = { label: string; reason: string; as_of: number | null; trend?: { direction: string; reason: string }; evidence: Evidence[]; supporting?: Evidence[]; opposing?: Evidence[]; warnings?: Evidence[]; levels: Levels; scenarios: Scenario[]; actionable?: boolean; blocked_reasons?: string[]; disclaimer?: string };
export type Swing = { kind: string; index: number; confirmed_index: number; price: number; time: number; confirmed_time: number };
export type ChartData = {
  symbol: string; timeframe: string; sessions: string; feed: string; feed_label: string; adjustment: string; volume_note: string;
  provenance: { now: number; replay_until: number | null; fetched_at: number | null; fetch_age_seconds: number | null; market_state: string; last_candle_time: number | null; last_completed_time: number | null; last_completed_end: number | null; freshness_seconds: number | null; rate_limit: Record<string, string> | null; late_corrections_this_fetch: number; recent_corrections: { t: number; old: string; new: string; seen_at: number }[]; session_rules: string };
  error: string | null; corporate_action_error: string | null; missing: { expected_buckets: number; present: number };
  candles: Candle[]; indicators: Record<string, Series>; swings: Swing[]; signals: Signal[]; assessment: Assessment;
  gates: { actionable: boolean; blocked_reasons: string[]; notes: string[] }; corporate_actions: { type: string; date: string; message: string }[];
  data_hash: string; config: Record<string, number>;
};

export class ApiError extends Error {}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...init });
  } catch {
    throw new ApiError('Cannot reach the local backend at /api. Is uvicorn running on port 8000?');
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(body?.detail ? String(body.detail) : `Backend returned HTTP ${response.status}`);
  return body as T;
}

export const TIMEFRAMES = ['1Min', '5Min', '15Min', '1Hour', '1Day'];
export const ZONES = { ny: 'America/New_York', manila: 'Asia/Manila' } as const;
export type ZoneKey = keyof typeof ZONES;

export function fmtTime(ts: number | null | undefined, zone: ZoneKey): string {
  if (!ts) return 'n/a';
  return new Intl.DateTimeFormat('en-US', { timeZone: ZONES[zone], year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short' }).format(new Date(ts * 1000));
}
export const fmtPrice = (n: number | null | undefined, digits = 4) => (n === null || n === undefined || Number.isNaN(n) ? 'n/a' : n.toFixed(digits));
export const fmtMoney = (n: number | null | undefined) => (n === null || n === undefined ? 'n/a' : n.toLocaleString('en-US', { style: 'currency', currency: 'USD' }));
