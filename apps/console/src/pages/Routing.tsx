import { api, type RunRecord } from '../api';
import { SCENARIO, SKILL, eventOf, when } from '../model';
import { Empty, Loading, Meter, Offline, go, useAsync } from '../ui';

const RISK = { low: '低', medium: '中', high: '高' } as Record<string, string>;
const TOOL: Record<string, string> = { 'repo.read': '只读仓库', 'repo.write': '写入仓库', 'harness.run': '沙箱执行', 'trace.read': '读取轨迹', 'artifact.write': '写入证据', 'git.write': '写入 Git' };

export function RoutingPage({ runs, selectedId }: { runs: RunRecord[]; selectedId?: string }) {
  const run = runs.find((r) => r.id === selectedId) ?? runs[0];
  const events = useAsync(() => run ? api.events(run.id) : Promise.resolve([]), [run?.id]);
  if (!run) return <Empty>还没有运行记录。</Empty>;

  const route = events.data && eventOf(events.data, 'route');
  const plan = events.data && eventOf(events.data, 'model_plan');
  const nodes: any[] = route?.data?.nodes || [];
  const selected = new Set<string>(route?.data?.selected || []);
  const max = Math.max(0.0001, ...nodes.map((n) => n.score));

  return <div className="routing">
    <header className="routing-head">
      <div>
        <h1>技能路由</h1>
        <p>模型可以提建议，但哪些技能能执行由契约和评分决定。每个技能声明了前置条件、可用工具和风险等级，路由器只从中选出得分最高的 {selected.size || 5} 个。</p>
      </div>
      <label className="select">
        <span>查看运行</span>
        <select value={run.id} onChange={(e) => go({ page: 'routing', id: e.target.value })}>
          {runs.map((r) => <option key={r.id} value={r.id}>{r.title}（{r.task ? SCENARIO[r.task.scenario].name : '本地'}，{when(r.updated_at)}）</option>)}
        </select>
      </label>
    </header>

    <div className="formula" aria-label="评分公式">
      <span className="formula-label">得分</span>
      <span>= 0.35 × 相关度</span><span>+ 0.20 × 前置条件匹配</span><span>+ 0.15 × 证据增益</span><span>− 0.05 × 风险</span><span>− 0.05 × 成本</span><span>+ 0.13 基准</span>
    </div>

    {events.loading ? <Loading label="正在读取路由决策" /> : events.error ? <Offline message={events.error} onRetry={events.reload} /> : !nodes.length ? <Empty>这次运行没有记录路由决策。</Empty> :
      <div className="routing-grid">
        <section>
          <h2 className="block-title">{nodes.length} 个候选技能的评分</h2>
          <table className="skills">
            <thead><tr><th scope="col">技能</th><th scope="col" className="num">得分</th><th scope="col">相关度</th><th scope="col">证据增益</th><th scope="col">风险</th><th scope="col">可用工具</th></tr></thead>
            <tbody>
              {nodes.map((n, i) => <tr key={n.skill} className={selected.has(n.skill) ? 'picked' : ''}>
                <th scope="row">
                  <span className="rank">{i + 1}</span>
                  <span><strong>{SKILL[n.skill] || n.skill}</strong><code>{n.skill}</code></span>
                  {selected.has(n.skill) && <span className="picked-tag">已选入</span>}
                </th>
                <td className="num"><span className="score"><b>{n.score.toFixed(3)}</b><Meter value={n.score / max} tone={selected.has(n.skill) ? 'ink' : 'mute'} /></span></td>
                <td className="num">{Math.round(n.relevance * 100)}%</td>
                <td className="num">{Math.round(n.evidence_gain * 100)}%</td>
                <td><span className={`risk risk-${n.risk}`}>{RISK[n.risk] || n.risk}</span></td>
                <td className="tools">{(n.tools || []).map((t: string) => TOOL[t] || t).join('、')}</td>
              </tr>)}
            </tbody>
          </table>
        </section>

        <aside className="advisory">
          <h2 className="block-title">模型规划建议</h2>
          {!plan ? <p className="block-sub">这次运行关闭了远程模型规划，路由完全由规则决定。</p> : <>
            <p className="block-sub">由 <code>{plan.data.model}</code> 生成。原始回复不落盘，只保存摘要和哈希 <code>{plan.data.response_hash}</code>。</p>
            <blockquote>{plan.data.summary}</blockquote>
            {plan.data.suggested_skills?.length > 0 && <div className="adv-block">
              <h3>建议的技能</h3>
              <ul className="adv-skills">{plan.data.suggested_skills.map((s: string) => <li key={s} className={selected.has(s) ? 'agree' : ''}>{SKILL[s] || s}<span>{selected.has(s) ? '路由也选了' : '路由未选'}</span></li>)}</ul>
            </div>}
            {plan.data.risk_flags?.length > 0 && <div className="adv-block"><h3>提示的风险</h3><ul>{plan.data.risk_flags.map((f: string) => <li key={f}>{f}</li>)}</ul></div>}
            {plan.data.acceptance_checks?.length > 0 && <div className="adv-block"><h3>建议的验收条件</h3><ul>{plan.data.acceptance_checks.map((f: string) => <li key={f}>{f}</li>)}</ul></div>}
          </>}
        </aside>
      </div>}
  </div>;
}
