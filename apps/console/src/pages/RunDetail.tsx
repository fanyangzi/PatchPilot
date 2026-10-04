import { useState } from 'react';
import type { CSSProperties } from 'react';
import { ChevronDown, ChevronRight, ShieldCheck } from 'lucide-react';
import { api, type RunEvent, type RunRecord } from '../api';
import { CHECKS, SCENARIO, VERDICT, attemptsOf, eventOf, pct, secs, stageName, verdictOf, when } from '../model';
import { Loading, Mark, Offline, Stamp, href, useAsync } from '../ui';
import { describe, toneOf } from './eventText';
import { Deliverables } from './Deliverables';

export function RunDetail({ run, fresh }: { run: RunRecord; fresh: boolean }) {
  const events = useAsync(() => api.events(run.id), [run.id]);
  const verdict = verdictOf(run.conclusion);
  const v = VERDICT[verdict];
  const task = run.task;

  return <article className="detail">
    <header className="detail-head">
      <div className="detail-title">
        {task && <span className="case-kind">{SCENARIO[task.scenario].name}</span>}
        <h1>{run.title}</h1>
        <dl className="facts">
          <div><dt>仓库</dt><dd>{run.repo}</dd></div>
          <div><dt>基线</dt><dd><code>{run.commit}</code></dd></div>
          <div><dt>耗时</dt><dd>{secs(run.runtime_sec)}</dd></div>
          <div><dt>尝试</dt><dd>{run.attempt} 次</dd></div>
          <div><dt>证据完整度</dt><dd>{pct(run.metrics?.evidence_completeness)}</dd></div>
          <div><dt>完成于</dt><dd>{when(run.updated_at)}</dd></div>
        </dl>
      </div>
      <div className="detail-verdict">
        <Stamp verdict={verdict} size="lg" fresh={fresh} />
        <p>{v.note}</p>
      </div>
    </header>

    {task && <section className="issue">
      <h2 className="block-title">原始 Issue</h2>
      <blockquote>{task.issue_body}</blockquote>
      <dl className="policy">
        <div><dt>测试命令</dt><dd><code>{task.test_command}</code></dd></div>
        <div><dt>允许写入</dt><dd>{(task.risk_policy?.allowed_paths || []).map((p) => <code key={p}>{p}</code>)}</dd></div>
        <div><dt>网络</dt><dd>{task.risk_policy?.network ? '允许' : '禁用'}</dd></div>
        <div><dt>时间上限</dt><dd>{task.risk_policy?.max_runtime_sec} 秒</dd></div>
      </dl>
    </section>}

    {events.loading ? <Loading label="正在读取执行轨迹" />
      : events.error ? <Offline message={events.error} onRetry={events.reload} />
      : <>
        <Checks events={events.data!} />
        <Timeline events={events.data!} />
        <Deliverables run={run} />
        <a className="case-link" href={href({ page: 'routing', id: run.id })}><ShieldCheck size={14} /> 查看这次运行的技能路由决策</a>
      </>}
  </article>;
}

function Checks({ events }: { events: RunEvent[] }) {
  const attempts = attemptsOf(events);
  if (!attempts.length) return null;
  return <section>
    <h2 className="block-title">验证门禁</h2>
    <p className="block-sub">{attempts.length > 1 ? `共验证 ${attempts.length} 次。逐次对比可以看到失败在哪一项被修复。` : '六项检查决定补丁能否交付。'}</p>
    <div className="checks-table" role="table" aria-label="验证检查结果" style={{ '--cols': attempts.length } as CSSProperties}>
      <div className="checks-row checks-headrow" role="row">
        <span role="columnheader">检查项</span>
        {attempts.map((a) => <span role="columnheader" key={a.attempt}>第 {a.attempt} 次</span>)}
      </div>
      {CHECKS.map((c) => <div className="checks-row" role="row" key={c.key}>
        <span role="rowheader"><strong>{c.name}</strong><small>{c.hint}</small></span>
        {attempts.map((a) => <span role="cell" key={a.attempt}><Mark ok={a.checks[c.key]} /></span>)}
      </div>)}
    </div>
  </section>;
}

function Timeline({ events }: { events: RunEvent[] }) {
  const [open, setOpen] = useState<string | null>(null);
  const t0 = events[0]?.ts ?? 0;
  const plan = eventOf(events, 'model_plan');
  return <section>
    <h2 className="block-title">执行轨迹</h2>
    <p className="block-sub">共 {events.length} 条事件，每条都写入证据库，可展开查看原始记录。</p>
    <ol className="timeline">
      {events.map((e) => {
        const isOpen = open === e.event_id;
        return <li key={e.event_id} className={`tl tl-${toneOf(e)}`}>
          <span className="tl-time">+{(e.ts - t0).toFixed(1)}s</span>
          <span className="tl-dot" aria-hidden />
          <div className="tl-body">
            <button className="tl-toggle" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : e.event_id)}>
              <strong>{stageName(e.kind)}</strong>
              {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </button>
            <p>{describe(e)}</p>
            {e === plan && e.data?.risk_flags?.length > 0 && <p className="tl-flags">模型提示的风险：{e.data.risk_flags.join('；')}</p>}
            {isOpen && <pre className="code raw" tabIndex={0}>{JSON.stringify(e.data, null, 2)}</pre>}
          </div>
        </li>;
      })}
    </ol>
  </section>;
}
