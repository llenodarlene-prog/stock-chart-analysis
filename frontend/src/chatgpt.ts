import { fmtPrice, fmtTime, type ChartData, type ZoneKey } from './api';

// Builds a self-contained prompt from the app's recorded data, so ChatGPT
// explains these exact numbers instead of guessing its own.
export function chatgptPrompt(data: ChartData, zone: ZoneKey, extra?: string): string {
  const a = data.assessment;
  const n = a.next_steps;
  const L = a.levels;
  const ind = data.indicators;
  const last = data.candles.filter(c => c.complete).length - 1;
  const val = (k: string, d = 2) => (last >= 0 && ind[k]?.[last] != null ? (ind[k][last] as number).toFixed(d) : 'n/a');
  const signals = data.signals.slice(-5).map(s => `- ${fmtTime(s.time, zone)}: ${s.kind.replace(/_/g, ' ')} (${s.variant}, ${s.direction}) at ${fmtPrice(s.level)}. ${s.explanation}`).join('\n') || '- none in range';
  return [
    'You are helping me understand my stock chart app. Use ONLY the data below. Explain in simple, beginner-friendly language.',
    'Rules: do not invent price levels or probabilities, do not predict prices, and do not tell me to buy or sell. Explain what the app\'s conditions mean and what to watch. I make my own decisions.',
    '',
    `Symbol: ${data.symbol} | Timeframe: ${data.timeframe} | Sessions: ${data.sessions} | Feed: ${data.feed.toUpperCase()} (IEX only)`,
    `Data as of the completed candle at ${fmtTime(data.provenance.last_completed_time, zone)}. Last close: ${fmtPrice(L.close)}`,
    `Data usable for signals: ${data.gates.actionable ? 'yes' : 'no: ' + data.gates.blocked_reasons.join(' ')}`,
    '',
    `App assessment: ${a.label}. ${a.reason}`,
    n ? `App summary: ${n.headline}\nApp next steps:\n${n.steps.map(s => `- ${s}`).join('\n')}` : '',
    '',
    `Levels: resistance ${fmtPrice(L.resistance)}, support ${fmtPrice(L.support)}, breakout buffer ${fmtPrice(L.buffer)}, ATR ${fmtPrice(L.atr)}`,
    `Indicators: EMA ${data.config.ema_fast} ${val('ema_fast')}, EMA ${data.config.ema_slow} ${val('ema_slow')}, RSI ${val('rsi', 1)}, MACD histogram ${val('macd_hist', 4)}, VWAP ${val('vwap')}, relative volume ${val('rvol')}x`,
    `Evidence:\n${a.evidence.map(e => `- [${e.category}, ${e.direction}] ${e.text}`).join('\n')}`,
    `Recent signals:\n${signals}`,
    extra ? `\nMy plan:\n${extra}` : '',
    '',
    'My question: ',
  ].filter(line => line !== undefined).join('\n');
}

export async function copyText(text: string): Promise<boolean> {
  try { await navigator.clipboard.writeText(text); return true; } catch { return false; }
}
