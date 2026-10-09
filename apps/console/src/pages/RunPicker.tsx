import { Activity, ArrowRight, BookOpenCheck, ScanSearch } from 'lucide-react';
import type { RunRecord } from '../api';
import { SCENARIO, secs, verdictOf, when } from '../model';
import { Empty, Loading, Offline, go, href } from '../ui';

/** Entry state for pages that need a concrete, sealed run.
 *
 * Inspect and Policy intentionally have no synthetic default. A reviewer must
 * choose a run that exists in the evidence store before seeing a dossier.
 */
export function RunPicker({
  page,
  runs,
  loading,
  error,
  onRetry,
}: {
  page: 'inspect' | 'policy';
  runs?: RunRecord[];
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  const isInspect = page === 'inspect';
  const title = isInspect ? '仓库体检' : '策略';
  const Icon = isInspect ? ScanSearch : BookOpenCheck;

  if (loading && !runs) return <Loading label="正在读取真实运行记录" />;
  if (error) return <Offline message={error} onRetry={onRetry} />;
  if (!runs?.length) {
    return <div className="g-entry-empty">
      <Empty>还没有可复核的运行记录。</Empty>
      <a className="btn btn-primary" href={href({ page: 'runs' })}><Activity size={14} />先运行一个任务</a>
    </div>;
  }

  return <div className="g-entry" aria-labelledby="run-entry-title">
    <header className="g-entry-head">
      <span className="g-entry-kicker"><Icon size={14} />真实运行入口</span>
      <h1 id="run-entry-title">{title}</h1>
      <p>{isInspect
        ? '仓库体检只展示证据库中已经完成或正在执行的运行，不使用虚构样张。请选择一条记录查看六项检查和执行轨迹。'
        : '策略页展示某次运行实际加载的策略。请选择一条记录，查看它的允许路径、敏感模式和必需检查。'}</p>
    </header>

    <section className="g-run-chooser" aria-label="选择运行记录">
      <div className="g-run-chooser-head">
        <div><strong>选择一条运行记录</strong><span>{runs.length} 条记录，按最近更新时间排列</span></div>
        <a className="btn" href={href({ page: 'runs' })}>打开运行记录 <ArrowRight size={13} /></a>
      </div>
      <ol className="g-run-options">
        {runs.map((run, index) => {
          const scenario = run.task?.scenario;
          const ScenarioIcon = scenario ? SCENARIO[scenario].icon : Activity;
          const verdict = verdictOf(run.conclusion);
          return <li key={run.id}>
            <button className={`g-run-option${index === 0 ? ' g-run-option-recommended' : ''}`} onClick={() => go({ page, id: run.id })}>
              <span className="g-run-option-icon"><ScenarioIcon size={14} /></span>
              <span className="g-run-option-copy">
                <strong>{run.title || run.run_id}</strong>
                <span>{run.repo} · {scenario ? SCENARIO[scenario].name : '本地运行'} · {when(run.updated_at)}</span>
              </span>
              <span role="img" className={`g-run-option-verdict dot dot-${verdict}`} aria-label={`结论：${verdict}`} />
              <span className="g-run-option-meta">{secs(run.runtime_sec)}<ArrowRight size={14} /></span>
            </button>
          </li>;
        })}
      </ol>
    </section>

    <p className="g-entry-foot">数据来自本地 PatchPilot API；运行记录本身包含结论、事件和证据引用。</p>
  </div>;
}
