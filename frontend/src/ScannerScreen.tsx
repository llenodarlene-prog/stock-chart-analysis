import { useEffect, useRef, useState } from 'react';
import { CandlestickSeries, ColorType, LineSeries, LineStyle, createChart, type UTCTimestamp } from 'lightweight-charts';
import { api, fmtPrice, fmtTime, type Candle, type ZoneKey } from './api';

type Plan = { horizon: string; entry_trigger: string; invalidation: string; stop: number; trigger: number; risk_per_share: number; targets: { price: number; source: string }[]; time_stop: string; gap_note?: string };
type Candidate = {
  symbol: string; name: string; setup: string; quiet: boolean; date: string; close: number; atr: number; rsi: number | null; rvol: number | null; extension_atr: number; day_gain_pct: number; trend: string;
  evidence: string[]; optional: Record<string, boolean>; rank: number; rank_note: string; plans: { overnight: Plan; swing: Plan };
  liquidity: { iex_avg_dollar_volume: number; spread_pct: number | null; quote_time: string | null; ok: boolean; issues: string[]; note: string };
  catalyst: { status: string; note: string; articles: { id: number; headline: string; source: string; created_at: string; url: string | null }[] };
  data_quality: { ok: boolean; last_session: string; history_days: number; issues: string[] };
  account_availability: { available: number | boolean | null; confirmed_at?: number; note: string };
  reassessment?: { at: number; status: string; notes: string[] };
};
type Scan = { id?: number; created_at: number; last_session: string; feed: string; universe: { source_url: string; source_last_modified: string | null; fetched_at: number; symbols: number; scanned: number }; candidates: Candidate[]; qualified_count: number; held_back_for_liquidity: number; rejected: Record<string, number>; errors: string[]; notes: string[]; reassessed_at?: number; message?: string };
type Universe = { count: number; source_url: string | null; source_last_modified: string | null; fetched_at: number | null; confirmed_available: number; confirmed_unavailable: number; note: string };
type Summary = { trades: number; win_rate?: number; avg_r?: number | null; avg_return_pct?: number; worst_r?: number | null; gap_exits?: number };
type Backtest = { symbols: number; split_date: string | null; results: Record<string, Record<string, { in_sample: Summary; out_of_sample: Summary }>>; notes: string[]; errors: string[] };

type Job = { state: string; phase?: string; done?: number; total?: number; error?: string; scan_id?: number };
const SETTING_FIELDS: [string, string][] = [['min_price', 'Minimum price ($)'], ['min_iex_dollar_volume', 'Minimum 20-day IEX dollar volume ($)'], ['max_spread_pct', 'Maximum spread (%)'], ['near_resistance_atr', 'Breakout: max distance below resistance (ATR)'], ['momentum_min_rvol', 'Momentum: minimum relative volume'], ['pullback_min_atr', 'Pullback: minimum depth (ATR)'], ['max_candidates', 'Maximum candidates'], ['max_per_setup', 'Maximum per setup'], ['news_days', 'News lookback (days)'], ['swing_max_hold_days', 'Swing: maximum hold (days)'], ['slippage_bps', 'Slippage (basis points)']];
const SETTING_DEFAULTS: Record<string, string> = { min_price: '1', min_iex_dollar_volume: '100000', max_spread_pct: '1', near_resistance_atr: '0.75', momentum_min_rvol: '1.2', pullback_min_atr: '1', max_candidates: '10', max_per_setup: '4', news_days: '3', swing_max_hold_days: '10', slippage_bps: '10' };
const SETUP_NAMES: Record<string, string> = { developing_breakout: 'Developing breakout', momentum_continuation: 'Momentum continuation', uptrend_pullback: 'Uptrend pullback' };

