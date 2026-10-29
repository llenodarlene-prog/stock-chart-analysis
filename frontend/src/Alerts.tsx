import { useEffect, useRef, useState } from 'react';
import { api, fmtTime, TIMEFRAMES, type ZoneKey } from './api';

type Alert = { id: number; created_at: number; symbol: string; timeframe: string; kind: string; actionable: number; title: string; message: string; read: number };
type Monitor = { settings: { enabled: boolean; timeframe: string; sessions: string; adjustment: string; interval_seconds: number }; running: boolean; last_run: number | null; last_error: string | null; checked: { symbol: string; ok: boolean; new_alerts?: number; label?: string; error?: string | null }[]; watchlist: string[] };

const POLL_MS = 20_000;

function beep() {
  try {
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.15, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.5);
    osc.connect(gain).connect(ctx.destination);
    osc.start(); osc.stop(ctx.currentTime + 0.5);
  } catch { /* sound unavailable */ }
}

// Polls the backend for alerts created by the background watchlist monitor and
// shows them as Mac/browser notifications with a short sound.
export function AlertsBell({ zone, onPick }: { zone: ZoneKey; onPick: (symbol: string) => void }) {
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [open, setOpen] = useState(false);
  const [permission, setPermission] = useState(typeof Notification === 'undefined' ? 'unsupported' : Notification.permission);
  const lastId = useRef<number | null>(null);

  useEffect(() => {
    const poll = async () => {
      try {
        const r = await api<{ alerts: Alert[] }>('/api/alerts');
        setAlerts(r.alerts);
        const newest = r.alerts[0]?.id ?? 0;
        if (lastId.current !== null && newest > lastId.current) {
          const fresh = r.alerts.filter(a => a.id > (lastId.current ?? 0));
          beep();
          if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
            for (const a of fresh.slice(0, 3)) new Notification(a.title, { body: a.message.slice(0, 180) });
          }
        }
        lastId.current = newest;
      } catch { /* backend offline; the main screen shows that error */ }
    };
    poll();
    const id = window.setInterval(poll, POLL_MS);
    return () => window.clearInterval(id);
  }, []);

  const unread = alerts.filter(a => !a.read).length;
  return (
    <div className="alerts">
      <button onClick={() => setOpen(!open)} aria-expanded={open}>🔔 Alerts{unread ? ` (${unread})` : ''}</button>
      {open && (
        <div className="alerts-panel" role="dialog" aria-label="Alerts">
          <div className="actions">
            {permission !== 'granted' && permission !== 'unsupported' && <button className="small-btn" onClick={async () => setPermission(await Notification.requestPermission())}>Turn on Mac notifications</button>}
            {permission === 'granted' && <span className="small">Notifications on.</span>}
            <button className="small-btn" onClick={async () => { await api('/api/alerts/read', { method: 'POST' }); setAlerts(alerts.map(a => ({ ...a, read: 1 }))); }}>Mark all read</button>
          </div>
          {!alerts.length && <p className="small">No alerts yet. Add stocks to your watchlist; they are checked in the background every minute while the app is running.</p>}
          <ul>{alerts.map(a => (
            <li key={a.id} className={a.read ? 'read' : ''}>
              <button className="linkish" onClick={() => { onPick(a.symbol); setOpen(false); }}><b>{a.title}</b></button>
              <p className="small">{fmtTime(Math.floor(a.created_at), zone)} · {a.actionable ? 'data checks passed' : 'information only'}</p>
              <p>{a.message}</p>
            </li>
          ))}</ul>
          <p className="small">Alerts tell you when to look. You decide whether to act; this app never places trades.</p>
        </div>
      )}
    </div>
  );
}

export function MonitorPanel({ zone }: { zone: ZoneKey }) {
  const [m, setM] = useState<Monitor | null>(null);
  const [busy, setBusy] = useState(false);
  const load = () => api<Monitor>('/api/monitor').then(setM).catch(() => setM(null));
  useEffect(() => { load(); const id = window.setInterval(load, 30_000); return () => window.clearInterval(id); }, []);
  if (!m) return null;
  const update = async (patch: Record<string, unknown>) => { await api('/api/monitor/settings', { method: 'POST', body: JSON.stringify(patch) }); load(); };
  return (
    <div className="monitor">
      <h2>Background checks</h2>
      <label className="small"><input type="checkbox" checked={m.settings.enabled} onChange={e => update({ enabled: e.target.checked })} /> Check watchlist every minute</label>
      <label className="small">Timeframe <select value={m.settings.timeframe} onChange={e => update({ timeframe: e.target.value })}>{TIMEFRAMES.map(t => <option key={t}>{t}</option>)}</select></label>
      <p className="small">Last check: {m.last_run ? fmtTime(Math.floor(m.last_run), zone) : m.running ? 'starting…' : 'not running (restart the app after adding Alpaca keys)'}</p>
      <ul className="small">{m.checked.map(c => <li key={c.symbol}>{c.symbol}: {c.ok ? c.label : `error: ${c.error}`}</li>)}</ul>
      <button className="small-btn" disabled={busy} onClick={async () => { setBusy(true); try { setM(await api<Monitor>('/api/monitor/run', { method: 'POST' })); } finally { setBusy(false); } }}>{busy ? 'Checking…' : 'Check now'}</button>
    </div>
  );
}
