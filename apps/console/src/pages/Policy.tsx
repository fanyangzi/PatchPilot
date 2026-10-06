import { Loading, Offline, useAsync, useRoute } from '../ui';
import { api } from '../api';
import type { PolicyData } from '../api';
import type { RunRecord } from '../api';
import { when } from '../model';
import { RunPicker } from './RunPicker';

type PolicyRule = { key: string; value: string; list?: string[]; note?: string; strong?: boolean };

function RuleBlock({ rules }: { rules: PolicyRule[] }) {
  return <div className="g-rules">
    {rules.map((r) => (
      <div key={r.key} className="g-rule">
        <div className="g-rule-kv">
          <span className="g-rule-key">{r.key}</span>
          <span className={`g-rule-val${r.strong ? ' g-strong' : ''}`}>{r.value}</span>
        </div>
        {r.list && <div className="g-rule-list">{r.list.map((item) => <span key={item} className="g-rule-item">{item}</span>)}</div>}
        {r.note && <p className="g-rule-note">{r.note}</p>}
      </div>
    ))}
  </div>;
}

function lineClass(line: string) {
  const t = line.trimStart();
  if (t.startsWith('#')) return 'g-yl-comment';
  if (/^\w[\w_-]*:/.test(t)) return 'g-yl-key';
  return '';
}

function StringList({ items, empty = '无限制' }: { items: string[]; empty?: string }) {
  if (items.length === 0) {
    return <span style={{ fontSize: 'var(--t-xs)', color: 'var(--ink-3)' }}>{empty}</span>;
  }
  return <div className="g-rule-list">{items.map(i => <span key={i} className="g-rule-item">{i}</span>)}</div>;
}

function PolicyShell({ data, runId }: { data: PolicyData; runId: string }) {
  const clauses: Array<{ n: string; text: string; key: keyof PolicyData; empty?: string }> = [
    { n: '01', text: '允许修改的路径范围。补丁中的改动必须落在这些路径内。', key: 'allowed_paths' },
    { n: '02', text: '敏感路径模式。补丁不得触碰匹配这些模式的文件（凭据、CI、发布配置、密钥材料）。', key: 'sensitive_patterns' },
    { n: '03', text: '必须通过的检查项。结论为可信交付时，这些检查全部必须通过。', key: 'required_checks', empty: '无' },
  ];
  return <div>
    <div className="g-policy-head">
      <h1 className="g-policy-title">{data.source || '策略'}</h1>
      <p className="g-policy-preamble" style={{ fontSize: 'var(--t-xs)', color: 'var(--ink-3)', marginTop: 4 }}>
        运行 {runId} 所用策略
      </p>
      <div className="g-meta-chips" aria-label="策略来源">
        {data.repository && <span className="g-chip">仓库 · {data.repository}</span>}
        {data.commit && <span className="g-chip">commit · {data.commit.slice(0, 8)}</span>}
        {data.compiled_at && <span className="g-chip">编译于 · {when(data.compiled_at)}</span>}
        {data.scope?.length && <span className="g-chip">作用域 · {data.scope.join('，')}</span>}
      </div>
    </div>

    <div className="g-clauses">
      {clauses.map(c => (
        <div key={c.n} className="g-clause">
          <div className="g-clause-left">
            <p className="g-clause-n">规则 {c.n}</p>
            <p className="g-clause-text">{c.text}</p>
          </div>
          <div className="g-clause-right">
            <div className="g-rules">
              <div className="g-rule">
                <div className="g-rule-kv">
                  <span className="g-rule-key">{c.key}</span>
                </div>
                <StringList items={data[c.key] as string[]} empty={c.empty} />
              </div>
            </div>
          </div>
        </div>
      ))}
    </div>

    <div className="g-floor" style={{ marginTop: 24 }}>
      <div className="g-floor-head">全局配置（不可覆盖）</div>
      <div className="g-floor-rules">
        <div className="g-floor-rule">
          <RuleBlock rules={[{ key: 'max_attempts', value: String(data.max_attempts), strong: true }]} />
        </div>
      </div>
    </div>
    {data.yaml && <div className="g-yaml-band">
      <h3>编译后的策略 · YAML</h3>
      <pre className="g-yaml-pre">{data.yaml.split('\n').filter(Boolean).map((line, i) => {
        const cls = lineClass(line);
        return <span key={i} className={`g-yl${cls ? ` ${cls}` : ''}`}>{line}</span>;
      })}</pre>
    </div>}
  </div>;
}

export function PolicyPage() {
  const route = useRoute();
  const id = route.id;

  const entry = useAsync<RunRecord[]>(() => id ? Promise.resolve([]) : api.runs(), [id]);

  const { data: policy, error, loading, reload } = useAsync(
    () => id ? api.policy(id) : Promise.resolve(null), [id]);

  if (!id) {
    return <RunPicker page="policy" runs={entry.data} loading={entry.loading} error={entry.error} onRetry={entry.reload} />;
  }

  if (loading) return <Loading />;
  if (error) return <Offline message={error} onRetry={reload} />;
  if (!policy) return <Offline message="找不到策略数据" onRetry={reload} />;

  return <PolicyShell data={policy} runId={id} />;
}
