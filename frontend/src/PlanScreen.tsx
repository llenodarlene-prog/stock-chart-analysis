import { useEffect, useMemo, useState } from 'react';
import { api, fmtMoney, fmtPrice, fmtTime, type ChartData, type ZoneKey } from './api';
import Chart, { type Overlay, type Toggles } from './Chart';

type PlanInput = { id?: number; symbol: string; timeframe: string; mode: 'plan' | 'existing'; side: 'long' | 'short'; capital: string; entry: string; shares: string; max_risk: string; stop: string; target1: string; target2: string; fee_per_share: string; fee_per_order: string; slippage_bps: string; notes: string; actual_entry: string; actual_exit: string; actual_shares: string };
type Result = { shares: number; shares_by_risk: number | null; shares_by_capital: number | null; position_cost: number; risk_per_share: number; loss_at_stop: number; round_trip_fees: number; entry_with_slippage: number; targets: { price: number; source: string; label: string; pnl: number; reward_to_risk: number | null }[]; exit_comparison: { rule: string; pnl_if_reached: number | null; pnl_if_stopped: number; pnl_if_stopped_after_partial?: number; note?: string }[]; warnings: string[]; reminder: string };
type Saved = PlanInput & { id: number; created_at: number; updated_at: number; data_time?: number; outcome: { actual_pnl: number; planned_risk: number; actual_r: number | null } | null; original_stop?: number };

const blank = (data: ChartData): PlanInput => ({ symbol: data.symbol, timeframe: data.timeframe, mode: 'plan', side: 'long', capital: '', entry: '', shares: '', max_risk: '', stop: '', target1: '', target2: '', fee_per_share: '0', fee_per_order: '0', slippage_bps: '5', notes: '', actual_entry: '', actual_exit: '', actual_shares: '' });
const num = (v: string) => (v.trim() === '' ? null : Number(v));

