import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Activity, BookOpenCheck, ChartColumn, ClipboardCheck, FileText, GitPullRequest, Play, ScanSearch, Terminal, MoreHorizontal } from 'lucide-react';
import { api, type RunRecord } from './api';
import { v1 } from './apiV1';
import { useAsync, useRoute, href, go, Loading, Offline } from './ui';
import { OverviewPage } from './pages/Overview';
import { RunsPage } from './pages/Runs';
import { RoutingPage } from './pages/Routing';
import { EvalPage } from './pages/Eval';
import { RunDialog } from './pages/RunDialog';
import { InspectPage } from './pages/Inspect';
import { PolicyPage } from './pages/Policy';
import { AcceptanceInboxPage, CodeReviewPage, CounterexamplePage, AcceptanceContractPage, DeliveryReportPage, IntakeDialog } from './pages/AcceptancePages';
import './styles/tokens.css';
import './styles/base.css';
import './styles/pages.css';
import './styles/gate.css';
import './styles/acceptance.css';

const PRIMARY_TABS = [
  { page: 'inbox', name: '验收队列', icon: ClipboardCheck },
  { page: 'review', name: '代码审查', icon: GitPullRequest },
  { page: 'investigation', name: '反例调查', icon: ScanSearch },
  { page: 'contract', name: '验收条件', icon: BookOpenCheck },
  { page: 'report', name: '交付报告', icon: FileText },
] as const;

/** Legacy evidence views remain deep-linkable, but the acceptance workspace is
 * the product's primary surface. Keeping these links out of the masthead
 * makes the first frame read like a maintainer inbox instead of a demo menu. */
const SECONDARY_TABS = [
  { page: 'inspect', name: '仓库体检', icon: ScanSearch },
  { page: 'overview', name: '总览', icon: ChartColumn },
  { page: 'runs', name: '运行记录', icon: Activity },
  { page: 'policy', name: '策略', icon: BookOpenCheck },
  { page: 'eval', name: '对照评测', icon: Play },
] as const;

function App() {
  const route = useRoute();
  const legacyPage = route.page === 'overview' || route.page === 'runs' || route.page === 'routing';
  const runs = useAsync(() => legacyPage ? api.runs() : Promise.resolve([] as RunRecord[]), [legacyPage]);
  const service = useAsync(() => v1.health(), []);
  const [dialog, setDialog] = useState(false);
  const [intakeDialog, setIntakeDialog] = useState(false);
  const [freshRun, setFreshRun] = useState<string | null>(null);

  const onFinished = (run: RunRecord) => {
    setDialog(false);
    setFreshRun(run.id);
    runs.reload();
    go({ page: 'runs', id: run.id });
  };

  const online = !service.error;
  return <div className="shell">
    <header className="topbar">
      <a className="brand" href={href({ page: 'inbox' })} aria-label="PatchPilot 验收队列">
        <span className="brand-mark" aria-hidden><Terminal size={17} /></span>
        <span className="brand-text">
          <span className="brand-name">PatchPilot</span>
          <span className="brand-sub">可信的开源维护流水线</span>
        </span>
      </a>
      <nav className="tabs" aria-label="主导航">
        {PRIMARY_TABS.map((t) => <a key={t.page} href={href({ page: t.page })} className={route.page === t.page ? 'tab on' : 'tab'} aria-current={route.page === t.page ? 'page' : undefined}>
          <t.icon size={15} />{t.name}
        </a>)}
        <details className="tab-more">
          <summary className={SECONDARY_TABS.some((t) => route.page === t.page) ? 'tab on' : 'tab'}><MoreHorizontal size={15} />证据工具</summary>
          <div className="tab-more-menu" role="menu">
            {SECONDARY_TABS.map((t) => <a key={t.page} href={href({ page: t.page })} role="menuitem" className={route.page === t.page ? 'on' : undefined}>
              <t.icon size={14} />{t.name}
            </a>)}
          </div>
        </details>
      </nav>
      <div className="topbar-end">
        <span className={`conn ${online ? 'conn-on' : 'conn-off'}`}>
          <Activity size={14} />{service.loading ? '连接中' : online ? '服务已连接' : '服务未连接'}
        </span>
        <button className="btn btn-primary" onClick={() => setIntakeDialog(true)} disabled={!online}><ClipboardCheck size={14} /> 导入验收任务</button>
      </div>
    </header>

    <main className="page" id="main">
      {route.page === 'inspect' && <InspectPage />}
      {route.page === 'policy' && <PolicyPage />}
      {route.page === 'inbox' && <AcceptanceInboxPage routeId={route.id} onImport={() => setIntakeDialog(true)} />}
      {route.page === 'review' && <CodeReviewPage routeId={route.id} candidateId={route.candidateId} contractId={route.contractId} checkId={route.checkId} />}
      {route.page === 'investigation' && <CounterexamplePage routeId={route.id} candidateId={route.candidateId} />}
      {route.page === 'contract' && <AcceptanceContractPage routeId={route.id} contractId={route.contractId} />}
      {route.page === 'report' && <DeliveryReportPage routeId={route.id} reportId={route.reportId} />}
      {route.page !== 'inspect' && route.page !== 'policy' && route.page !== 'inbox' && route.page !== 'review' && route.page !== 'investigation' && route.page !== 'contract' && route.page !== 'report' && (
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
    {intakeDialog && <IntakeDialog onClose={() => setIntakeDialog(false)} onCreated={(taskId) => { setIntakeDialog(false); go({ page: 'review', id: taskId }); }} />}
  </div>;
}

createRoot(document.getElementById('root')!).render(<App />);
