import { useEffect, useState } from 'react';
import { chatgptPrompt, copyText } from './chatgpt';
import { api, fmtPrice, fmtTime, type ChartData, type Evidence, type ZoneKey } from './api';

export function Provenance({ data, zone, hashOk }: { data: ChartData; zone: ZoneKey; hashOk: boolean | null }) {
  const p = data.provenance;
  const formingCount = data.candles.filter(c => !c.complete).length;
  return (
    <section className="provenance" aria-label="Data source">
      <span><b>Feed:</b> {data.feed_label}</span>
      <span><b>Sessions:</b> {data.sessions === 'regular' ? 'Regular 09:30-16:00 ET' : 'Extended 04:00-20:00 ET (pre, regular, after-hours separated)'}</span>
      <span><b>Prices:</b> {data.adjustment} (unadjusted)</span>
      <span><b>Market now:</b> {p.market_state}</span>
      {p.replay_until && <span className="tag warn">Replay as of {fmtTime(p.replay_until, zone)}</span>}
      <span><b>Last completed candle:</b> {fmtTime(p.last_completed_time, zone)}</span>
      <span><b>Fetched:</b> {p.fetched_at ? `${fmtTime(Math.floor(p.fetched_at), zone)} (${Math.round(p.fetch_age_seconds ?? 0)} s ago)` : 'never'}</span>
      <span><b>Forming candles:</b> {formingCount} (grey, excluded from signals)</span>
      <span><b>Candles:</b> {data.missing.present} of {data.missing.expected_buckets} session buckets have trades</span>
      {p.late_corrections_this_fetch > 0 && <span className="tag warn">{p.late_corrections_this_fetch} late bar corrections applied</span>}
      <span className={hashOk ? 'tag ok' : 'tag error'}>{hashOk === null ? 'Checking data identity…' : hashOk ? `Chart and calculations use identical candles (SHA-256 ${data.data_hash.slice(0, 12)}…)` : 'Chart data does not match calculation data'}</span>
      {data.volume_note && <span className="small">{data.volume_note}</span>}
      {data.corporate_actions.map(a => <span key={a.date + a.type} className="tag warn">{a.message}</span>)}
      {data.corporate_action_error && <span className="tag warn">{data.corporate_action_error}</span>}
      {data.gates.notes.map(n => <span key={n} className="small">{n}</span>)}
    </section>
  );
}

const arrow = (d: string) => (d === 'bullish' ? '▲' : d === 'bearish' ? '▼' : '•');

function EvidenceList({ items }: { items: Evidence[] }) {
  if (!items.length) return <p className="small muted">None recorded.</p>;
  return <ul className="evidence">{items.map((e, i) => <li key={i} className={e.direction}><span aria-hidden="true">{arrow(e.direction)}</span> <span className="cat">{e.category.replace('_', ' ')}</span> {e.text}</li>)}</ul>;
}

export function AssessmentPanel({ data, zone }: { data: ChartData; zone: ZoneKey }) {
  const a = data.assessment;
  const groups = ['entry_qualification', 'trend_continuation', 'momentum', 'momentum_warning', 'volume', 'invalidation'];
  return (
    <section className="panel">
      <h2>Assessment: <span className={`label ${a.label}`}>{a.label}</span></h2>
      <p>{a.reason} As of the {data.timeframe} candle at {fmtTime(a.as_of, zone)}.</p>
      {!data.gates.actionable && <div className="banner warn"><b>Actionable confirmation blocked.</b><ul>{data.gates.blocked_reasons.map(r => <li key={r}>{r}</li>)}</ul></div>}
      <div className="grid2">
        <div><h3>Supporting</h3><EvidenceList items={a.supporting ?? []} /></div>
        <div><h3>Opposing</h3><EvidenceList items={a.opposing ?? []} /></div>
      </div>
      <details><summary>All evidence by category</summary>
        {groups.map(g => { const items = a.evidence.filter(e => e.category === g); return items.length ? <div key={g}><h4>{g.replace('_', ' ')}</h4><EvidenceList items={items} /></div> : null; })}
      </details>
      <h3>Observed levels</h3>
      <table className="kv"><tbody>
        <tr><th>Resistance (latest confirmed swing high)</th><td>{fmtPrice(a.levels.resistance)}</td></tr>
        <tr><th>Support (latest confirmed swing low)</th><td>{fmtPrice(a.levels.support)}</td></tr>
        <tr><th>Breakout buffer ({data.config.breakout_buffer_atr} × ATR)</th><td>{fmtPrice(a.levels.buffer)}</td></tr>
      </tbody></table>
      <h3>Conditional scenarios</h3>
      <p className="small">{a.disclaimer}</p>
      {a.scenarios.map(s => (
        <div key={s.name} className={`scenario ${s.name}`}>
          <h4>{s.name}</h4>
          <p><b>If:</b> {s.trigger}</p>
          <p><b>Then observed targets:</b> {s.targets.map(t => `${fmtPrice(t.price)} (${t.source})`).join('; ')}</p>
          <p><b>Invalidated if:</b> {s.invalidation}</p>
        </div>
      ))}
    </section>
  );
}

