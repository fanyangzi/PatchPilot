import { useState } from 'react';
import type { RunRecord, Scenario } from '../api';
import { SCENARIO, secs, verdictOf, when } from '../model';
import { Empty, href } from '../ui';
import { RunDetail } from './RunDetail';

type Filter = 'all' | Scenario;

export function RunsPage({ runs, selectedId, freshId }: { runs: RunRecord[]; selectedId?: string; freshId: string | null }) {
  const [filter, setFilter] = useState<Filter>('all');
  const visible = runs.filter((r) => filter === 'all' || r.task?.scenario === filter);
  const selected = runs.find((r) => r.id === selectedId) ?? visible[0];
  const count = (f: Filter) => f === 'all' ? runs.length : runs.filter((r) => r.task?.scenario === f).length;

  return <div className="runs">
    <aside className="runs-list" aria-label="运行记录列表">
      <div className="filters" role="group" aria-label="按场景筛选">
        {(['all', 'normal', 'failure', 'risk'] as Filter[]).map((f) =>
          <button key={f} className={filter === f ? 'chip on' : 'chip'} aria-pressed={filter === f} onClick={() => setFilter(f)}>
            {f === 'all' ? '全部' : SCENARIO[f].name}<span>{count(f)}</span>
          </button>)}
      </div>
      {visible.length === 0 ? <Empty>这个场景还没有运行记录。</Empty> :
        <ol className="run-items">
          {visible.map((r) => {
            const v = verdictOf(r.conclusion);
            return <li key={r.id}>
              <a href={href({ page: 'runs', id: r.id })} className={`run-item ${selected?.id === r.id ? 'on' : ''}`} aria-current={selected?.id === r.id ? 'true' : undefined}>
                <span className={`dot dot-${v}`} aria-hidden />
                <span className="run-item-main">
                  <strong>{r.title}</strong>
                  <span>{r.task ? SCENARIO[r.task.scenario].name : '本地运行'}，{r.attempt > 1 ? `重试 ${r.attempt - 1} 次` : '一次完成'}，{secs(r.runtime_sec)}</span>
                </span>
                <time>{when(r.updated_at)}</time>
              </a>
            </li>;
          })}
        </ol>}
    </aside>
    <section className="runs-detail">
      {selected ? <RunDetail key={selected.id} run={selected} fresh={selected.id === freshId} /> : <Empty>选择左侧的一条运行记录。</Empty>}
    </section>
  </div>;
}
