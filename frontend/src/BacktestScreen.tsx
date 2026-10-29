import { useState } from 'react';
import { api, fmtMoney, fmtTime, type ChartData, type ZoneKey } from './api';
import Chart, { type Toggles } from './Chart';
import { AssessmentPanel } from './Panels';

type Summary = { trades: number; wins?: number; win_rate?: number; net_pnl?: number; fees?: number; avg_r?: number; profit_factor?: number | null; max_drawdown?: number; losses_beyond_planned_risk?: number; premature_exits?: number; stopped_then_recovered?: number };
type Trade = { signal_time: number; entry_time: number; entry_price: number; shares: number; exit_reason: string; net_pnl: number; r_multiple: number; sample: string; premature: { further_favorable_r?: number; stopped_then_recovered?: boolean } };
type Result = { bars: number; split_time: number | null; trades: Trade[]; skipped: { time: number; reason: string }[]; summary: { all: Summary; in_sample: Summary; out_of_sample: Summary }; notes: string[] };

const DEFAULTS = { side: 'long', exit_mode: 'full', stop_mode: 'swing', stop_atr: '1', target_r: '2', partial_r: '1', partial_fraction: '0.5', trail_atr: '2', risk_per_trade: '100', starting_capital: '10000', fee_per_share: '0', fee_per_order: '0', slippage_bps: '5', reentry_cooldown_bars: '3', out_of_sample_fraction: '0.3' };

