import { ArrowRight, Play } from 'lucide-react';
import { api, type RunEvent, type RunRecord, type Scenario } from '../api';
import { CHECKS, FAILURE_CLASS, RECOVERY_ACTION, SCENARIO, STAGES, attemptsOf, eventOf, showcaseRuns, verdictOf } from '../model';
import { Mark, Stamp, href, useAsync } from '../ui';

export function OverviewPage({ runs, onRun }: { runs: RunRecord[]; onRun: () => void }) {
  const show = showcaseRuns(runs);
  const ids = [show.normal?.id, show.failure?.id, show.risk?.id];
  const events = useAsync(() => Promise.all(ids.map((id) => id ? api.events(id) : Promise.resolve([] as RunEvent[]))), ids);
  const evalData = useAsync(() => api.evalSummary(), []);

  return <div className="overview">
    <section className="hero">
      <h1 className="hero-title">
        <span>一个 Issue 进来，</span>
        <span>要么带着证据放行，</span>
        <span>要么停下等人复核。</span>
      </h1>
      <div className="hero-side">
        <p>PatchPilot 在隔离沙箱里复现问题、生成补丁、跑完测试和策略检查，再把每一步的证据封存起来。它从不自动合并：维护者看到的是一份可以逐条核对的交付记录。</p>
        <div className="hero-actions">
          <button className="btn btn-primary btn-lg" onClick={onRun}><Play size={15} /> 运行一个任务</button>
          <a className="btn btn-lg" href={href({ page: 'eval' })}>查看对照评测</a>
        </div>
      </div>
    </section>

    <section className="pipeline" aria-label="处理流程">
      <h2 className="section-title">每次运行都走完这九步</h2>
      <ol className="pipeline-steps">
        {STAGES.map((s, i) => <li key={s.kind} className={s.kind === 'verification' ? 'gate' : ''}>
          <span className="pipeline-num">{i + 1}</span>
          <span className="pipeline-name">{s.name}</span>
          {s.kind === 'verification' && <span className="pipeline-note">失败则诊断、回滚、重试；越界则拦截</span>}
        </li>)}
      </ol>
    </section>

    <section className="cases" aria-label="三种结局">
      <h2 className="section-title">三种结局，都来自真实运行记录</h2>
      <div className="case-grid">
        {(['normal', 'failure', 'risk'] as Scenario[]).map((s, i) => <CaseCard key={s} scenario={s} run={show[s]} events={events.data?.[i]} />)}
      </div>
    </section>

    {evalData.data && <EvalTeaser summary={evalData.data} />}
  </div>;
}

function CaseCard({ scenario, run, events }: { scenario: Scenario; run?: RunRecord; events?: RunEvent[] }) {
  const meta = SCENARIO[scenario];
  if (!run) return <article className="case case-empty"><h3>{meta.name}</h3><p>还没有这个场景的运行记录。</p></article>;
  const verdict = verdictOf(run.conclusion);
  const attempts = events ? attemptsOf(events) : [];
  const last = attempts[attempts.length - 1];
  const files: string[] = events ? eventOf(events, 'localization')?.data?.files || [] : [];

  return <article className={`case case-${verdict}`}>
    <header className="case-head">
      <div>
        <span className="case-kind">{meta.name}</span>
        <h3>{run.title}</h3>
      </div>
      <Stamp verdict={verdict} size="md" />
    </header>
    <p className="case-promise">{meta.promise}</p>

    {scenario === 'failure' && attempts.length > 1 && <div className="case-story">
      <div><span className="story-k">第 1 次验证</span><span className="story-v bad">{FAILURE_CLASS[attempts[0].failure?.failure_class || ''] || '失败'}</span></div>
      <div><span className="story-k">恢复动作</span><span className="story-v">{(eventOf(events!, 'recovery')?.data?.recovery_actions || []).map((a: string) => RECOVERY_ACTION[a] || a).join('，')}</span></div>
      <div><span className="story-k">第 {attempts.length} 次验证</span><span className="story-v ok">通过</span></div>
    </div>}

    {scenario === 'risk' && <div className="case-story">
      <div><span className="story-k">触碰文件</span><code className="story-v">{files.join('，') || '—'}</code></div>
      <div><span className="story-k">允许范围</span><code className="story-v">{(run.task?.risk_policy?.allowed_paths || []).join('，')}</code></div>
      <div><span className="story-k">处理</span><span className="story-v bad">不生成可合并 PR</span></div>
    </div>}

    {scenario === 'normal' && <div className="case-story">
      <div><span className="story-k">修改文件</span><code className="story-v">{files.join('，') || '—'}</code></div>
      <div><span className="story-k">验证次数</span><span className="story-v">{attempts.length || run.attempt} 次</span></div>
      <div><span className="story-k">基线 commit</span><code className="story-v">{run.commit}</code></div>
    </div>}

    {last && <ul className="case-checks" aria-label="最终检查结果">
      {CHECKS.map((c) => <li key={c.key} title={c.hint}><Mark ok={last.checks[c.key]} />{c.name}</li>)}
    </ul>}

    <a className="case-link" href={href({ page: 'runs', id: run.id })}>查看完整轨迹 <ArrowRight size={14} /></a>
  </article>;
}

function EvalTeaser({ summary }: { summary: import('../api').EvalSummary }) {
  const unsafe = (m: string) => summary.rows.filter((r) => r.method === m && r.status === 'UNSAFE_DELIVERY').length;
  const risky = summary.rows.filter((r) => r.method === 'patchpilot_full' && r.scenario === 'risk').length;
  return <section className="teaser">
    <div className="teaser-copy">
      <h2>在同样 {summary.task_count} 个任务上，只有 PatchPilot 没有放出越界补丁。</h2>
      <p>直接生成和线性 Agent 各有 {unsafe('direct_llm')} 个、{unsafe('linear_agent')} 个越界补丁被交付；PatchPilot 把 {risky} 个风险任务全部拦下，交由维护者复核。</p>
      <a className="case-link" href={href({ page: 'eval' })}>查看逐任务对照 <ArrowRight size={14} /></a>
    </div>
    <dl className="teaser-nums">
      {(['direct_llm', 'linear_agent', 'patchpilot_full'] as const).map((m) => <div key={m} className={m === 'patchpilot_full' ? 'us' : ''}>
        <dt>{m === 'direct_llm' ? '直接生成' : m === 'linear_agent' ? '线性 Agent' : 'PatchPilot'}</dt>
        <dd><b>{unsafe(m)}</b> 个越界交付</dd>
      </div>)}
    </dl>
  </section>;
}
