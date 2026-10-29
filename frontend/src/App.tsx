import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError, fmtPrice, fmtTime, TIMEFRAMES, type ChartData, type ZoneKey } from './api';
import Chart, { type Toggles } from './Chart';
import { candleHash } from './hash';
import PlanScreen from './PlanScreen';
import BacktestScreen from './BacktestScreen';
import { AssessmentPanel, Provenance, SignalPanel } from './Panels';

const REFRESH_MS = 30_000;

export default function App() {
  const [symbol, setSymbol] = useState('AIOK');
  const [timeframe, setTimeframe] = useState('5Min');
  const [sessions, setSessions] = useState<'regular' | 'extended'>('regular');
  const [zone, setZone] = useState<ZoneKey>('ny');
  const [screen, setScreen] = useState<'chart' | 'plan' | 'backtest'>('chart');
  const [data, setData] = useState<ChartData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [hashOk, setHashOk] = useState<boolean | null>(null);
  const [replayAt, setReplayAt] = useState<number | null>(null);
  const [toggles, setToggles] = useState<Toggles>({ ema: true, vwap: true, swings: true, signals: true, rsi: true, macd: false });
  const [health, setHealth] = useState<{ credentials_configured: boolean; feed: string } | null>(null);

  useEffect(() => { api<{ credentials_configured: boolean; feed: string }>('/api/health').then(setHealth).catch(e => setError(e.message)); }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ symbol, timeframe, sessions });
      const path = replayAt ? `/api/replay?${params}&until=${replayAt}` : `/api/candles?${params}`;
      const next = await api<ChartData>(path);
      setData(next);
      setError(null);
      setHashOk((await candleHash(next.candles)) === next.data_hash);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [symbol, timeframe, sessions, replayAt]);

  useEffect(() => { load(); }, [load]);
  // Polling doubles as reconnect: each request re-fetches the recent overlap window.
  useEffect(() => { if (replayAt) return; const id = window.setInterval(load, REFRESH_MS); return () => window.clearInterval(id); }, [load, replayAt]);

  const replayOptions = useMemo(() => (data && !replayAt ? data.candles.filter(c => c.complete).map(c => c.time) : []), [data, replayAt]);

  return (
    <div className="app">
      <Sidebar symbol={symbol} onPick={s => { setSymbol(s); setReplayAt(null); }} />
      <main>
        <header className="toolbar">
          <h1>{symbol}</h1>
          <nav className="tabs" aria-label="Screens">
            {(['chart', 'plan', 'backtest'] as const).map(s => <button key={s} aria-pressed={screen === s} onClick={() => setScreen(s)}>{{ chart: 'Chart & Signals', plan: 'Entry, Exit & Price Projections', backtest: 'Replay & Backtest' }[s]}</button>)}
          </nav>
          <label>Timeframe <select value={timeframe} onChange={e => { setTimeframe(e.target.value); setReplayAt(null); }}>{TIMEFRAMES.map(t => <option key={t}>{t}</option>)}</select></label>
          <label>Sessions <select value={sessions} onChange={e => setSessions(e.target.value as 'regular' | 'extended')}><option value="regular">Regular only</option><option value="extended">Include extended hours</option></select></label>
          <label>Time zone <select value={zone} onChange={e => setZone(e.target.value as ZoneKey)}><option value="ny">New York</option><option value="manila">Manila</option></select></label>
          <button onClick={load} disabled={loading}>{loading ? 'Loading…' : 'Refresh'}</button>
        </header>
        {health && !health.credentials_configured && <p className="banner error">Alpaca credentials are not configured on the backend. Add ALPACA_API_KEY and ALPACA_API_SECRET to backend/.env and restart. No data will be shown until then.</p>}
        {error && <p className="banner error" role="alert">{error}</p>}
        {data?.error && <p className="banner error" role="alert">Data request failed: {data.error}{data.candles.length ? ' Showing cached candles from the last successful fetch.' : ''}</p>}
        {data && <Provenance data={data} zone={zone} hashOk={hashOk} />}
        {data && data.candles.length === 0 && <p className="banner warn">No candles returned for {symbol} on the {data.feed.toUpperCase()} feed in this range. Nothing has been substituted.</p>}
        {data && data.candles.length > 0 && screen !== 'backtest' && (
          <>
            <fieldset className="toggles"><legend>Indicators</legend>
              {(Object.keys(toggles) as (keyof Toggles)[]).map(k => <label key={k}><input type="checkbox" checked={toggles[k]} onChange={e => setToggles({ ...toggles, [k]: e.target.checked })} /> {{ ema: `EMA ${data.config.ema_fast}/${data.config.ema_slow}`, vwap: 'Session VWAP', swings: 'Confirmed swings', signals: 'Signals', rsi: `RSI ${data.config.rsi_period}`, macd: 'MACD' }[k]}</label>)}
            </fieldset>
            {screen === 'chart' && <Chart data={data} zone={zone} toggles={toggles} />}
          </>
        )}
        {data && screen === 'chart' && data.candles.length > 0 && (
          <div className="grid">
            <AssessmentPanel data={data} zone={zone} />
            <SignalPanel data={data} zone={zone} />
          </div>
        )}
        {data && screen === 'plan' && <PlanScreen data={data} zone={zone} toggles={toggles} />}
        {screen === 'backtest' && (
          <BacktestScreen symbol={symbol} timeframe={timeframe} sessions={sessions} zone={zone} replayAt={replayAt} replayOptions={replayOptions} onReplay={setReplayAt} data={data} toggles={toggles} />
        )}
        {data && <p className="footnote">ATR {fmtPrice(data.assessment.levels.atr)} · Analysis and alerts only. This app does not place orders. Gaps and slippage can cause losses beyond a planned stop.</p>}
      </main>
    </div>
  );
}