export default function PlanScreen({ data, zone, toggles }: { data: ChartData; zone: ZoneKey; toggles: Toggles }) {
  const [form, setForm] = useState<PlanInput>(() => blank(data));
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<Saved[]>([]);
  const [status, setStatus] = useState('');
  const levels = data.assessment.levels;
  const set = (key: keyof PlanInput) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => setForm({ ...form, [key]: e.target.value });

  const refresh = () => api<{ plans: Saved[] }>('/api/plans').then(r => setSaved(r.plans.filter(p => p.symbol === data.symbol))).catch(e => setError(e.message));
  useEffect(() => { refresh(); }, [data.symbol]);

  const payload = () => ({
    ...form, entry: num(form.entry), stop: num(form.stop), capital: num(form.capital), shares: num(form.shares), max_risk: num(form.max_risk),
    fee_per_share: num(form.fee_per_share) ?? 0, fee_per_order: num(form.fee_per_order) ?? 0, slippage_bps: num(form.slippage_bps) ?? 0,
    targets: [form.target1, form.target2].filter(t => t.trim()).map((t, i) => ({ price: Number(t), label: `Your target ${i + 1}` })),
    actual_entry: num(form.actual_entry), actual_exit: num(form.actual_exit), actual_shares: num(form.actual_shares),
    algo_levels: levels, data_time: data.provenance.last_completed_time, data_hash: data.data_hash,
  });

  const calculate = async () => {
    setError(null);
    try { setResult(await api<Result>('/api/plans/calculate', { method: 'POST', body: JSON.stringify(payload()) })); }
    catch (e) { setResult(null); setError((e as Error).message); }
  };
  const save = async () => {
    try {
      const r = await api<{ id: number }>('/api/plans', { method: 'POST', body: JSON.stringify(payload()) });
      setForm({ ...form, id: r.id }); setStatus(`Saved plan #${r.id}.`); refresh();
    } catch (e) { setError((e as Error).message); }
  };
  const useAlgo = (field: keyof PlanInput, value: number | null) => () => { if (value !== null) setForm({ ...form, [field]: value.toFixed(4) }); };

  const overlays = useMemo<Overlay[]>(() => {
    const out: Overlay[] = [];
    const add = (price: number | null, label: string, source: Overlay['source'], kind: Overlay['kind']) => { if (price !== null && Number.isFinite(price)) out.push({ price, label, source, kind }); };
    add(num(form.entry), 'entry', 'user', 'entry');
    add(num(form.stop), 'stop', 'user', 'stop');
    add(num(form.target1), 'target 1', 'user', 'target');
    add(num(form.target2), 'target 2', 'user', 'target');
    add(levels.resistance, 'resistance', 'algorithm', 'level');
    add(levels.support, 'support', 'algorithm', 'level');
    for (const t of result?.targets.filter(t => t.source !== 'user') ?? []) add(t.price, t.label, t.source as Overlay['source'], 'target');
    return out;
  }, [form, levels, result]);

  return (
    <div className="plan">
      <p className="banner info">Timeframe {data.timeframe}, {data.sessions} session, data through the candle at {fmtTime(data.provenance.last_completed_time, zone)} ({data.feed.toUpperCase()} feed). Solid lines are your levels; dashed lines are algorithm-derived or calculated.</p>
      {!data.gates.actionable && <div className="banner warn"><b>Signal confirmation is blocked for this data.</b> You can still plan, but the algorithm will not mark anything actionable: {data.gates.blocked_reasons.join(' ')}</div>}
      <Chart data={data} zone={zone} toggles={toggles} overlays={overlays} />
      <div className="grid">
        <section className="panel">
          <h2>Your plan</h2>
          <div className="form-grid">
            <label>Mode <select value={form.mode} onChange={set('mode')}><option value="plan">Plan a potential trade</option><option value="existing">Manage an existing position</option></select></label>
            <label>Side <select value={form.side} onChange={set('side')}><option value="long">Long</option><option value="short">Short</option></select></label>
            <label>Available capital ($)<input value={form.capital} onChange={set('capital')} inputMode="decimal" /></label>
            <label>Maximum planned risk ($)<input value={form.max_risk} onChange={set('max_risk')} inputMode="decimal" /></label>
            <label>Entry price{form.mode === 'existing' ? ' (your fill)' : ''}<input value={form.entry} onChange={set('entry')} inputMode="decimal" /></label>
            <label>Shares (blank = size by risk and capital)<input value={form.shares} onChange={set('shares')} inputMode="numeric" /></label>
            <label>Stop / invalidation price<input value={form.stop} onChange={set('stop')} inputMode="decimal" /></label>
            <label>Your target 1<input value={form.target1} onChange={set('target1')} inputMode="decimal" /></label>
            <label>Your target 2<input value={form.target2} onChange={set('target2')} inputMode="decimal" /></label>
            <label>Fee per share ($)<input value={form.fee_per_share} onChange={set('fee_per_share')} inputMode="decimal" /></label>
            <label>Fee per order ($)<input value={form.fee_per_order} onChange={set('fee_per_order')} inputMode="decimal" /></label>
            <label>Slippage (basis points)<input value={form.slippage_bps} onChange={set('slippage_bps')} inputMode="decimal" /></label>
          </div>
          {form.mode === 'existing' && <p className="small">For a saved existing position, the stop can only be tightened. Widening it to avoid an exit is rejected.</p>}
          <p className="small">Fill from algorithm-derived levels (you still decide):{' '}
            <button className="small-btn" onClick={useAlgo('entry', levels.resistance !== null ? levels.resistance + levels.buffer : null)}>Entry = resistance + buffer</button>{' '}
            <button className="small-btn" onClick={useAlgo('stop', levels.support)}>Stop = support</button>
          </p>
          <label className="block">Notes<textarea value={form.notes} onChange={set('notes')} rows={2} /></label>
          <div className="actions"><button className="primary" onClick={calculate}>Calculate</button><button onClick={save}>Save plan</button><button onClick={() => { setForm(blank(data)); setResult(null); }}>New plan</button></div>
          {error && <p className="banner error" role="alert">{error}</p>}
          {status && <p className="small">{status}</p>}
        </section>
        <section className="panel">
          <h2>Results</h2>
          {!result && <p className="muted">Enter an entry and a stop, then Calculate.</p>}
          {result && (
            <>
              <table className="kv"><tbody>
                <tr><th>Shares</th><td>{result.shares} (risk allows {result.shares_by_risk ?? 'n/a'}, capital allows {result.shares_by_capital ?? 'n/a'})</td></tr>
                <tr><th>Position cost (with slippage)</th><td>{fmtMoney(result.position_cost)}</td></tr>
                <tr><th>Risk per share</th><td>{fmtPrice(result.risk_per_share)}</td></tr>
                <tr><th>P/L if stopped (fees and slippage included)</th><td className="bearish">{fmtMoney(result.loss_at_stop)}</td></tr>
                <tr><th>Round-trip fees</th><td>{fmtMoney(result.round_trip_fees)}</td></tr>
              </tbody></table>
              {result.warnings.map(w => <p key={w} className="small warn">{w}</p>)}
              <h3>Target zones</h3>
              <table className="signals"><thead><tr><th>Price</th><th>Source</th><th>P/L if reached</th><th>Reward : risk</th></tr></thead><tbody>
                {result.targets.map(t => <tr key={t.label + t.price}><td>{fmtPrice(t.price)}</td><td><span className={`src ${t.source}`}>{t.source}</span> {t.label}</td><td>{fmtMoney(t.pnl)}</td><td>{t.reward_to_risk === null ? 'n/a' : `${t.reward_to_risk.toFixed(2)} : 1`}</td></tr>)}
              </tbody></table>
              <h3>Exit approaches</h3>
              <ul>{result.exit_comparison.map(x => <li key={x.rule}><b>{x.rule}.</b> If reached: {fmtMoney(x.pnl_if_reached)}. If stopped first: {fmtMoney(x.pnl_if_stopped)}.{x.pnl_if_stopped_after_partial !== undefined && ` If stopped at entry after the partial: ${fmtMoney(x.pnl_if_stopped_after_partial)}.`} {x.note}</li>)}</ul>
              <p className="small">{result.reminder}</p>
            </>
          )}
        </section>
      </div>
      <section className="panel">
        <h2>Scenarios from observed structure</h2>
        <p className="small">{data.assessment.disclaimer}</p>
        <div className="grid3">{data.assessment.scenarios.map(s => (
          <div key={s.name} className={`scenario ${s.name}`}>
            <h3>{s.name}</h3>
            <p><b>Trigger:</b> {s.trigger}</p>
            <p><b>Observed targets:</b> {s.targets.map(t => `${fmtPrice(t.price)} (${t.source})`).join('; ')}</p>
            <p><b>Invalidation:</b> {s.invalidation}</p>
            <p><b>Evidence supporting now:</b></p><ul className="small">{s.supporting_now.map(x => <li key={x}>{x}</li>)}{!s.supporting_now.length && <li>None recorded.</li>}</ul>
            <p><b>Evidence against now:</b></p><ul className="small">{s.opposing_now.map(x => <li key={x}>{x}</li>)}{!s.opposing_now.length && <li>None recorded.</li>}</ul>
          </div>
        ))}</div>
      </section>
      <section className="panel">
        <h2>Saved plans: planned versus actual</h2>
        <p className="small">Record the actual fill and exit yourself after any trade you decide to make. Nothing here is pre-filled from a broker.</p>
        <table className="signals"><thead><tr><th>Saved</th><th>Plan</th><th>Planned risk</th><th>Actual result</th><th></th></tr></thead><tbody>
          {saved.map(p => (
            <tr key={p.id}>
              <td>{fmtTime(Math.floor(p.updated_at), zone)}</td>
              <td>{p.mode} {p.side} · entry {fmtPrice(Number(p.entry))} · stop {fmtPrice(Number(p.stop))} · {p.timeframe}</td>
              <td>{p.outcome ? fmtMoney(p.outcome.planned_risk) : 'n/a'}</td>
              <td>{p.outcome ? `${fmtMoney(p.outcome.actual_pnl)} (${p.outcome.actual_r?.toFixed(2)} R)` : 'Not recorded'}</td>
              <td><button className="small-btn" onClick={() => setForm({ ...blank(data), ...Object.fromEntries(Object.entries(p).map(([k, v]) => [k, v === null || v === undefined ? '' : typeof v === 'number' || typeof v === 'string' ? String(v) : v])), id: p.id, target1: '', target2: '' } as PlanInput)}>Edit</button>{' '}
                <button className="small-btn" onClick={() => api(`/api/plans/${p.id}`, { method: 'DELETE' }).then(refresh)}>Delete</button></td>
            </tr>
          ))}
          {!saved.length && <tr><td colSpan={5}>No saved plans for {data.symbol}.</td></tr>}
        </tbody></table>
        {form.id && (
          <div className="form-grid">
            <label>Actual entry<input value={form.actual_entry} onChange={set('actual_entry')} inputMode="decimal" /></label>
            <label>Actual exit<input value={form.actual_exit} onChange={set('actual_exit')} inputMode="decimal" /></label>
            <label>Actual shares<input value={form.actual_shares} onChange={set('actual_shares')} inputMode="numeric" /></label>
            <button onClick={save}>Save outcome</button>
          </div>
        )}
      </section>
    </div>
  );
}
