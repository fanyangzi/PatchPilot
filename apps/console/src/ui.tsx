/** Shared primitives: data hook, routing, verdict stamp, diff view, states. */
import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Check, X, Minus, LoaderCircle, PlugZap } from 'lucide-react';
import { VERDICT, type Verdict } from './model';

export function useAsync<T>(load: () => Promise<T>, deps: unknown[]) {
  const [state, setState] = useState<{ data?: T; error?: string; loading: boolean }>({ loading: true });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    setState((s) => ({ ...s, loading: true, error: undefined }));
    load().then(
      (data) => live && setState({ data, loading: false }),
      (e) => live && setState({ error: e instanceof Error ? e.message : String(e), loading: false }),
    );
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, reload };
}

/** Hash routes: #/overview, #/runs/<id>, #/routing/<id>, #/eval */
export type Route = { page: 'overview' | 'runs' | 'routing' | 'eval'; id?: string };

function parse(hash: string): Route {
  const [page, id] = hash.replace(/^#\/?/, '').split('/');
  if (page === 'runs' || page === 'routing' || page === 'eval') return { page, id: id ? decodeURIComponent(id) : undefined };
  return { page: 'overview' };
}

export function useRoute() {
  const [route, setRoute] = useState(() => parse(location.hash));
  useEffect(() => {
    const on = () => { setRoute(parse(location.hash)); window.scrollTo({ top: 0 }); };
    addEventListener('hashchange', on);
    return () => removeEventListener('hashchange', on);
  }, []);
  return route;
}

export const href = (r: Route) => `#/${r.page}${r.id ? `/${encodeURIComponent(r.id)}` : ''}`;
export const go = (r: Route) => { location.hash = href(r); };

/** The verdict seal. `fresh` plays the one stamping animation in the app. */
export function Stamp({ verdict, size = 'md', fresh = false }: { verdict: Verdict; size?: 'sm' | 'md' | 'lg'; fresh?: boolean }) {
  const v = VERDICT[verdict];
  return <span className={`stamp stamp-${verdict} stamp-${size} ${fresh ? 'stamp-fresh' : ''}`} role="img" aria-label={`结论：${v.label}`}>
    <span className="stamp-text">{v.stamp}</span>
  </span>;
}

export function VerdictTag({ verdict }: { verdict: Verdict }) {
  return <span className={`tag tag-${verdict}`}><i aria-hidden />{VERDICT[verdict].label}</span>;
}

export function Mark({ ok }: { ok: boolean | undefined }) {
  if (ok === undefined) return <span className="mark mark-none" aria-label="未执行"><Minus size={12} /></span>;
  return ok
    ? <span className="mark mark-ok" aria-label="通过"><Check size={12} strokeWidth={3} /></span>
    : <span className="mark mark-bad" aria-label="未通过"><X size={12} strokeWidth={3} /></span>;
}

export function Loading({ label = '正在读取' }: { label?: string }) {
  return <div className="state"><LoaderCircle className="spin" size={18} /> {label}</div>;
}

export function Offline({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return <div className="state state-offline">
    <PlugZap size={22} />
    <div>
      <strong>{message}</strong>
      <p>在项目根目录启动后端后再刷新：</p>
      <code>PYTHONPATH=src .venv/bin/uvicorn apps.api.main:app --port 8010</code>
    </div>
    {onRetry && <button className="btn" onClick={onRetry}>重新连接</button>}
  </div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="state state-empty">{children}</div>;
}

export function DiffView({ text }: { text: string }) {
  const lines = text.replace(/\n$/, '').split('\n');
  return <pre className="code diff" tabIndex={0}>{lines.map((line, i) => {
    const cls = line.startsWith('+++') || line.startsWith('---') ? 'd-file'
      : line.startsWith('@@') ? 'd-hunk' : line.startsWith('+') ? 'd-add' : line.startsWith('-') ? 'd-del' : '';
    return <span key={i} className={`d-line ${cls}`}>{line || ' '}</span>;
  })}</pre>;
}

export function Meter({ value, tone = 'ink' }: { value: number; tone?: string }) {
  return <span className={`meter meter-${tone}`} aria-hidden><i style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }} /></span>;
}
