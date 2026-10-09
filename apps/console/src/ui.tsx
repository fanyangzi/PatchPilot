/** Shared primitives: data hook, routing, verdict seal, ruled sections, diff, states. */
import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Check, LoaderCircle, Minus, PlugZap, X } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
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

/** URL routes: /inbox, /review/<task>, /investigation/<task>, /contract/<task>, /report/<task>. Legacy hash links remain readable. */
export type Route = {
  page: 'overview' | 'runs' | 'routing' | 'eval' | 'inspect' | 'policy' | 'inbox' | 'review' | 'investigation' | 'contract' | 'report';
  id?: string;
  /** Review selections are part of the URL so a refresh cannot silently switch evidence. */
  candidateId?: string;
  contractId?: string;
  checkId?: string;
  reportId?: string;
};

function parse(pathname = location.pathname, search = location.search, hash = location.hash): Route {
  // Read old hash links during migration, but emit and navigate with ordinary
  // paths so a copied review URL remains useful outside the SPA session.
  const legacyPath = hash.startsWith('#/') ? hash.slice(2) : '';
  const raw = legacyPath || `${pathname.replace(/^\/+/, '')}${search}`;
  const [path, query = ''] = raw.split('?');
  const [page, id] = path.split('/');
  const params = new URLSearchParams(query);
  const selection = {
    candidateId: params.get('candidate') || undefined,
    contractId: params.get('contract') || undefined,
    checkId: params.get('check') || undefined,
    reportId: params.get('report') || undefined,
  };
  if (page === 'runs' || page === 'routing' || page === 'eval' || page === 'inbox' || page === 'review' || page === 'investigation' || page === 'contract' || page === 'report') return { page, id: id ? decodeURIComponent(id) : undefined, ...selection };
  if (page === 'inspect' || page === 'policy') return { page, id: id ? decodeURIComponent(id) : undefined, ...selection };
  return { page: 'inbox' };
}

export function useRoute() {
  const [route, setRoute] = useState(() => parse());
  useEffect(() => {
    const on = () => {
      // Convert bookmarks made by the previous hash router once, preserving
      // the path and query while removing the fragment from the address bar.
      if (location.hash.startsWith('#/')) history.replaceState({}, '', `${location.hash.slice(1)}`);
      setRoute(parse()); window.scrollTo({ top: 0 });
    };
    addEventListener('popstate', on);
    addEventListener('hashchange', on);
    on();
    return () => { removeEventListener('popstate', on); removeEventListener('hashchange', on); };
  }, []);
  return route;
}

export const href = (r: Route) => {
  const params = new URLSearchParams();
  if (r.candidateId) params.set('candidate', r.candidateId);
  if (r.contractId) params.set('contract', r.contractId);
  if (r.checkId) params.set('check', r.checkId);
  if (r.reportId) params.set('report', r.reportId);
  const query = params.toString();
  return `/${r.page}${r.id ? `/${encodeURIComponent(r.id)}` : ''}${query ? `?${query}` : ''}`;
};
export const go = (r: Route) => {
  const target = href(r);
  if (`${location.pathname}${location.search}` !== target) history.pushState({}, '', target);
  dispatchEvent(new PopStateEvent('popstate'));
};

/** A ruled section head. Every section in the console is introduced this way. */
export function Section({ icon: Icon, title, note, sub, children, className = '' }: {
  icon?: LucideIcon; title: string; note?: ReactNode; sub?: ReactNode; children: ReactNode; className?: string;
}) {
  return <section className={`sec ${className}`}>
    <div className="sec-head">
      {Icon && <Icon size={15} />}
      <h2>{title}</h2>
      {note && <span className="sec-note">{note}</span>}
    </div>
    {sub && <p className="sec-sub">{sub}</p>}
    {children}
  </section>;
}

/** The verdict seal. `fresh` plays the one stamping animation in the app. */
export function Stamp({ verdict, size = 'md', fresh = false, date }: { verdict: Verdict; size?: 'sm' | 'md' | 'lg' | 'xl'; fresh?: boolean; date?: string }) {
  const v = VERDICT[verdict];
  return <span className={`stamp stamp-${verdict} stamp-${size} ${fresh ? 'stamp-fresh' : ''}`} role="img" aria-label={`结论：${v.label}`}>
    <span className="stamp-text">{v.stamp}</span>
    {date && <span className="stamp-date" aria-hidden>{date}</span>}
  </span>;
}

export function VerdictTag({ verdict, size = 13 }: { verdict: Verdict; size?: number }) {
  const v = VERDICT[verdict];
  const Icon = v.icon;
  return <span className={`tag tag-${verdict}`}><Icon size={size} />{v.label}</span>;
}

export function Mark({ ok }: { ok: boolean | undefined }) {
  if (ok === undefined) return <span className="mark mark-none" aria-label="未执行"><Minus size={12} /></span>;
  return ok
    ? <span className="mark mark-ok" aria-label="通过"><Check size={12} strokeWidth={3} /></span>
    : <span className="mark mark-bad" aria-label="未通过"><X size={12} strokeWidth={3} /></span>;
}

export function Loading({ label = '正在读取' }: { label?: string }) {
  return <div className="state" role="status" aria-live="polite"><LoaderCircle className="spin" size={18} aria-hidden /> {label}</div>;
}

export function Offline({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return <div className="state state-offline" role="alert">
    <PlugZap size={22} aria-hidden />
    <div>
      <strong>{message}</strong>
      <p>在项目根目录启动本地服务后再刷新：</p>
      <code>./start.sh</code>
    </div>
    {onRetry && <button className="btn" onClick={onRetry}>重新连接</button>}
  </div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="state state-empty">{children}</div>;
}

/** A unified diff rendered with a line-number gutter.
 *
 * Line numbers are counted as the patch is read: hunks reset both counters,
 * context and added lines advance the new file, removed lines the old one.
 */
export function DiffView({ text }: { text: string }) {
  const lines = text.replace(/\n$/, '').split('\n');
  let oldNo = 0;
  let newNo = 0;
  return <pre className="code diff" tabIndex={0}>{lines.map((line, i) => {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)?/.exec(line);
    if (hunk) { oldNo = Number(hunk[1]); newNo = Number(hunk[2]); }
    const file = line.startsWith('+++') || line.startsWith('---');
    const cls = file ? 'd-file' : hunk ? 'd-hunk'
      : line.startsWith('+') ? 'd-add' : line.startsWith('-') ? 'd-del' : '';
    let no: number | null = null;
    if (!file && !hunk) {
      if (cls === 'd-del') no = oldNo++;
      else { no = newNo++; if (cls !== 'd-add') oldNo++; }
    }
    return <span key={i} className={`d-line ${cls}`}><i>{no ?? ''}</i><span>{line || ' '}</span></span>;
  })}</pre>;
}

export function Meter({ value, tone = 'ink' }: { value: number; tone?: string }) {
  return <span className={`meter meter-${tone}`} aria-hidden><i style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }} /></span>;
}
