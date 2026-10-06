import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Activity, BookOpenCheck, ChartColumn, Play, ScanSearch, Terminal } from 'lucide-react';
import { api, type RunRecord } from './api';
import { useAsync, useRoute, href, go, Loading, Offline } from './ui';
import { OverviewPage } from './pages/Overview';
import { RunsPage } from './pages/Runs';
import { RoutingPage } from './pages/Routing';
import { EvalPage } from './pages/Eval';
import { RunDialog } from './pages/RunDialog';
import { InspectPage } from './pages/Inspect';
import { PolicyPage } from './pages/Policy';
import './styles/tokens.css';
import './styles/base.css';
import './styles/pages.css';
import './styles/gate.css';

const TABS = [
  { page: 'inspect', name: '仓库体检', icon: ScanSearch },
  { page: 'overview', name: '总览', icon: ChartColumn },
  { page: 'runs', name: '运行记录', icon: Activity },
  { page: 'policy', name: '策略', icon: BookOpenCheck },
  { page: 'eval', name: '对照评测', icon: Play },
] as const;

function App() {
  const route = useRoute();
  const runs = useAsync(() => api.runs(), []);
  const [dialog, setDialog] = useState(false);
  const [freshRun, setFreshRun] = useState<string | null>(null);

  const onFinished = (run: RunRecord) => {
    setDialog(false);
    setFreshRun(run.id);
    runs.reload();
    go({ page: 'runs', id: run.id });
  };

  const online = !runs.error;
  return <div className="shell">
    <header className="topbar">
      <a className="brand" href={href({ page: 'overview' })} aria-label="PatchPilot 总览">
        <span className="brand-mark" aria-hidden><Terminal size={17} /></span>
        <span className="brand-text">
          <span className="brand-name">PatchPilot</span>
          <span className="brand-sub">可信的开源维护流水线</span>
        </span>
      </a>
      <nav className="tabs" aria-label="主导航">
        {TABS.map((t) => <a key={t.page} href={href({ page: t.page })} className={route.page === t.page ? 'tab on' : 'tab'} aria-current={route.page === t.page ? 'page' : undefined}>
          <t.icon size={15} />{t.name}
        </a>)}
      </nav>
      <div className="topbar-end">
        <span className={`conn ${online ? 'conn-on' : 'conn-off'}`}>
          <Activity size={14} />{runs.loading ? '连接中' : online ? '服务已连接' : '服务未连接'}
        </span>
        <button className="btn btn-primary" onClick={() => setDialog(true)} disabled={!online}><Play size={14} /> 运行一个任务</button>
      </div>
    </header>

    <main className="page" id="main">
      {route.page === 'inspect' && <InspectPage />}
      {route.page === 'policy' && <PolicyPage />}
      {route.page !== 'inspect' && route.page !== 'policy' && (
        runs.loading && !runs.data ? <Loading label="正在连接 PatchPilot 服务" />
        : runs.error ? <Offline message={runs.error} onRetry={runs.reload} />
        : <>
          {route.page === 'overview' && <OverviewPage runs={runs.data!} onRun={() => setDialog(true)} />}
          {route.page === 'runs' && <RunsPage runs={runs.data!} selectedId={route.id} freshId={freshRun} />}
          {route.page === 'routing' && <RoutingPage runs={runs.data!} selectedId={route.id} />}
          {route.page === 'eval' && <EvalPage />}
        </>
      )}
    </main>

    {dialog && <RunDialog onClose={() => setDialog(false)} onFinished={onFinished} />}
  </div>;
}

createRoot(document.getElementById('root')!).render(<App />);
