import { useState } from 'react';
import type { CSSProperties } from 'react';
import {
  BookOpenCheck, Bug, ChevronDown, ChevronRight, Clock, FileDiff, GitCommitHorizontal, Hash, Lock,
  MessageSquareQuote, Play, RotateCcw, Route, ScanSearch, ShieldCheck, Timer, TriangleAlert, Wifi,
} from 'lucide-react';
import { api, type RunEvent, type RunRecord } from '../api';
import { CHECKS, SCENARIO, VERDICT, attemptsOf, eventOf, pct, secs, stageName, StageIcon, verdictOf, when } from '../model';
import { Loading, Mark, Offline, Section, Stamp, href, useAsync } from '../ui';
import { describe, toneOf } from './eventText';
import { Deliverables } from './Deliverables';

export function RunDetail({ run, fresh }: { run: RunRecord; fresh: boolean }) {
  const events = useAsync(() => api.events(run.id), [run.id]);
  const verdict = verdictOf(run.conclusion);
  const v = VERDICT[verdict];
  const task = run.task;
  const ScenarioGlyph = task ? SCENARIO[task.scenario].icon : null;

  return <article className="record">
    <header className="record-head">
      <div>
        {task && <span className="record-kind">{ScenarioGlyph && <ScenarioGlyph size={12} />} {SCENARIO[task.scenario].name}</span>}
        <h1>{run.title}</h1>
        <dl className="facts">
          <div><dt><GitCommitHorizontal size={11} /> 仓库</dt><dd>{run.repo}</dd></div>
          <div><dt><Hash size={11} /> 基线 commit</dt><dd><code>{run.commit}</code></dd></div>
          <div><dt><Timer size={11} /> 耗时</dt><dd>{secs(run.runtime_sec)}</dd></div>
          <div><dt><RotateCcw size={11} /> 尝试</dt><dd>{run.attempt} 次</dd></div>
          <div><dt><FileDiff size={11} /> 证据完整度</dt><dd>{pct(run.metrics?.evidence_completeness)}</dd></div>
          <div><dt><Clock size={11} /> 完成于</dt><dd>{when(run.updated_at)}</dd></div>
        </dl>
      </div>
      <div className="record-verdict">
        <Stamp verdict={verdict} size="lg" fresh={fresh} />
        <p>{v.note}</p>
        <nav className="record-links" aria-label="运行复核视图">
          <a href={href({ page: 'inspect', id: run.id })}><ScanSearch size={13} />仓库体检</a>
          <a href={href({ page: 'policy', id: run.id })}><BookOpenCheck size={13} />生效策略</a>
        </nav>
      </div>
    </header>

    {task && <Section icon={MessageSquareQuote} title="原始 Issue" note={`commit ${task.commit?.slice(0, 8) ?? '—'}`}>
      <blockquote className="issue-quote"><MessageSquareQuote size={15} /><span>{task.issue_body}</span></blockquote>
      <dl className="policy">
        <div><dt><Play size={11} /> 测试命令</dt><dd><code>{task.test_command}</code></dd></div>
        <div><dt><Lock size={11} /> 允许写入</dt><dd>{(task.risk_policy?.allowed_paths || []).map((p) => <code key={p}>{p}</code>)}</dd></div>
        <div><dt><Wifi size={11} /> 网络</dt><dd>{task.risk_policy?.network ? '允许' : '禁用'}</dd></div>
        <div><dt><Timer size={11} /> 时间上限</dt><dd>{task.risk_policy?.max_runtime_sec} 秒</dd></div>
      </dl>
    </Section>}

    {events.loading ? <Loading label="正在读取执行轨迹" />
      : events.error ? <Offline message={events.error} onRetry={events.reload} />
      : <>
        <Checks events={events.data!} conclusion={run.conclusion} />
        <Timeline events={events.data!} />
        <Deliverables run={run} />
        <a className="case-link" href={href({ page: 'routing', id: run.id })}><Route size={14} /> 查看这次运行的技能路由决策</a>
      </>}
  </article>;
}

function Checks({ events, conclusion }: { events: RunEvent[]; conclusion: RunRecord['conclusion'] }) {
  const attempts = attemptsOf(events);
  if (!attempts.length) return null;
  const last = attempts[attempts.length - 1];
  const blocked = CHECKS.filter((c) => attempts.some((a) => a.checks[c.key] === false));
  const failedFirst = attempts.slice(0, -1).find((a) => Object.values(a.checks).some((ok) => ok === false));

  return <Section
    icon={ShieldCheck}
    title="验证门禁"
    note={attempts.length > 1 ? `第 ${attempts.length} 次通过` : '一次通过'}
    sub={attempts.length > 1
      ? `共验证 ${attempts.length} 次，逐列对比可以看到失败在哪一项被修复。`
      : `六项检查全部通过后才允许交付。最终结论：${VERDICT[verdictOf(conclusion)].label}。`}
  >
    <div className="gate-table" role="table" aria-label="验证检查结果" style={{ '--cols': attempts.length } as CSSProperties}>
      <div className="gate-row gate-head" role="row">
        <span role="columnheader">检查项</span>
        {attempts.map((a) => <span role="columnheader" key={a.attempt}>第 {a.attempt} 次</span>)}
      </div>
      {CHECKS.map((c) => {
        const bad = attempts.some((a) => a.checks[c.key] === false);
        return <div className={`gate-row ${bad ? 'blocked' : ''}`} role="row" key={c.key}>
          <span className="gate-name" role="rowheader">
            <c.icon size={15} />
            <span><strong>{c.name}</strong><small>{c.hint}</small></span>
          </span>
          {attempts.map((a) => <span role="cell" key={a.attempt}><Mark ok={a.checks[c.key]} /></span>)}
        </div>;
      })}
    </div>
    {blocked.length > 0 && failedFirst?.failure && <p className="gate-note">
      <TriangleAlert size={12} />
      第 {failedFirst.attempt} 次验证在「{blocked[0].name}」被拦下（{failedFirst.failure.failure_class}），触发诊断、回滚与重试。
    </p>}
  </Section>;
}

function Timeline({ events }: { events: RunEvent[] }) {
  const [open, setOpen] = useState<string | null>(null);
  const t0 = events[0]?.ts ?? 0;
  const plan = eventOf(events, 'model_plan');
  return <Section icon={Bug} title="执行轨迹" note={`${events.length} 条事件`} sub="每条事件都写入证据库，可展开查看原始记录。">
    <ol className="track">
      {events.map((e) => {
        const isOpen = open === e.event_id;
        return <li key={e.event_id} className={`tk tk-${toneOf(e)}`}>
          <span className="tk-time">+{(e.ts - t0).toFixed(1)}s</span>
          <span className="tk-node"><span className="tk-icon"><StageIcon kind={e.kind} size={12} /></span></span>
          <div className="tk-body">
            <button className="tk-toggle" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : e.event_id)}>
              <span className="tk-title">{stageName(e.kind)}</span>
              {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </button>
            <p>{describe(e)}</p>
            {e === plan && e.data?.risk_flags?.length > 0 && <p className="tk-flags"><TriangleAlert size={12} /> 模型提示的风险：{e.data.risk_flags.join('；')}</p>}
            {isOpen && <pre className="code" tabIndex={0}>{JSON.stringify(e.data, null, 2)}</pre>}
          </div>
        </li>;
      })}
    </ol>
  </Section>;
}