export default function ScannerScreen({ zone }: { zone: ZoneKey }) {
  const [universe, setUniverse] = useState<Universe | null>(null);
  const [scan, setScan] = useState<Scan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState('');
  const [symbols, setSymbols] = useState('');
  const [bt, setBt] = useState<Backtest | null>(null);
  const loadUniverse = () => api<Universe>('/api/universe').then(setUniverse).catch(e => setError(e.message));
  useEffect(() => { loadUniverse(); api<Scan>('/api/scanner/latest').then(s => setScan(s.candidates ? s : null)).catch(() => undefined); }, []);

  const act = async (label: string, fn: () => Promise<void>) => {
    setBusy(label); setError(null);
    try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(''); }
  };
  const list = () => symbols.split(/[\s,]+/).map(s => s.trim().toUpperCase()).filter(Boolean);
  const [settings, setSettings] = useState<Record<string, string>>(() => { try { return { ...SETTING_DEFAULTS, ...JSON.parse(localStorage.getItem('scanSettings') || '{}') }; } catch { return { ...SETTING_DEFAULTS }; } });
  const [job, setJob] = useState<Job | null>(null);
  const config = () => Object.fromEntries(Object.entries(settings).map(([k, v]) => [k, Number(v)]));
  const saveSetting = (k: string, v: string) => { const next = { ...settings, [k]: v }; setSettings(next); try { localStorage.setItem('scanSettings', JSON.stringify(next)); } catch { /* storage unavailable */ } };
  // Scans run in the background on the server; poll for progress.
  useEffect(() => {
    let timer: number | undefined;
    const poll = async () => {
      try {
        const j = await api<Job>('/api/scanner/job');
        setJob(j);
        if (j.state === 'running') timer = window.setTimeout(poll, 2000);
        else if (j.state === 'done') setScan(await api<Scan>('/api/scanner/latest'));
        else if (j.state === 'error') setError(`Scan failed: ${j.error}`);
      } catch (e) { setError((e as Error).message); }
    };
    if (job?.state === 'running') timer = window.setTimeout(poll, 2000);
    return () => window.clearTimeout(timer);
  }, [job?.state, job?.done]);
  useEffect(() => { api<Job>('/api/scanner/job').then(setJob).catch(() => undefined); }, []);

  return (
    <div>
      <section className="panel">
        <h2>Next Trading Day Watchlist</h2>
        <p className="small">Universe: the official GoTrade US stock directory. Confirm availability in your own GoTrade account; Alpaca tradability is not used. Rank is an ordering, not a probability, and no move size is promised. Analysis only: no trades are placed.</p>
        {universe && (
          <p>
            <b>{universe.count}</b> symbols from <a href={universe.source_url ?? '#'} target="_blank" rel="noreferrer">{universe.source_url ?? 'not loaded'}</a>
            {universe.source_last_modified && <> · source last modified {universe.source_last_modified}</>}
            {universe.fetched_at && <> · fetched {fmtTime(Math.floor(universe.fetched_at), zone)}</>}
            {' '}· confirmed available by you: {universe.confirmed_available}, not available: {universe.confirmed_unavailable}
          </p>
        )}
        <div className="actions">
          <button onClick={() => act('universe', async () => { await api('/api/universe/refresh', { method: 'POST' }); await loadUniverse(); })} disabled={!!busy}>{busy === 'universe' ? 'Refreshing…' : 'Refresh GoTrade directory'}</button>
          <input value={symbols} onChange={e => setSymbols(e.target.value)} placeholder="Optional: limit to symbols, e.g. AAPL, MSFT" style={{ maxWidth: 320 }} aria-label="Limit scan to symbols" />
          <button className="primary" disabled={!!busy || !universe?.count} onClick={() => act('scan', async () => { setJob(await api<Job>('/api/scanner/run', { method: 'POST', body: JSON.stringify({ symbols: list(), config: config() }) })); })} hidden={job?.state === 'running'}>Run scan</button>
          {scan?.id && <button disabled={!!busy} onClick={() => act('reassess', async () => { setScan(await api<Scan>(`/api/scanner/${scan.id}/reassess`, { method: 'POST' })); })}>{busy === 'reassess' ? 'Checking…' : 'Reassess after news and opening gaps'}</button>}
        </div>
        {job?.state === 'running' && (
          <div aria-live="polite">
            <p className="small">{job.phase}: {job.done ?? 0} of {job.total ?? '?'}. The first full scan downloads history for every stock and can take several minutes. You can keep using the other tabs.</p>
            <div className="progress"><div style={{ width: `${job.total ? Math.min(100, ((job.done ?? 0) / job.total) * 100) : 5}%` }} /></div>
          </div>
        )}
        {error && <p className="banner error" role="alert">{error}</p>}
        <details className="techdetails"><summary>Scanner settings</summary>
          <div className="form-grid">
            {SETTING_FIELDS.map(([k, label]) => <label key={k}>{label}<input value={settings[k]} onChange={e => saveSetting(k, e.target.value)} inputMode="decimal" /></label>)}
          </div>
          <div className="actions"><button className="small-btn" onClick={() => { setSettings({ ...SETTING_DEFAULTS }); try { localStorage.removeItem('scanSettings'); } catch { /* ignore */ } }}>Restore defaults</button><span className="small">Saved in this browser. Used for the next scan and backtest.</span></div>
        </details>
      </section>
      {scan && (
        <section className="panel">
          <h2>{scan.candidates.length} candidate{scan.candidates.length === 1 ? '' : 's'} for the session after {scan.last_session}</h2>
          <p className="small">Scanned {scan.universe.scanned} of {scan.universe.symbols} directory symbols on the {scan.feed.toUpperCase()} feed at {fmtTime(scan.created_at, zone)}. {scan.qualified_count} passed every rule; {scan.held_back_for_liquidity} were held back for liquidity or spread. Not scanned or no setup: {Object.entries(scan.rejected).map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`).join(', ')}.{scan.reassessed_at && ` Reassessed ${fmtTime(scan.reassessed_at, zone)}.`}</p>
          {scan.candidates.length < 10 && <p className="small">Fewer than 10 candidates met the rules, so fewer are shown.</p>}
          {scan.errors.map(e => <p key={e} className="banner warn">{e}</p>)}
          {scan.candidates.map(c => <CandidateCard key={c.symbol + c.setup} c={c} onAvailability={async (available) => { await api(`/api/availability/${c.symbol}`, { method: 'POST', body: JSON.stringify({ available, note: 'Confirmed in my GoTrade account' }) }); c.account_availability = { available, confirmed_at: Date.now() / 1000, note: 'Confirmed in my GoTrade account' }; setScan({ ...scan }); loadUniverse(); }} zone={zone} />)}
        </section>
      )}
      <section className="panel">
        <h2>Backtest by horizon</h2>
        <p className="small">Overnight-to-opening-hour and multi-day swing trades are tested separately, per setup, on a deterministic sample spread across the whole directory (or the symbols above).</p>
        <button disabled={!!busy || !universe?.count} onClick={() => act('bt', async () => { setBt(await api<Backtest>('/api/scanner/backtest', { method: 'POST', body: JSON.stringify({ symbols: list(), config: config() }) })); })}>{busy === 'bt' ? 'Running…' : 'Run scanner backtest'}</button>
        {bt && (
          <>
            <p className="small">{bt.symbols} symbols. Out-of-sample from {bt.split_date ?? 'n/a'}. {bt.notes.join(' ')}</p>
            {bt.errors.map(e => <p key={e} className="banner warn">{e}</p>)}
            <table className="signals"><thead><tr><th>Horizon</th><th>Setup</th><th>Sample</th><th>Trades</th><th>Win rate</th><th>Avg R</th><th>Avg return</th><th>Worst R</th><th>Gap exits</th></tr></thead><tbody>
              {Object.entries(bt.results).flatMap(([h, setups]) => Object.entries(setups).flatMap(([s, r]) => (['in_sample', 'out_of_sample'] as const).map(k => {
                const x = r[k];
                return <tr key={h + s + k}><td>{h}</td><td>{SETUP_NAMES[s]}</td><td>{k.replace(/_/g, ' ')}</td><td>{x.trades}</td><td>{x.win_rate === undefined ? 'n/a' : `${(x.win_rate * 100).toFixed(0)}%`}</td><td>{x.avg_r?.toFixed(2) ?? 'n/a'}</td><td>{x.avg_return_pct === undefined ? 'n/a' : `${x.avg_return_pct.toFixed(2)}%`}</td><td>{x.worst_r?.toFixed(2) ?? 'n/a'}</td><td>{x.gap_exits ?? 'n/a'}</td></tr>;
              })))}
            </tbody></table>
          </>
        )}
      </section>
    </div>
  );
}

function CandidateCard({ c, zone, onAvailability }: { c: Candidate; zone: ZoneKey; onAvailability: (available: boolean) => Promise<void> }) {
  const [horizon, setHorizon] = useState<'overnight' | 'swing'>('swing');
  const plan = c.plans[horizon];
  const avail = c.account_availability.available;
  return (
    <article className="scenario candidate">
      <h3>#{c.rank} {c.symbol} <span className="small">{c.name}</span> · {SETUP_NAMES[c.setup]}{c.quiet ? ' (quiet)' : ''}</h3>
      <p className="small">{c.rank_note}</p>
      {c.reassessment && <p className={`banner ${c.reassessment.status === 'plan unchanged' ? 'info' : 'warn'}`}><b>Reassessment: {c.reassessment.status}.</b> {c.reassessment.notes.join(' ')}</p>}
      <div className="grid2">
        <MiniChart symbol={c.symbol} plan={plan} />
        <div>
          <p><b>As of the {c.date} close:</b> {fmtPrice(c.close, 2)} · day {c.day_gain_pct.toFixed(2)}% · ATR {fmtPrice(c.atr, 2)} · extension {c.extension_atr.toFixed(2)} ATR · RSI {c.rsi?.toFixed(1) ?? 'n/a'} · relative volume {c.rvol?.toFixed(2) ?? 'n/a'}x · trend {c.trend}</p>
          <ul className="small">{c.evidence.map(e => <li key={e}>{e}</li>)}</ul>
          <p className="small">Optional confirmations: {Object.entries(c.optional).map(([k, v]) => `${v ? '✓' : '✗'} ${k}`).join(' · ')}</p>
          <div className="tabs" role="tablist" aria-label="Horizon">
            <button aria-pressed={horizon === 'swing'} onClick={() => setHorizon('swing')}>Multi-day swing</button>
            <button aria-pressed={horizon === 'overnight'} onClick={() => setHorizon('overnight')}>Overnight to opening hour</button>
          </div>
          <p><b>Entry trigger:</b> {plan.entry_trigger}</p>
          <p><b>Invalidation:</b> {plan.invalidation}</p>
          <p><b>Conditional targets:</b> {plan.targets.map(t => `${fmtPrice(t.price, 2)} (${t.source})`).join('; ') || 'none from observed structure'}</p>
          <p className="small">{plan.time_stop} Risk per share {fmtPrice(plan.risk_per_share, 2)}. {plan.gap_note ?? 'Gaps and slippage can cause losses beyond the stop.'}</p>
        </div>
      </div>
      <div className="grid3">
        <div><h4>Liquidity</h4><p className={c.liquidity.ok ? '' : 'warn'}>{c.liquidity.ok ? 'Passes thresholds' : c.liquidity.issues.join(' ')}</p><p className="small">20-day avg IEX dollar volume ${Math.round(c.liquidity.iex_avg_dollar_volume).toLocaleString()} · spread {c.liquidity.spread_pct === null ? 'n/a' : `${c.liquidity.spread_pct.toFixed(2)}%`}. {c.liquidity.note}</p></div>
        <div><h4>Data quality</h4><p>{c.data_quality.ok ? 'OK' : c.data_quality.issues.join(' ')}</p><p className="small">{c.data_quality.history_days} completed sessions; last {c.data_quality.last_session}. Daily candles built from regular-session 30-minute IEX bars.</p></div>
        <div><h4>Catalyst</h4><p className="small">{c.catalyst.note}</p><ul className="small">{c.catalyst.articles.map(a => <li key={a.id}>{a.url ? <a href={a.url} target="_blank" rel="noreferrer">{a.headline}</a> : a.headline} · {a.source} · {a.created_at}</li>)}</ul></div>
      </div>
      <p className="small"><b>GoTrade account availability:</b> {avail === null || avail === undefined ? 'Not confirmed.' : avail ? `Confirmed available ${c.account_availability.confirmed_at ? fmtTime(Math.floor(c.account_availability.confirmed_at), zone) : ''}.` : 'You marked it unavailable.'}{' '}
        <button className="small-btn" onClick={() => onAvailability(true)}>I can trade it in GoTrade</button>{' '}
        <button className="small-btn" onClick={() => onAvailability(false)}>Not available to me</button></p>
    </article>
  );
}

function MiniChart({ symbol, plan }: { symbol: string; plan: Plan }) {
  const host = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let chart: ReturnType<typeof createChart> | null = null;
    let cancelled = false;
    api<{ candles: Candle[]; indicators: Record<string, (number | null)[]> }>(`/api/scanner/chart/${symbol}`).then(d => {
      if (cancelled || !host.current) return;
      if (!d.candles.length) { setError('No cached candles for this symbol.'); return; }
      chart = createChart(host.current, { localization: { locale: 'en-US' }, autoSize: true, layout: { background: { type: ColorType.Solid, color: '#fff' }, textColor: '#202124' }, grid: { vertLines: { visible: false }, horzLines: { color: '#f1f3f4' } }, timeScale: { timeVisible: false } });
      const t = (x: number) => x as UTCTimestamp;
      const s = chart.addSeries(CandlestickSeries, { upColor: '#0f9d58', downColor: '#d93025', wickUpColor: '#0f9d58', wickDownColor: '#d93025', borderVisible: false });
      s.setData(d.candles.map(c => ({ time: t(c.time), open: c.open, high: c.high, low: c.low, close: c.close, ...(c.complete ? {} : { color: '#9aa0a6' }) })));
      for (const [k, color] of [['ema_fast', '#f29900'], ['ema_slow', '#1a73e8']] as const) {
        chart.addSeries(LineSeries, { color, lineWidth: 1, lastValueVisible: false, priceLineVisible: false }).setData(d.candles.map((c, i) => (d.indicators[k][i] === null ? { time: t(c.time) } : { time: t(c.time), value: d.indicators[k][i] as number })));
      }
      s.createPriceLine({ price: plan.trigger, color: '#1967d2', lineStyle: LineStyle.Dashed, lineWidth: 2, axisLabelVisible: true, title: 'Trigger' });
      s.createPriceLine({ price: plan.stop, color: '#d93025', lineStyle: LineStyle.Dashed, lineWidth: 2, axisLabelVisible: true, title: 'Invalidation' });
      plan.targets.slice(0, 3).forEach((x, i) => s.createPriceLine({ price: x.price, color: '#0f9d58', lineStyle: LineStyle.Dotted, lineWidth: 1, axisLabelVisible: true, title: `Target ${i + 1}` }));
      chart.timeScale().fitContent();
    }).catch(e => setError(e.message));
    return () => { cancelled = true; chart?.remove(); };
  }, [symbol, plan]);
  return <div>{error ? <p className="banner warn">{error}</p> : <div ref={host} className="minichart" role="img" aria-label={`${symbol} daily chart with trigger, invalidation, and targets`} />}</div>;
}
