import { Calculator, CircleCheckBig, Route, ShieldAlert, ShieldCheck, Sparkles, Table2, TriangleAlert, Waypoints } from 'lucide-react';
import { api, type RunRecord } from '../api';
import { SCENARIO, SkillIcon, eventOf, skillName, when } from '../model';
import { Empty, Loading, Meter, Offline, go, useAsync } from '../ui';

const RISK: Record<string, { label: string; icon: typeof ShieldCheck }> = {
  low: { label: '低', icon: ShieldCheck },
  medium: { label: '中', icon: TriangleAlert },
  high: { label: '高', icon: ShieldAlert },
};
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
      <label className="picker">
        <span><Route size={11} /> 查看运行</span>
        <select value={run.id} onChange={(e) => go({ page: 'routing', id: e.target.value })}>
          {runs.map((r) => <option key={r.id} value={r.id}>{r.title}（{r.task ? SCENARIO[r.task.scenario].name : '本地'}，{when(r.updated_at)}）</option>)}
        </select>
      </label>
    </header>

    <div className="formula" aria-label="路由评分公式">
      <span className="formula-label"><Calculator size={14} /> 得分</span>
      <span>= 0.35 × 相关度</span><span>+ 0.20 × 前置条件匹配</span><span>+ 0.15 × 证据增益</span><span>− 0.05 × 风险</span><span>− 0.05 × 成本</span><span>+ 0.13 基准</span>
    </div>

    {events.loading ? <Loading label="正在读取路由决策" /> : events.error ? <Offline message={events.error} onRetry={events.reload} /> : !nodes.length ? <Empty>这次运行没有记录路由决策。</Empty> :
      <div className="routing-grid">
        <section className="panel">
          <div className="sec-head">
            <Table2 size={15} />
            <h2>候选技能评分</h2>
            <span className="sec-note">{selected.size} / {nodes.length} 选入</span>
          </div>
          <p className="sec-sub">评分由契约和权重算出，与模型的建议无关。绿底的行是路由器最终选入并执行的技能。</p>
          <div className="skills-scroll">
            <table className="skills">
            <thead><tr><th scope="col">技能</th><th scope="col" className="num">得分</th><th scope="col">相关度</th><th scope="col">证据增益</th><th scope="col">风险</th><th scope="col">可用工具</th></tr></thead>
            <tbody>
              {nodes.map((n, i) => {
                const picked = selected.has(n.skill);
                const risk = RISK[n.risk];
                const RiskIcon = risk?.icon;
                return <tr key={n.skill} className={picked ? 'picked' : ''}>
                  <th scope="row">
                    <span className="rank">{i + 1}</span>
                    <span className="skill-icon"><SkillIcon skill={n.skill} size={14} /></span>
                    <span><strong>{skillName(n.skill)}</strong><code>{n.skill}</code></span>
                    {picked && <span className="picked-tag"><CircleCheckBig size={12} /> 已选入</span>}
                  </th>
                  <td className="num"><span className="score"><b>{n.score.toFixed(3)}</b><Meter value={n.score / max} tone={picked ? 'ink' : 'mute'} /></span></td>
                  <td className="num">{Math.round(n.relevance * 100)}%</td>
                  <td className="num">{Math.round(n.evidence_gain * 100)}%</td>
                  <td><span className={`risk risk-${n.risk}`}>{RiskIcon && <RiskIcon size={11} />}{risk?.label ?? n.risk}</span></td>
                  <td className="tools">{(n.tools || []).map((t: string) => TOOL[t] || t).join('、')}</td>
                </tr>;
              })}
            </tbody>
            </table>
          </div>
        </section>

        <aside className="panel panel-aside">
          <div className="sec-head">
            <Sparkles size={15} />
            <h2>模型规划建议</h2>
          </div>
          {!plan ? <p className="sec-sub">这次运行关闭了远程模型规划，路由完全由规则决定。</p> : <>
            <p className="sec-sub">由 <code>{plan.data.model}</code> 生成。原始回复不落盘，只保存摘要。</p>
            <blockquote className="plan-quote">{plan.data.summary}</blockquote>
            {plan.data.suggested_skills?.length > 0 && <div className="adv-block">
              <h3><Waypoints size={13} /> 建议的技能</h3>
              <ul className="adv-skills">{plan.data.suggested_skills.map((s: string) => <li key={s} className={selected.has(s) ? 'agree' : ''}>{skillName(s)}<span>{selected.has(s) ? '路由也选了' : '路由未选'}</span></li>)}</ul>
            </div>}
            {plan.data.risk_flags?.length > 0 && <div className="adv-block">
              <h3><TriangleAlert size={13} /> 提示的风险</h3>
              <ul>{plan.data.risk_flags.map((f: string) => <li key={f}>{f}</li>)}</ul>
            </div>}
            {plan.data.acceptance_checks?.length > 0 && <div className="adv-block">
              <h3><CircleCheckBig size={13} /> 建议的验收条件</h3>
              <ul>{plan.data.acceptance_checks.map((f: string) => <li key={f}>{f}</li>)}</ul>
            </div>}
            <p className="adv-note"><ShieldCheck size={13} /> 建议只作为参考：最终执行哪几个技能，仍然由上面的评分表决定。</p>
          </>}
        </aside>
      </div>}
  </div>;
}
