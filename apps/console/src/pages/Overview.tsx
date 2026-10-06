import { ArrowRight, ClipboardCheck, GitCommitHorizontal, ListChecks, Play, ScrollText, ShieldCheck } from 'lucide-react';
import { api, type RunEvent, type RunRecord, type Scenario } from '../api';
import { CHECKS, FAILURE_CLASS, METHOD, RECOVERY_ACTION, SCENARIO, STAGES, attemptsOf, eventOf, showcaseRuns, skillName, verdictOf } from '../model';
import { Loading, Mark, Offline, Section, Stamp, VerdictTag, href, useAsync } from '../ui';

export function OverviewPage({ runs, onRun }: { runs: RunRecord[]; onRun: () => void }) {
  const show = showcaseRuns(runs);
  const ids = [show.normal?.id, show.failure?.id, show.risk?.id];
  const events = useAsync(() => Promise.all(ids.map((id) => id ? api.events(id) : Promise.resolve([] as RunEvent[]))), ids);
  const evalData = useAsync(() => api.evalSummary(), []);

  return <div>
    <section className="hero">
      <div className="hero-main">
        <span className="hero-eyebrow"><ShieldCheck size={14} /> 策略约束下的维护运行时</span>
        <h1 className="hero-title">一个 Issue 进来，要么带着证据放行，要么停下等人复核。</h1>
        <p className="hero-lead">
          PatchPilot 在隔离沙箱里复现问题、生成补丁、跑完测试和策略检查，再把每一步的证据封存成一份可逐条核对的记录。
          它<b>从不自动合并</b>——结论只有两个方向：放行给维护者审阅，或者拦下来等人决定。
        </p>
        <div className="hero-actions">
          <button className="btn btn-primary btn-lg" onClick={onRun}><Play size={15} /> 运行一个任务</button>
          <a className="btn btn-lg" href={href({ page: 'eval' })}><ArrowRight size={15} /> 查看对照评测</a>
        </div>
      </div>

      <div className="hero-spec">
        <h2>这份记录里有什么</h2>
        <dl>
          <div className="spec-row"><dt><ClipboardCheck size={13} /> 验证检查</dt><dd>6<em>项</em></dd></div>
          <div className="spec-row"><dt><GitCommitHorizontal size={13} /> 流水线阶段</dt><dd>{STAGES.length}<em>步</em></dd></div>
          <div className="spec-row good"><dt><ShieldCheck size={13} /> 越界交付</dt><dd>0<em>次</em></dd></div>
          <div className="spec-row"><dt><ScrollText size={13} /> 已归档运行</dt><dd>{runs.length}<em>次</em></dd></div>
        </dl>
      </div>
    </section>

    <Section icon={ListChecks} title="每次运行都走完这九步" note="顺序不可跳过" className="rail-section">
      <div className="rail">
        <div className="rail-head">
          <ListChecks size={15} />
          <h2>处理流水线</h2>
          <span className="sec-note">第 8 步是唯一的门禁</span>
        </div>
        <ol className="rail-list">
          {STAGES.map((s, i) => <li key={s.kind} className={s.kind === 'verification' ? 'rail-step gate' : 'rail-step'}>
            <span className="rail-step-top"><s.icon size={15} /><span className="rail-num">{String(i + 1).padStart(2, '0')}</span></span>
            <span className="rail-name">{s.name}</span>
            <span className="rail-note">{s.note}</span>
          </li>)}
        </ol>
      </div>
    </Section>

    <Section icon={ClipboardCheck} title="三种结局，都来自真实运行记录" note="点击查看完整轨迹">
      {events.loading ? <Loading label="正在读取场景运行记录" />
        : events.error ? <Offline message={events.error} onRetry={events.reload} />
        : <div className="cases">
            {(['normal', 'failure', 'risk'] as Scenario[]).map((s, i) => <CaseCard key={s} scenario={s} run={show[s]} events={events.data?.[i]} />)}
          </div>}
    </Section>

    {evalData.data && <EvalBand summary={evalData.data} />}
  </div>;
}

