import { Fragment } from 'react';
import { BookOpenCheck, ChevronDown, Clock, GitCommit, ScanSearch } from 'lucide-react';
import { CHECKS, verdictOf } from '../model';
import { DiffView, Loading, Mark, Offline, Stamp, go, useAsync, useRoute } from '../ui';
import { api } from '../api';
import type { RunEvent } from '../api';
import { RunPicker } from './RunPicker';

type CheckRow = {
  id: string;
  key: string;
  name: string;
  state: 'pass' | 'block' | 'skip';
  judgement: string;
  basis: string;
  paths?: Array<{ path: string; change: string; note: string; ok: boolean }>;
  hits?: Array<{ path: string; pattern: string }>;
  evidence?: Array<
    | { kind: 'raw'; label: string; text: string }
    | { kind: 'diff'; label: string; text: string }
    | { kind: 'facts'; label: string; rows: [string, string][] }
  >;
};

function FactsBlock({ rows }: { rows: [string, string][] }) {
  return <dl className="g-facts">{rows.map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd>{v}</dd></Fragment>)}</dl>;
}

function CheckBody({ row }: { row: CheckRow }) {
  const ev = row.evidence ?? [];
  return <div className="g-check-body">
    {row.paths && <table className="g-paths">
      <thead><tr><th>路径</th><th>变更</th><th>匹配情况</th><th></th></tr></thead>
      <tbody>{row.paths.map((p) => <tr key={p.path}>
        <td><code>{p.path}</code></td>
        <td>{p.change}</td>
        <td className="g-path-note">{p.note}</td>
        <td><Mark ok={p.ok} /></td>
      </tr>)}</tbody>
    </table>}
    {row.hits && <div className="g-hits">{row.hits.map((h, i) => <div key={i} className="g-hit">
      <span className="g-hit-path">{h.path}</span>
      <span className="g-hit-pattern">{h.pattern}</span>
    </div>)}</div>}
    {ev.map((e, i) => <div key={i}>
      <p className="g-ev-label">{e.label}</p>
      {e.kind === 'raw' && <pre className="code" tabIndex={0}>{e.text}</pre>}
      {e.kind === 'diff' && <DiffView text={e.text} />}
      {e.kind === 'facts' && <FactsBlock rows={e.rows} />}
    </div>)}
  </div>;
}

function CheckItem({ row }: { row: CheckRow }) {
  const def = CHECKS.find((c) => c.key === row.key);
  const Icon = def?.icon;
  const blocked = row.state === 'block';
  const hasDetail = !!(row.evidence?.length || row.paths || row.hits);
  const inner = <div className="g-check-row">
    <span className="g-check-num">{row.id}</span>
    <div>
      <p className="g-check-name">{Icon && <Icon size={13} style={{ marginRight: 5, verticalAlign: 'middle' }} />}{row.name}</p>
      <p className="g-check-judgement">{row.judgement}</p>
      <p className="g-check-basis">{row.basis}</p>
    </div>
    <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
      <Mark ok={row.state === 'pass' ? true : row.state === 'block' ? false : undefined} />
      {hasDetail && <ChevronDown size={14} className="g-check-chevron" />}
    </div>
  </div>;
  const cls = `g-check${blocked ? ' g-check-block' : ''}`;
  if (!hasDetail) return <div className={cls}>{inner}</div>;
  return <details className={cls}>
    <summary>{inner}</summary>
    <CheckBody row={row} />
  </details>;
}

function DossierShell({ runId, issueTitle, repo, commit, runtimeSec, attempts, verdict, checks }: {
  runId: string; issueTitle: string; repo: string; commit: string | null;
  runtimeSec: number; attempts: number; verdict: import('../model').Verdict;
  checks: CheckRow[];
}) {
  const passCount = checks.filter(c => c.state === 'pass').length;
  const blockCount = checks.filter(c => c.state === 'block').length;
  return <div className="g-dossier">
    <div className="g-doc-head">
      <div className="g-verdict-zone">
        <Stamp verdict={verdict} size="xl" fresh />
        <button className="btn btn-ghost" style={{ fontSize: 'var(--t-xs)' }}
          onClick={() => go({ page: 'policy', id: runId })}>
          <BookOpenCheck size={12} style={{ marginRight: 4 }} />去看这条策略
        </button>
      </div>
      <div>
        <h1 className="g-doc-title">{issueTitle}</h1>
        <div className="g-meta-chips">
          <span className="g-chip"><GitCommit size={11} />{repo}{commit ? ` @ ${commit.slice(0, 8)}` : ''}</span>
          <span className="g-chip"><Clock size={11} />{runtimeSec.toFixed(1)}s · {attempts} 次</span>
          <span className="g-chip"><ScanSearch size={11} />{runId}</span>
        </div>
      </div>
    </div>

    <section className="sec">
      <div className="sec-head"><h2>六项检查</h2><span className="sec-note">{passCount} 通过 · {blockCount} 拦截</span></div>
      <div className="g-checks">{checks.map((row) => <CheckItem key={row.id} row={row} />)}</div>
    </section>

    <section className="g-evidence-band" aria-label="证据状态">
      <div><strong>证据已封存</strong><span>本次运行的检查结果、执行轨迹和交付物已保存。</span></div>
      <span className="g-evidence-count">{runId}</span>
    </section>
  </div>;
}

function checksFromEvents(events: import('../api').RunEvent[]): CheckRow[] {
  // A retry produces multiple verification events. The dossier must reflect
  // the final gate decision, while the run detail keeps the full history.
  const verifyEv = [...events].reverse().find(e => e.kind === 'verification');
  const raw: Record<string, boolean> = verifyEv?.data?.checks ?? {};
  return CHECKS.map((def, i) => {
    const result = raw[def.key];
    return {
      id: String(i + 1).padStart(2, '0'),
      key: def.key,
      name: def.name,
      state: result === false ? 'block' : result === true ? 'pass' : 'skip' as 'pass' | 'block' | 'skip',
      judgement: result === true ? def.hint : result === false ? `${def.name}未通过` : '未执行',
      basis: def.hint,
    };
  });
}

export function InspectPage() {
  const route = useRoute();
  const id = route.id;

  const entry = useAsync(() => id ? Promise.resolve([]) : api.runs(), [id]);

  const { data: run, error: runErr, loading: runLoading, reload } = useAsync(
    () => id ? api.run(id) : Promise.resolve(null), [id]);
  const { data: events, loading: evLoading } = useAsync(
    () => id ? api.events(id) : Promise.resolve(null), [id]);
  if (!id) {
    return <RunPicker page="inspect" runs={entry.data} loading={entry.loading} error={entry.error} onRetry={entry.reload} />;
  }

  if (runLoading || evLoading) return <Loading />;
  if (runErr) return <Offline message={runErr} onRetry={reload} />;
  if (!run) return <Offline message="找不到运行记录" onRetry={reload} />;

  const checks = checksFromEvents(events ?? []);
  return <DossierShell
    runId={run.run_id}
    issueTitle={run.task?.issue_title ?? run.title}
    repo={run.repo}
    commit={run.commit}
    runtimeSec={run.runtime_sec}
    attempts={run.attempt}
    verdict={verdictOf(run.conclusion)}
    checks={checks}
  />;
}
