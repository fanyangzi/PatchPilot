import { SampleNote, Loading, Offline, useAsync, useRoute } from '../ui';
import { api } from '../api';
import type { PolicyData } from '../api';
import { POLICY, type PolicyRule } from '../samples';

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
  </div>;
}

export function PolicyPage() {
  const route = useRoute();
  const id = route.id;

  const { data: policy, error, loading, reload } = useAsync(
    () => id ? api.policy(id) : Promise.resolve(null), [id]);

  if (!id) {
    const p = POLICY;
    return <div>
      <SampleNote>模拟数据，来自假设仓库的 AI_POLICY.md。从运行记录点进来即读取真实策略。</SampleNote>

      <div className="g-policy-head">
        <h1 className="g-policy-title">{p.source}</h1>
        <p className="g-policy-preamble">{p.preamble}</p>
        <p className="g-policy-preamble" style={{ fontSize: 'var(--t-xs)', color: 'var(--ink-3)', marginTop: 4 }}>
          来源：{p.repo} @ {p.sourceCommit} · 编译于 {p.compiledAt} · 生效分支：{p.scope}
        </p>
      </div>

      <div className="g-clauses">
        {p.clauses.map((c) => (
          <div key={c.n} className="g-clause">
            <div className="g-clause-left">
              <p className="g-clause-n">第 {c.n} 条</p>
              <p className="g-clause-text">{c.text}</p>
              {c.skipped && <p className="g-clause-skipped">{c.skipped}</p>}
            </div>
            <div className="g-clause-right">
              {c.rules.length > 0
                ? <RuleBlock rules={c.rules} />
                : <p style={{ fontSize: 'var(--t-xs)', color: 'var(--ink-3)', margin: 0 }}>无可执行检查</p>}
            </div>
          </div>
        ))}
      </div>

      <div className="g-floor">
        <div className="g-floor-head">固定规则（所有仓库，不可覆盖）</div>
        <div className="g-floor-rules">
          {p.floor.map((r) => (
            <div key={r.key} className="g-floor-rule">
              <RuleBlock rules={[r]} />
            </div>
          ))}
        </div>
      </div>

      <div className="g-yaml-band">
        <h3>编译后的策略 · YAML</h3>
        <pre className="g-yaml-pre">
          {p.yaml.map((line, i) => {
            const cls = lineClass(line);
            return <span key={i} className={`g-yl${cls ? ` ${cls}` : ''}`}>{line}</span>;
          })}
        </pre>
      </div>
    </div>;
  }

  if (loading) return <Loading />;
  if (error) return <Offline message={error} onRetry={reload} />;
  if (!policy) return <Offline message="找不到策略数据" onRetry={reload} />;

  return <PolicyShell data={policy} runId={id} />;
}