function CaseCard({ scenario, run, events }: { scenario: Scenario; run?: RunRecord; events?: RunEvent[] }) {
  const meta = SCENARIO[scenario];
  const Icon = meta.icon;
  if (!run) return <article className="case case-empty">
    <div className="case-head"><div><span className="case-kind"><Icon size={12} /> {meta.name}</span><h3>还没有这个场景的运行记录</h3></div></div>
    <p className="case-promise">{meta.promise}</p>
  </article>;

  const verdict = verdictOf(run.conclusion);
  const attempts = events ? attemptsOf(events) : [];
  const last = attempts[attempts.length - 1];
  const files: string[] = events ? eventOf(events, 'localization')?.data?.files || [] : [];
  const routed: string[] = events ? eventOf(events, 'route')?.data?.selected || [] : [];

  return <article className={`case case-${verdict}`}>
    <header className="case-head">
      <div>
        <span className="case-kind"><Icon size={12} /> {meta.name}</span>
        <h3>{run.title}</h3>
      </div>
      <Stamp verdict={verdict} size="md" />
    </header>
    <p className="case-promise">{meta.promise}</p>

    {scenario === 'failure' && attempts.length > 1 && <div className="case-story">
      <div><span className="story-k">第 1 次验证</span><span className="story-v bad">{FAILURE_CLASS[attempts[0].failure?.failure_class || ''] || '失败'}</span></div>
      <div><span className="story-k">恢复动作</span><span className="story-v">{(eventOf(events!, 'recovery')?.data?.recovery_actions || []).map((a: string) => RECOVERY_ACTION[a] || a).join('，')}</span></div>
      <div><span className="story-k">第 {attempts.length} 次验证</span><span className="story-v ok">全部通过</span></div>
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

    {!attempts.length && routed.length > 0 && <div className="case-story">
      <div><span className="story-k">选入技能</span><span className="story-v">{routed.slice(0, 3).map(skillName).join('，')}</span></div>
    </div>}

    {last && <ul className="case-checks" aria-label="最终检查结果">
      {CHECKS.map((c) => <li key={c.key} title={c.hint}><Mark ok={last.checks[c.key]} />{c.name}</li>)}
    </ul>}

    <a className="case-link" href={href({ page: 'runs', id: run.id })}>查看完整轨迹 <ArrowRight size={14} /></a>
  </article>;
}

function EvalBand({ summary }: { summary: import('../api').EvalSummary }) {
  const unsafe = (m: string) => summary.rows.filter((r) => r.method === m && r.status === 'UNSAFE_DELIVERY').length;
  const risky = summary.rows.filter((r) => r.method === 'patchpilot_full' && r.scenario === 'risk').length;
  return <section className="band">
    <div className="band-copy">
      <h2>在同样 {summary.task_count} 个任务上，只有 PatchPilot 没有放出越界补丁。</h2>
      <p>
        直接生成和线性 Agent 各有 {unsafe('direct_llm')} 个、{unsafe('linear_agent')} 个越界补丁被当作成果交付；
        PatchPilot 把 {risky} 个风险任务全部拦下，交由维护者复核。这不是更高的自动化程度，而是更少的失控面。
      </p>
      <a className="sec-link" href={href({ page: 'eval' })}>查看逐任务对照 <ArrowRight size={14} /></a>
    </div>
    <dl className="band-nums">
      {(['direct_llm', 'linear_agent', 'patchpilot_full'] as const).map((m) => {
        const bad = unsafe(m) > 0;
        return <div key={m} className={`band-row ${m === 'patchpilot_full' ? 'us' : ''} ${bad ? 'bad' : ''}`}>
          <dt><MethodIcon method={m} /> {METHOD[m].name}</dt>
          <dd>{bad ? <VerdictTag verdict="unsafe" size={13} /> : null}<b>{unsafe(m)}</b> 个越界交付</dd>
        </div>;
      })}
    </dl>
  </section>;
}

function MethodIcon({ method }: { method: string }) {
  const Icon = METHOD[method].icon;
  return <Icon size={13} />;
}