export default function BacktestScreen(props: { symbol: string; timeframe: string; sessions: string; zone: ZoneKey; replayAt: number | null; replayOptions: number[]; onReplay: (t: number | null) => void; data: ChartData | null; toggles: Toggles }) {
  const { symbol, timeframe, sessions, zone, replayAt, replayOptions, onReplay, data, toggles } = props;
  const [cfg, setCfg] = useState<Record<string, string>>(DEFAULTS);
  const [results, setResults] = useState<Record<string, Result> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [options, setOptions] = useState<number[]>([]);
  const run = async () => {
    setRunning(true); setError(null);
    try {
      const config = Object.fromEntries(Object.entries(cfg).map(([k, v]) => [k, ['side', 'exit_mode', 'stop_mode'].includes(k) ? v : Number(v)]));
      const r = await api<{ results: Record<string, Result> }>('/api/backtest', { method: 'POST', body: JSON.stringify({ symbol, timeframe, sessions, config }) });
      setResults(r.results);
    } catch (e) { setError((e as Error).message); } finally { setRunning(false); }
  };
  const field = (key: string, label: string, choices?: string[]) => (
    <label key={key}>{label}{choices
      ? <select value={cfg[key]} onChange={e => setCfg({ ...cfg, [key]: e.target.value })}>{choices.map(c => <option key={c}>{c}</option>)}</select>
      : <input value={cfg[key]} onChange={e => setCfg({ ...cfg, [key]: e.target.value })} inputMode="decimal" />}</label>
  );
  const list = replayAt ? options : replayOptions;
  return (
    <div>
      <section className="panel">
        <h2>Historical replay</h2>
        <p className="small">Replay recomputes every indicator, swing, signal, and assessment from candles up to the chosen time only.</p>
        {list.length > 0 ? (
          <div className="actions">
            <input type="range" min={0} max={list.length - 1} value={replayAt ? Math.max(0, list.indexOf(replayAt)) : list.length - 1} onChange={e => { if (!replayAt) setOptions(replayOptions); onReplay(list[Number(e.target.value)]); }} aria-label="Replay position" style={{ flex: 1 }} />
            <span>{replayAt ? fmtTime(replayAt, zone) : 'Live'}</span>
            {replayAt && <button onClick={() => onReplay(null)}>Back to live</button>}
          </div>
        ) : <p className="muted">Load candles first.</p>}
      </section>
      {data && replayAt && data.candles.length > 0 && <><Chart data={data} zone={zone} toggles={toggles} /><AssessmentPanel data={data} zone={zone} /></>}
      <section className="panel">
        <h2>Backtest entry variants</h2>
        <p className="small">Early, candle-confirmed, and retest entries are tested independently with the same exits, costs, and data-quality checks. Signals fill at the next candle's open.</p>
        <div className="form-grid">
          {field('side', 'Side', ['long', 'short'])}
          {field('exit_mode', 'Exit rule', ['full', 'partial', 'trailing'])}
          {field('stop_mode', 'Initial stop', ['swing', 'atr'])}
          {field('stop_atr', 'ATR stop multiple')}
          {field('target_r', 'Target (R)')}
          {field('partial_r', 'Partial at (R)')}
          {field('partial_fraction', 'Partial fraction')}
          {field('trail_atr', 'Trailing ATR multiple')}
          {field('risk_per_trade', 'Risk per trade ($)')}
          {field('starting_capital', 'Starting capital ($)')}
          {field('fee_per_share', 'Fee per share ($)')}
          {field('fee_per_order', 'Fee per order ($)')}
          {field('slippage_bps', 'Slippage (bps)')}
          {field('reentry_cooldown_bars', 'Re-entry cooldown (candles)')}
          {field('out_of_sample_fraction', 'Out-of-sample fraction')}
        </div>
        <div className="actions"><button className="primary" onClick={run} disabled={running}>{running ? 'Running…' : `Backtest ${symbol} ${timeframe}`}</button></div>
        {error && <p className="banner error" role="alert">{error}</p>}
      </section>
      {results && (
        <section className="panel">
          <h2>Results</h2>
          <p className="small">{Object.values(results)[0]?.notes.join(' ')} Out-of-sample begins at {fmtTime(Object.values(results)[0]?.split_time, zone)}. Rules are fixed before the split and are not tuned on it.</p>
          <table className="signals"><thead><tr><th>Variant</th><th>Sample</th><th>Trades</th><th>Win rate</th><th>Net P/L</th><th>Avg R</th><th>Profit factor</th><th>Max drawdown</th><th>Beyond planned risk</th><th>Premature exits</th><th>Stopped then recovered</th></tr></thead><tbody>
            {Object.entries(results).flatMap(([variant, r]) => (['in_sample', 'out_of_sample', 'all'] as const).map(k => {
              const s = r.summary[k];
              return <tr key={variant + k}><td>{variant}</td><td>{k.replace(/_/g, ' ')}</td><td>{s.trades}</td><td>{s.win_rate === undefined ? 'n/a' : `${(s.win_rate * 100).toFixed(0)}%`}</td><td>{fmtMoney(s.net_pnl)}</td><td>{s.avg_r?.toFixed(2) ?? 'n/a'}</td><td>{s.profit_factor?.toFixed(2) ?? 'n/a'}</td><td>{fmtMoney(s.max_drawdown)}</td><td>{s.losses_beyond_planned_risk ?? 'n/a'}</td><td>{s.premature_exits ?? 'n/a'}</td><td>{s.stopped_then_recovered ?? 'n/a'}</td></tr>;
            }))}
          </tbody></table>
          <p className="small">Small samples are not evidence of an edge. Win rates here describe this data only and are not probabilities of future outcomes.</p>
          {Object.entries(results).map(([variant, r]) => (
            <details key={variant}><summary>{variant}: {r.trades.length} trades, {r.skipped.length} skipped by data checks</summary>
              <table className="signals"><thead><tr><th>Signal</th><th>Entry</th><th>Shares</th><th>Exit</th><th>Net P/L</th><th>R</th><th>Sample</th></tr></thead><tbody>
                {r.trades.map(t => <tr key={t.signal_time}><td>{fmtTime(t.signal_time, zone)}</td><td>{t.entry_price.toFixed(4)} at {fmtTime(t.entry_time, zone)}</td><td>{t.shares}</td><td>{t.exit_reason}{t.premature.stopped_then_recovered ? ' (recovered later)' : ''}</td><td>{fmtMoney(t.net_pnl)}</td><td>{t.r_multiple.toFixed(2)}</td><td>{t.sample}</td></tr>)}
              </tbody></table>
            </details>
          ))}
        </section>
      )}
    </div>
  );
}