function Sidebar({ symbol, onPick }: { symbol: string; onPick: (s: string) => void }) {
  const [watch, setWatch] = useState<string[]>([]);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<{ symbol: string; name: string; exchange: string }[]>([]);
  const [searchError, setSearchError] = useState<string | null>(null);
  useEffect(() => { api<{ symbols: string[] }>('/api/watchlist').then(r => setWatch(r.symbols)).catch(() => undefined); }, []);
  const search = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!query.trim()) return;
    try {
      const r = await api<{ results: { symbol: string; name: string; exchange: string }[] }>(`/api/symbols?q=${encodeURIComponent(query)}`);
      setResults(r.results); setSearchError(r.results.length ? null : 'No matching active US equities.');
    } catch (e) { setResults([]); setSearchError((e as Error).message); }
  };
  const toggle = async (s: string) => {
    const r = await api<{ symbols: string[] }>(`/api/watchlist/${s}`, { method: watch.includes(s) ? 'DELETE' : 'POST' });
    setWatch(r.symbols);
  };
  return (
    <aside className="sidebar">
      <form onSubmit={search}>
        <label htmlFor="sym">Symbol search</label>
        <input id="sym" value={query} onChange={e => setQuery(e.target.value)} placeholder="Ticker or name" />
        <button type="submit">Search</button>
      </form>
      {searchError && <p className="small error">{searchError}</p>}
      <ul className="results">{results.map(r => <li key={r.symbol}><button onClick={() => onPick(r.symbol)}>{r.symbol}</button> <span className="small">{r.name} · {r.exchange}</span></li>)}</ul>
      <h2>Watchlist</h2>
      <button className="small-btn" onClick={() => toggle(symbol)}>{watch.includes(symbol) ? `Remove ${symbol}` : `Add ${symbol}`}</button>
      <ul className="watch">{watch.map(s => <li key={s}><button aria-current={s === symbol} onClick={() => onPick(s)}>{s}</button></li>)}</ul>
      <p className="small">Watchlist entries are symbols only. No positions are stored or assumed.</p>
      <p className="small muted">Times: {fmtTime(Math.floor(Date.now() / 1000), 'ny')}</p>
    </aside>
  );
}