export function SignalPanel({ data, zone }: { data: ChartData; zone: ZoneKey }) {
  const [logged, setLogged] = useState<{ id: number; kind: string; variant: string; direction: string; bar_time: number; first_seen_at: number; actionable: number; blocked_reasons: string[]; explanation: string }[]>([]);
  useEffect(() => {
    api<{ signals: typeof logged }>(`/api/signals/history?symbol=${data.symbol}&timeframe=${data.timeframe}&sessions=${data.sessions}`).then(r => setLogged(r.signals)).catch(() => setLogged([]));
  }, [data]);
  const recent = [...data.signals].reverse().slice(0, 40);
  return (
    <section className="panel">
      <h2>Signal history</h2>
      <p className="small">Computed from completed candles only. Early signals carry weaker confirmation. Rules are shown with each entry.</p>
      <table className="signals"><thead><tr><th>Candle</th><th>Signal</th><th>Level</th><th>Status</th></tr></thead><tbody>
        {recent.map(s => (
          <tr key={`${s.kind}${s.variant}${s.time}`} className={s.direction}>
            <td>{fmtTime(s.time, zone)}</td>
            <td><b>{arrow(s.direction)} {s.kind.replace(/_/g, ' ')}</b> ({s.variant})<details><summary>Why</summary><p>{s.explanation}</p><p className="small">Rule: {s.rule}</p><p className="small">{s.confirmation_note}</p>{s.comparability_caveat && <p className="small warn">A corporate action falls inside the lookback; compare prices with care.</p>}</details></td>
            <td>{fmtPrice(s.level)}</td>
            <td>{s.actionable ? 'Actionable (your decision)' : s.blocked_reasons.join(' ')}</td>
          </tr>
        ))}
        {!recent.length && <tr><td colSpan={4}>No signals in the loaded range.</td></tr>}
      </tbody></table>
      <h3>Forward log</h3>
      <p className="small">Signals recorded when first seen on a recent completed candle. These timestamps form the forward paper-test record.</p>
      <ul className="small">{logged.map(s => <li key={s.id}>{fmtTime(s.bar_time, zone)} · {s.kind} ({s.variant}, {s.direction}) · first seen {fmtTime(Math.floor(s.first_seen_at), zone)} · {s.actionable ? 'actionable' : `blocked: ${s.blocked_reasons.join(' ')}`}</li>)}
        {!logged.length && <li>No live signals recorded yet.</li>}</ul>
    </section>
  );
}

export function ChatGptButton({ data, zone, extra }: { data: ChartData; zone: ZoneKey; extra?: string }) {
  const [label, setLabel] = useState('Copy for ChatGPT');
  return (
    <button onClick={async () => {
      const ok = await copyText(chatgptPrompt(data, zone, extra));
      setLabel(ok ? 'Copied. Paste it into ChatGPT and add your question' : 'Copy blocked by the browser');
      window.setTimeout(() => setLabel('Copy for ChatGPT'), 4000);
    }}>{label}</button>
  );
}

export function NextStepsPanel({ data, zone }: { data: ChartData; zone: ZoneKey }) {
  const n = data.assessment.next_steps;
  if (!n) return null;
  const lv = n.levels;
  const p = (x: number | null) => (x === null ? 'n/a' : x.toLocaleString('en-US', { style: 'currency', currency: 'USD' }));
  return (
    <section className={`panel simple ${n.status}`}>
      <p className="small">{data.symbol} · {data.timeframe} chart · as of {fmtTime(data.provenance.last_completed_time, zone)} · last price {p(data.assessment.levels.close)}</p>
      <h2>{n.headline}</h2>
      <div className="levels-strip">
        <div><span>Possible buy point</span><b>{p(lv.buy_trigger)}</b><small>only if a candle closes above it</small></div>
        <div><span>Stop (exit if wrong)</span><b>{p(lv.stop)}</b><small>close below it</small></div>
        <div><span>Take-profit areas</span><b>{lv.targets.length ? lv.targets.map(p).join(' → ') : 'n/a'}</b><small>not guaranteed</small></div>
      </div>
      <h3>What to do next</h3>
      <ol className="steps">{n.steps.map(s => <li key={s}>{s}</li>)}</ol>
      {n.scenarios && n.scenarios.length > 0 && (
        <>
          <h3>Possible scenarios, with entry and exit ideas</h3>
          <div className="grid3">{n.scenarios.map(sc => (
            <div key={sc.name} className={`scenario simple-sc ${sc.tone}`}>
              <h4>{sc.name}</h4>
              <p><b>If:</b> {sc.what}</p>
              {sc.entries.map(e => <div key={e.label} className="entry"><p><b>{e.label}:</b> {e.text}</p>{e.history && <p className="small history">{e.history}</p>}</div>)}
              {sc.exits.length > 0 && <><p><b>Exit plan:</b></p><ul>{sc.exits.map(x => <li key={x}>{x}</li>)}</ul></>}
            </div>
          ))}</div>
          <p className="small">The percentages are counts of what happened on this stock and timeframe in the loaded history, tested with next-candle fills and slippage. They are not odds for the future.</p>
        </>
      )}
      <p className="small">{n.reminder}</p>
      <div className="actions"><ChatGptButton data={data} zone={zone} /><span className="small">Copies this chart's numbers so ChatGPT can answer questions about them. Free, using your own ChatGPT.</span></div>
    </section>
  );
}
