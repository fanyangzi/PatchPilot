import { useEffect, useMemo, useState } from 'react';
import type { FormEvent } from 'react';
import { Activity, ArrowDownRight, ArrowRight, ArrowUpRight, BookOpenCheck, Check, CircleAlert, ClipboardCheck, Download, FileSearch, FileText, GitBranch, GitCommitHorizontal, GitPullRequest, LoaderCircle, Plus, RefreshCw, ShieldAlert, ShieldCheck, SquareArrowOutUpRight, X } from 'lucide-react';
import { V1Error, v1, type Candidate, type Check as CheckItem, type Condition, type Contract, type EvidenceGraph, type InboxItem, type Task, type Verification } from '../apiV1';
import { go, href, type Route } from '../ui';

type Remote<T> = { data?: T; error?: string; loading: boolean; reload: () => void };
function useRemote<T>(key: string, load: () => Promise<T>): Remote<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    let current = true;
    // Clear the previous resource immediately. Keeping an old diff/report visible
    // while a new key is loading is a false success: candidate A must never look
    // like candidate B after a deep-link or selector change.
    setData(undefined);
    setLoading(true); setError(undefined);
    load().then((value) => { if (current) { setData(value); setLoading(false); } }, (e: unknown) => {
      if (current) { setError(e instanceof Error ? e.message : String(e)); setLoading(false); }
    });
    return () => { current = false; };
    // A keyed resource is reloaded explicitly; `load` is a render-local closure.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, version]);
  return { data, error, loading, reload: () => setVersion((v) => v + 1) };
}

function useTasks() {
  return useRemote('tasks', async () => (await v1.tasks()).items);
}
function useTask(id?: string) {
  return useRemote(id || 'no-task', async () => id ? v1.task(id) : Promise.reject(new Error('请选择一个任务')));
}
function unwrapError(e: unknown) { return e instanceof Error ? e.message : String(e); }
function Status({ label, value, tone = 'neutral' }: { label: string; value: string; tone?: 'good' | 'bad' | 'warn' | 'neutral' }) {
  return <span className={`ac-status ac-${tone}`}><i />{label}<b>{value}</b></span>;
}
function PageHead({ eyebrow, title, lead, icon: Icon, action }: { eyebrow: string; title: string; lead: string; icon: typeof ClipboardCheck; action?: React.ReactNode }) {
  return <div className="ac-head"><div><div className="ac-eyebrow"><Icon size={14} />{eyebrow}</div><h1>{title}</h1><p>{lead}</p></div>{action && <div className="ac-head-action">{action}</div>}</div>;
}
function RemoteState({ loading, error, empty, onRetry }: { loading: boolean; error?: string; empty?: string; onRetry: () => void }) {
  if (loading) return <div className="ac-state" role="status"><LoaderCircle className="spin" size={18} />正在读取服务端记录…</div>;
  if (error) return <div className="ac-state ac-state-error" role="alert"><CircleAlert size={19} /><div><strong>读取失败</strong><p>{error}</p><button className="btn" onClick={onRetry}><RefreshCw size={13} />重试</button></div></div>;
  if (empty) return <div className="ac-state ac-state-empty"><FileSearch size={19} /><div><strong>暂无记录</strong><p>{empty}</p></div></div>;
  return null;
}
function TaskSelect({ tasks, value, onChange }: { tasks: Task[]; value: string; onChange: (value: string) => void }) {
  return <label className="ac-select-label"><span>验收任务</span><select value={value} onChange={(e) => onChange(e.target.value)}><option value="">选择任务…</option>{tasks.map((task) => <option value={task.task_id} key={task.task_id}>{task.issue_snapshot.repo_id || '未知仓库'} · {task.issue_snapshot.title || task.task_id}</option>)}</select></label>;
}
function useSelectedTask(routeId?: string, page?: Route['page']) {
  const tasks = useTasks();
  const [selected, setSelectedState] = useState(routeId || '');
  // Route changes are authoritative and must preserve the candidate/contract
  // query parameters owned by the caller. Only an explicit user selection
  // should rewrite the hash.
  useEffect(() => { if (routeId) setSelectedState(routeId); else if (!selected && tasks.data?.length) setSelected(tasks.data[0].task_id); }, [routeId, tasks.data, selected]);
  // Prefer the route parameter during the render in which it changes. This
  // prevents one frame of the previous task from leaking into a deep-linked
  // review page before the synchronization effect runs.
  const active = routeId || selected;
  const task = useTask(active || undefined);
  const setSelected = (value: string) => {
    setSelectedState(value);
    if (page && value) go({ page, id: value });
  };
  return { tasks, selected: active, setSelected, task };
}
function RouteLink({ route, children }: { route: Route; children: React.ReactNode }) {
  return <a className="ac-link" href={href(route)}>{children}<ArrowRight size={14} /></a>;
}
function SourceRefs({ refs }: { refs: string[] }) {
  if (!refs.length) return <span className="ac-muted">没有已记录来源</span>;
  return <div className="ac-sources">{refs.map((ref) => {
    let safeHref: string | undefined;
    try {
      const parsed = new URL(ref, window.location.origin);
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') safeHref = parsed.href;
    } catch { /* Render malformed source references as inert evidence text. */ }
    return safeHref
      ? <a key={ref} href={safeHref} target="_blank" rel="noreferrer"><SquareArrowOutUpRight size={12} />{ref}</a>
      : <code key={ref} className="ac-source-text">{ref}</code>;
  })}</div>;
}
function taskTitle(task: Task) { return task.issue_snapshot.title || '未命名任务'; }
function stateTone(value?: string): 'good' | 'bad' | 'warn' | 'neutral' {
  if (value === 'accepted' || value === 'accepted_within_scope' || value === 'completed' || value === 'current' || value === 'frozen') return 'good';
  if (value === 'rejected' || value === 'blocked' || value === 'error' || value === 'stale') return 'bad';
  if (value === 'queued' || value === 'running' || value === 'pending' || value === 'inconclusive' || value === 'not_evaluated') return 'warn';
  return 'neutral';
}
function fourStatus(verification?: Verification | null) {
  if (!verification) return <div className="ac-four"><Status label="运行状态" value="未创建" /><Status label="结论" value="未评估" /><Status label="有效性" value="未绑定" /><Status label="维护者决策" value="待处理" /></div>;
  return <div className="ac-four"><Status label="运行状态" value={verification.run_state} tone={stateTone(verification.run_state)} /><Status label="结论" value={verification.verdict} tone={stateTone(verification.verdict)} /><Status label="有效性" value={verification.validity} tone={stateTone(verification.validity)} /><Status label="维护者决策" value={verification.review_decision} tone={stateTone(verification.review_decision)} /></div>;
}

const GRAPH_TYPE_LABELS: Record<string, string> = {
  task: '任务', source: '来源', candidate: '候选', contract: '合同', condition: '条件',
  verification: '验证', check: '检查', finding: '反例', artifact: '证据',
};

function EvidenceGraphPanel({ graph, loading, error, onRetry }: { graph?: EvidenceGraph; loading: boolean; error?: string; onRetry: () => void }) {
  return <section className="ac-sheet ac-graph-panel" aria-labelledby="evidence-graph-title">
    <div className="ac-section-title"><div><GitBranch size={16} /><h2 id="evidence-graph-title">证据关系图</h2></div><span>{graph ? `${graph.summary.node_count} 节点 · ${graph.summary.edge_count} 条关系` : '任务来源 → 候选 → 合同 → 验证'}</span></div>
    <p className="ac-note">把当前任务的来源、候选、验收合同、逐项检查和证据串成可追溯链。节点状态来自服务端持久化记录，缺失资源保持显式缺口。</p>
    <RemoteState loading={loading} error={error} empty={!loading && !error && !graph?.nodes.length ? '服务端尚未生成该任务的证据图。' : undefined} onRetry={onRetry} />
    {graph && graph.nodes.length > 0 && <>
      <div className="ac-graph-summary">{Object.entries(graph.summary.counts).map(([type, count]) => <span key={type}><b>{count}</b>{GRAPH_TYPE_LABELS[type] || type}</span>)}{graph.truncated && <span className="ac-graph-truncated">已按上限截断</span>}</div>
      <div className="ac-graph-nodes">{graph.nodes.map((node) => <article className="ac-graph-node" key={node.id}><div className="ac-graph-node-head"><span className="ac-graph-type">{GRAPH_TYPE_LABELS[node.type] || node.type}</span>{node.status && <Status label="状态" value={node.status} tone={stateTone(node.status)} />}</div><strong title={node.label}>{node.label}</strong><code>{node.id}</code>{node.refs?.length ? <SourceRefs refs={node.refs.slice(0, 2)} /> : null}</article>)}</div>
      {graph.edges.length > 0 && <div className="ac-graph-edges"><span className="ac-label">关系链</span>{graph.edges.slice(0, 24).map((edge) => <span key={edge.id}><code>{edge.source}</code><ArrowRight size={12} /><code>{edge.target}</code>{edge.label || edge.type ? <small>{edge.label || edge.type}</small> : null}</span>)}</div>}
    </>}
  </section>;
}

export function AcceptanceInboxPage({ routeId, onImport }: { routeId?: string; onImport?: () => void }) {
  const inbox = useRemote('inbox', () => v1.inbox());
  const rows = inbox.data?.items || [];
  const hasActiveWork = rows.some((row) => {
    const state = row.verification?.run_state || row.latest_verification?.run_state || row.status;
    return state === 'queued' || state === 'running' || state === 'pending';
  });
  // Verification workers are intentionally asynchronous. Refresh only while
  // a task is active so the queue reflects the measured state without turning
  // an idle inbox into a polling dashboard.
  useEffect(() => {
    if (!hasActiveWork || inbox.loading || inbox.error) return;
    const timer = window.setInterval(inbox.reload, 3000);
    return () => window.clearInterval(timer);
  }, [hasActiveWork, inbox.loading, inbox.error, inbox.reload]);
  return <div className="ac-page">
    <PageHead eyebrow="MAINTAINER INBOX / 01" title="验收队列" lead="维护者待决策入口。每一项都来自持久化任务、候选、验收合同和验证记录。" icon={ClipboardCheck} action={<button className="btn" onClick={inbox.reload}><RefreshCw size={14} />刷新队列</button>} />
    <div className="ac-metrics"><div><span>队列记录</span><strong>{inbox.loading ? '…' : inbox.error ? '—' : inbox.data?.total ?? rows.length}</strong></div><div><span>展示来源</span><strong>API v1</strong></div><div><span>队列状态</span><strong className={hasActiveWork ? 'ac-metric-live' : undefined}>{hasActiveWork ? '有任务执行中' : '等待真实导入'}</strong></div></div>
    <RemoteState loading={inbox.loading} error={inbox.error} empty={!inbox.loading && !inbox.error && rows.length === 0 ? '当前没有任务进入维护者验收队列。新建真实 PR、Issue 或补丁任务后会显示在这里。' : undefined} onRetry={inbox.reload} />
    {!inbox.loading && !inbox.error && rows.length === 0 && <div className="ac-empty-action"><div><strong>从真实候选开始</strong><p>导入仓库、基线、需求和 unified diff，系统会先建立可追溯任务，再由维护者决定何时执行验证。</p></div><button className="btn btn-primary" onClick={onImport}><Plus size={14} />导入真实任务</button></div>}
    {!inbox.loading && !inbox.error && rows.length > 0 && <div className="ac-table-wrap"><table className="ac-table"><thead><tr><th>问题 / 仓库</th><th>候选补丁</th><th>合同</th><th>四维状态</th><th>下一步</th><th /></tr></thead><tbody>{rows.map((row, i) => {
      const task = row.task || ({ task_id: row.task_id, issue_snapshot: { title: row.title, repo_id: row.repo, mode: row.mode }, source_refs: row.source_refs, mode: row.mode, revision: 1, created_at: row.created_at } as Task);
      const candidate = row.candidate;
      const verification = row.verification || row.latest_verification;
      const contract = row.contract || (row.current_contract ? { ...row.current_contract, contract_id: row.current_contract.contract_id, task_id: row.task_id, conditions: [], source_refs: [], contract_hash: '', created_at: row.created_at } as Contract : null);
      const id = task.task_id || row.task_id || '';
      return <tr key={id || i} tabIndex={0} aria-label={`打开验收任务：${taskTitle(task)}`} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); go({ page: 'review', id }); } }}><td><strong>{taskTitle(task)}</strong><span className="ac-cell-sub">{task.issue_snapshot?.repo_id || '仓库未记录'} · {id}</span></td><td>{row.candidate_count ?? (candidate ? 1 : 0)} 个{candidate?.candidate_id && <><br /><code>{candidate.candidate_id}</code></>}</td><td>{contract ? `v${contract.revision} · ${contract.state}` : <span className="ac-muted">尚无合同</span>}</td><td><div className="ac-mini-status">{verification ? <><span>{verification.run_state}</span><span>{verification.verdict}</span><span>{verification.validity}</span><span>{verification.review_decision}</span></> : <span>尚无验证记录</span>}</div></td><td><span className="ac-next-action">{row.next_action || '—'}</span><small className="ac-cell-sub">{row.next_action_reason || ''}</small></td><td><RouteLink route={{ page: 'review', id }}>复核</RouteLink></td></tr>;
    })}</tbody></table></div>}
    <div className="ac-footnote"><ShieldCheck size={15} /> 未运行、环境错误、未知和不完整验证保持原始状态；它们不会被提升为通过。</div>
  </div>;
}

/** Real intake entry point. It creates an intake, draft task and candidate on
 * the server; it never selects a fixture or invents a run result. */
export function IntakeDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (taskId: string) => void }) {
  const [repoId, setRepoId] = useState('');
  const [baseRef, setBaseRef] = useState('main');
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [patch, setPatch] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      if (!repoId.trim() || !/^[-A-Za-z0-9_.]+\/[-A-Za-z0-9_.]+$/.test(repoId.trim())) throw new Error('仓库必须填写 owner/name。');
      if (!baseRef.trim()) throw new Error('请填写基线分支或 commit。');
      if (!title.trim() || !body.trim()) throw new Error('请填写问题标题和真实需求描述。');
      if (!patch.trim()) throw new Error('请粘贴候选 unified diff；系统不会从空输入生成候选。');
      const intake = await v1.createIntake({ mode: 'local_patch', repo_id: repoId.trim(), base_ref: baseRef.trim(), patch_text: patch, issue_title: title.trim(), issue_body: body.trim(), source_refs: ['user:submitted-issue'] });
      const task = await v1.createTask({ intake_id: intake.intake_id, mode: 'verify' });
      await v1.createCandidate(task.task.task_id, { source: 'upload', base_sha: baseRef.trim(), patch_text: patch, author_type: 'human' });
      onCreated(task.task.task_id);
    } catch (e) { setError(unwrapError(e)); } finally { setBusy(false); }
  }
  return <div className="ac-modal-backdrop" role="presentation" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}><form className="ac-modal" onSubmit={submit} aria-labelledby="intake-title">
    <header><div><span className="ac-eyebrow"><GitPullRequest size={13} />REAL INTAKE / F01</span><h2 id="intake-title">导入验收任务</h2><p>输入真实仓库、需求和候选补丁。创建后进入验收队列，不自动运行或显示通过。</p></div><button type="button" className="icon-btn" onClick={onClose} aria-label="关闭"><X size={18} /></button></header>
    <div className="ac-form-grid"><label>仓库（owner/name）<input value={repoId} onChange={(e) => setRepoId(e.target.value)} placeholder="fanyangzi/PatchPilot" autoFocus /></label><label>基线 ref<input value={baseRef} onChange={(e) => setBaseRef(e.target.value)} placeholder="main 或 commit SHA" /></label><label className="ac-form-wide">问题标题<input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="要验证的真实问题" /></label><label className="ac-form-wide">需求 / Issue 描述<textarea value={body} onChange={(e) => setBody(e.target.value)} rows={4} placeholder="包含可核对的 change、preserve 或 constraint 条件" /></label><label className="ac-form-wide">候选 unified diff<textarea value={patch} onChange={(e) => setPatch(e.target.value)} rows={8} placeholder="粘贴真实候选补丁" /></label></div>
    {error && <div className="ac-action-error" role="alert"><CircleAlert size={14} />{error}</div>}<footer><button type="button" className="btn" onClick={onClose}>取消</button><button className="btn btn-primary" disabled={busy}>{busy ? <><LoaderCircle size={14} className="spin" />正在保存</> : <><ClipboardCheck size={14} />创建验收任务</>}</button></footer>
  </form></div>;
}

export function CodeReviewPage({ routeId, candidateId: routeCandidateId, contractId: routeContractId, checkId: routeCheckId }: { routeId?: string; candidateId?: string; contractId?: string; checkId?: string }) {
  const { tasks, selected, setSelected, task } = useSelectedTask(routeId, 'review');
  const [candidateId, setCandidateIdState] = useState(routeCandidateId || '');
  const [suite, setSuite] = useState(''); const [environment, setEnvironment] = useState(''); const [policy, setPolicy] = useState('');
  const [baselineHash, setBaselineHash] = useState('unavailable'); const [commandsJson, setCommandsJson] = useState('');
  const [repoId, setRepoId] = useState(''); const [repoPath, setRepoPath] = useState('');
  const [decision, setDecision] = useState<'accepted' | 'rejected' | 'exception_accepted'>('accepted'); const [decisionReason, setDecisionReason] = useState('');
  const [busy, setBusy] = useState(''); const [actionError, setActionError] = useState(''); const [toast, setToast] = useState('');
  const current = task.data?.task; const candidates = task.data?.candidates || [];
  const graph = useRemote(`task-graph:${selected}`, () => selected ? v1.taskGraph(selected, { depth: 2, limit: 200 }) : Promise.reject(new Error('请选择一个任务')));
  const setCandidateId = (value: string) => {
    setCandidateIdState(value);
    if (selected && value) go({ page: 'review', id: selected, candidateId: value, contractId: routeContractId, checkId: routeCheckId });
  };
  useEffect(() => {
    if (routeCandidateId && candidates.some((c) => c.candidate_id === routeCandidateId)) setCandidateIdState(routeCandidateId);
    else if (candidates.length && !candidates.some((c) => c.candidate_id === candidateId)) setCandidateId(candidates[0].candidate_id);
    if (!candidates.length) setCandidateIdState('');
  }, [candidates, candidateId, routeCandidateId]);
  const candidate = candidates.find((c) => c.candidate_id === candidateId);
  const diff = useRemote(candidateId, () => candidateId ? v1.candidateDiff(candidateId) : Promise.reject(new Error('该任务没有候选补丁')));
  const verification = task.data?.latest_verification?.candidate_id === candidateId ? task.data.latest_verification : null;
  const contracts = task.data?.contracts || [];
  const frozen = contracts.find((c) => c.contract_id === routeContractId && c.state === 'frozen') || [...contracts].reverse().find((c) => c.state === 'frozen');
  const checks = useRemote(`review-checks:${verification?.verification_id || 'none'}`, () => verification
    ? v1.checks(verification.verification_id)
    : Promise.resolve({ items: [] as CheckItem[], complete: false, required_gaps: [] as string[] }));
  useEffect(() => {
    if (routeCheckId && checks.data?.items.some((item) => item.check_id === routeCheckId)) {
      document.getElementById(`review-check-${routeCheckId}`)?.scrollIntoView({ block: 'center' });
    }
  }, [routeCheckId, checks.data]);
  const act = async (label: string, fn: () => Promise<unknown>) => { setBusy(label); setActionError(''); setToast(''); try { await fn(); setToast(label); task.reload(); } catch (e) { setActionError(unwrapError(e)); } finally { setBusy(''); } };
  const run = () => act('验证记录已创建', async () => {
    let argv: string[][];
    try { argv = JSON.parse(commandsJson); if (!Array.isArray(argv) || !argv.every((line) => Array.isArray(line) && line.every((x) => typeof x === 'string'))) throw new Error(); }
    catch { throw new Error('命令必须是 JSON argv 数组，例如 [["pytest", "-q"]]'); }
    await v1.createVerification(selected, { candidate_id: candidateId, contract_id: frozen!.contract_id, suite_id: suite, environment_id: environment, policy_id: policy, baseline_tests_hash: baselineHash || 'unavailable', command_argv: argv, verifier_revision: 'patchpilot-verifier@unversioned', probe_plan_hash: 'none', seed: 0 });
  });
  const execute = () => act('验证执行已记录', async () => {
    if (!verification) throw new Error('请先创建验证记录。');
    let argv: string[][];
    try { argv = JSON.parse(commandsJson); if (!Array.isArray(argv) || !argv.every((line) => Array.isArray(line) && line.every((x) => typeof x === 'string'))) throw new Error(); }
    catch { throw new Error('命令必须是 JSON argv 数组，例如 [[\"pytest\", \"-q\"]].'); }
    if (!repoId.trim() && !repoPath.trim()) throw new Error('请填写已配置的仓库 owner/name 或本地工作区路径。');
    await v1.executeVerification(verification.verification_id, { repo_id: repoId.trim() || undefined, repo_path: repoPath.trim() || undefined, suite_id: suite || undefined, command_argv: argv });
  });
  const recordDecision = () => act('维护者决定已记录', async () => {
    if (!verification) throw new Error('当前候选没有验证记录。');
    await v1.decision(verification.verification_id, { decision, reason: decisionReason, expected_key: verification.verification_key, actor: 'maintainer' });
  });
  return <div className="ac-page">
    <PageHead eyebrow="PATCH REVIEW / 02" title="代码审查" lead="同时核对候选代码差异、问题来源、当前冻结的验收依据和验证四维状态。" icon={GitPullRequest} />
    <div className="ac-toolbar"><RemoteState loading={tasks.loading} error={tasks.error} onRetry={tasks.reload} />{!tasks.loading && !tasks.error && tasks.data?.length ? <TaskSelect tasks={tasks.data} value={selected} onChange={(v) => { setSelected(v); setCandidateId(''); }} /> : null}</div>
    {!tasks.loading && !tasks.error && tasks.data?.length === 0 && <RemoteState loading={false} empty="任务列表为空。验收入口接收真实任务，不会展示演示记录。" onRetry={tasks.reload} />}
    {selected && <>
      <RemoteState loading={task.loading} error={task.error} onRetry={task.reload} />
      {current && <>
        <div className="ac-source-banner"><div><span className="ac-label">问题来源</span><h2>{taskTitle(current)}</h2><p>{current.issue_snapshot.repo_id} <span>·</span> 基线 <code>{current.issue_snapshot.base_ref || '未解析'}</code></p></div><SourceRefs refs={current.source_refs} /></div>
        <EvidenceGraphPanel graph={graph.data} loading={graph.loading} error={graph.error} onRetry={graph.reload} />
        <div className="ac-review-grid"><section className="ac-sheet ac-diff-sheet"><div className="ac-section-title"><div><GitBranch size={16} /><h2>候选补丁差异</h2></div><label className="ac-inline-control"><span className="sr-only">切换候选补丁</span><select aria-label="切换候选补丁" value={candidateId} onChange={(e) => setCandidateId(e.target.value)}><option value="">选择候选</option>{candidates.map((c) => <option key={c.candidate_id} value={c.candidate_id}>{c.candidate_id.slice(0, 25)} · {c.author_type}</option>)}</select></label></div>
          {!candidates.length ? <div className="ac-state ac-state-empty">此任务暂无候选补丁记录。</div> : <><div className="ac-candidate-meta"><span>候选 <code>{candidate?.candidate_id}</code></span><span>基线 <code>{candidate?.base_sha}</code></span><span>内容 SHA256 <code>{candidate?.patch_hash}</code></span></div><RemoteState loading={diff.loading} error={diff.error} empty={!diff.loading && !diff.error && !diff.data?.available ? `补丁内容未提供给 API（${diff.data?.content_ref || '无可读取的 content ref'}）；仅显示服务端保存的摘要。` : undefined} onRetry={diff.reload} />{diff.data?.available && diff.data.content && <pre className="ac-code"><code>{diff.data.content}</code></pre>}</>}
        </section>
        <aside className="ac-sheet ac-review-aside"><div className="ac-section-title"><div><ShieldCheck size={16} /><h2>验收依据</h2></div></div>
          {frozen ? <><div className="ac-contract-head"><strong>冻结合同 v{frozen.revision}</strong><code>{frozen.contract_hash}</code></div><ul className="ac-condition-list">{frozen.conditions.map((c) => <li key={c.condition_id}><span>{c.kind} · {c.required ? '必须' : '可选'}</span><p>{c.statement}</p><SourceRefs refs={c.source_refs} /></li>)}</ul></> : <div className="ac-state ac-state-empty">尚无冻结合同。验收标准确定并冻结后，才能创建验证记录。</div>}
          <div className="ac-verdict-area"><div className="ac-section-title"><div><Activity size={16} /><h2>验证状态</h2></div></div>{fourStatus(verification)}{verification && <><p className="ac-key-line">verification_key <code>{verification.verification_key}</code></p>{verification.gaps.length > 0 && <div className="ac-gap"><CircleAlert size={14} />未覆盖：{verification.gaps.join('、')}</div>}<div className="ac-decision-form"><label>维护者决定<select value={decision} onChange={(e) => setDecision(e.target.value as typeof decision)}><option value="accepted">accepted · 在范围内接受</option><option value="rejected">rejected · 拒绝</option><option value="exception_accepted">exception_accepted · 例外接受</option></select></label><label>决定理由<textarea rows={2} value={decisionReason} onChange={(e) => setDecisionReason(e.target.value)} placeholder="记录可追溯的维护者理由" /></label><div className="ac-form-actions">{actionError && <span className="ac-action-error"><CircleAlert size={14} />{actionError}</span>}{toast && <span className="ac-action-ok"><Check size={14} />{toast}</span>}<button className="btn" onClick={recordDecision} disabled={!!busy || !verification.verification_key.trim()}><ShieldCheck size={14} />记录维护者决定</button></div></div></>}</div>
        </aside>
        <section className="ac-sheet ac-review-matrix" aria-labelledby="review-matrix-title"><div className="ac-section-title"><div><ClipboardCheck size={16} /><h2 id="review-matrix-title">验证矩阵</h2></div><span>仅显示服务端持久化检查</span></div>
          {!verification && <div className="ac-state ac-state-empty">当前候选尚未建立验证记录，不能推断测试通过。</div>}
          {verification && <><RemoteState loading={checks.loading} error={checks.error} empty={!checks.loading && !checks.error && checks.data?.items.length === 0 ? '验证已创建但尚无逐项执行记录；queued / not_evaluated 保持原样。' : undefined} onRetry={checks.reload} />{checks.data?.items.length ? <div className="ac-table-wrap"><table className="ac-table"><thead><tr><th>检查</th><th>变体</th><th>结果</th><th>测试数</th><th>执行</th></tr></thead><tbody>{checks.data.items.map((item) => <tr id={`review-check-${item.check_id}`} key={item.check_id}><td><strong>{item.target}</strong><span className="ac-cell-sub">{item.suite_id}</span></td><td><code>{item.variant}</code></td><td><Status label="结果" value={item.outcome || 'unknown'} tone={stateTone(item.outcome)} /></td><td>{item.count}</td><td><RouteLink route={{ page: 'investigation', id: selected, candidateId, checkId: item.check_id }}>查看证据</RouteLink></td></tr>)}</tbody></table></div> : null}</>}
        </section></div>
        <section className="ac-sheet ac-run-form"><div className="ac-section-title"><div><GitCommitHorizontal size={16} /><h2>创建验证记录</h2></div><span>需要当前冻结合同和所选候选</span></div><p className="ac-note">提交后创建一条新的不可变验证记录。当前服务若没有执行 worker，状态仍会保持 queued / not_evaluated，不会显示为通过。</p><div className="ac-form-grid"><label>验证套件 ID<input value={suite} onChange={(e) => setSuite(e.target.value)} placeholder="例：repo-tests-v1" /></label><label>环境 ID<input value={environment} onChange={(e) => setEnvironment(e.target.value)} placeholder="固定镜像或环境摘要" /></label><label>策略 ID<input value={policy} onChange={(e) => setPolicy(e.target.value)} placeholder="实际启用的策略版本" /></label><label>基线测试哈希<input value={baselineHash} onChange={(e) => setBaselineHash(e.target.value)} placeholder="未知时保留 unavailable" /></label><label>仓库映射（owner/name）<input value={repoId} onChange={(e) => setRepoId(e.target.value)} placeholder="使用 PATCHPILOT_REPO_MAP 时填写" /></label><label className="ac-form-wide">本地工作区路径（可选）<input value={repoPath} onChange={(e) => setRepoPath(e.target.value)} placeholder="必须在 PATCHPILOT_WORKSPACE_ROOTS 内" /></label><label className="ac-form-wide">执行命令（argv JSON）<textarea value={commandsJson} onChange={(e) => setCommandsJson(e.target.value)} placeholder={'[["pytest", "-q"]]'} rows={2} /></label></div><div className="ac-form-actions">{actionError && <span className="ac-action-error"><CircleAlert size={14} />{actionError}</span>}{toast && <span className="ac-action-ok"><Check size={14} />{toast}</span>}<button className="btn btn-primary" disabled={!candidate || !frozen || !suite.trim() || !environment.trim() || !policy.trim() || !commandsJson.trim() || !!busy} onClick={run}>{busy ? <LoaderCircle size={14} className="spin" /> : <Activity size={14} />}创建验证记录</button>{verification && <button className="btn" disabled={!!busy || (!repoId.trim() && !repoPath.trim())} onClick={execute}>{busy === '验证执行已记录' ? <LoaderCircle size={14} className="spin" /> : <GitCommitHorizontal size={14} />}执行受控本地验收</button>}</div>
        <div className="ac-page-links"><RouteLink route={{ page: 'contract', id: selected, contractId: frozen?.contract_id }}>查看验收条件</RouteLink><RouteLink route={{ page: 'investigation', id: selected, candidateId }}>调查反例与检查</RouteLink><RouteLink route={{ page: 'report', id: selected }}>查看交付报告</RouteLink></div></section>
      </>}
    </>}
  </div>;
}

function ContractEditor({ task, onSaved, initialContractId }: { task: Task; onSaved: () => void; initialContractId?: string }) {
  const contracts = useRemote(task.task_id, () => v1.contracts(task.task_id));
  const [contractId, setContractId] = useState(initialContractId || ''); const [conditions, setConditions] = useState<Condition[]>([]);
  const [baseSnapshot, setBaseSnapshot] = useState(task.issue_snapshot.base_ref || '');
  const [sourceIds, setSourceIds] = useState(task.source_refs.join('\n'));
  const [exclusions, setExclusions] = useState(''); const [busy, setBusy] = useState(''); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const items = contracts.data?.items || [];
  const lastContract = items.length ? items[items.length - 1] : undefined;
  const activeId = contractId || lastContract?.contract_id || '';
  const contract = items.find((c) => c.contract_id === activeId) || lastContract;
  useEffect(() => { if (contract && contract.contract_id !== contractId) setContractId(contract.contract_id); if (contract) setConditions(contract.conditions); }, [contract?.contract_id, contract?.revision]);
  const editable = !contract || contract.state !== 'frozen';
  const mutate = async (label: string, fn: () => Promise<unknown>) => { setBusy(label); setError(''); setNotice(''); try { await fn(); setNotice(label); contracts.reload(); onSaved(); } catch (e) { setError(unwrapError(e)); } finally { setBusy(''); } };
  const createDraft = () => mutate('合同草稿已建立', async () => {
    const sources = sourceIds.split('\n').map((s) => s.trim()).filter(Boolean);
    if (!sources.length) throw new Error('至少填写一个真实来源引用。');
    if (!baseSnapshot.trim()) throw new Error('请填写实际基线引用或快照 ID。');
    const response = await v1.createDraft(task.task_id, { source_ids: sources, base_snapshot_id: baseSnapshot.trim(), model_profile: 'manual', conditions: [] });
    setContractId(response.contract.contract_id); setConditions([]);
  });
  const save = () => mutate('合同新版本已保存', async () => {
    if (!contract) throw new Error('请先建立合同草稿。');
    const valid = conditions.filter((c) => c.statement.trim() && c.source_refs.length);
    if (valid.length !== conditions.length || !valid.length) throw new Error('每条条件都需要描述和至少一个来源引用。');
    const response = await v1.updateContract(contract.contract_id, { expected_revision: contract.revision, conditions: valid });
    setConditions(response.contract.conditions);
  });
  const freeze = () => mutate('验收条件已冻结', async () => {
    if (!contract || !conditions.length) throw new Error('先保存至少一条有来源的验收条件。');
    await v1.freezeContract(contract.contract_id, { expected_revision: contract.revision, confirmed_condition_ids: conditions.map((c) => c.condition_id), scope_exclusions: exclusions.split('\n').map((x) => x.trim()).filter(Boolean), actor: 'maintainer' });
  });
  const changeCondition = (index: number, changes: Partial<Condition>) => setConditions((prev) => prev.map((c, i) => i === index ? { ...c, ...changes } : c));
  return <div className="ac-contract-layout"><aside className="ac-sheet ac-contract-sidebar"><div className="ac-section-title"><div><BookOpenCheck size={16} /><h2>合同版本</h2></div><button className="icon-btn" title="刷新合同" onClick={contracts.reload}><RefreshCw size={14} /></button></div><RemoteState loading={contracts.loading} error={contracts.error} empty={!contracts.loading && !contracts.error && !items.length ? '当前任务还没有验收合同。' : undefined} onRetry={contracts.reload} />{items.map((item) => <button key={`${item.contract_id}-${item.revision}`} className={`ac-contract-version ${item.contract_id === activeId ? 'selected' : ''}`} onClick={() => { setContractId(item.contract_id); setConditions(item.conditions); go({ page: 'contract', id: task.task_id, contractId: item.contract_id }); }}><span>v{item.revision} · {item.state}</span><small>{item.contract_id}</small></button>)}<button className="btn" onClick={() => { setContractId(''); setConditions([]); go({ page: 'contract', id: task.task_id }); createDraft(); }} disabled={!!busy}><Plus size={14} />建立新草稿</button></aside>
    <section className="ac-sheet ac-contract-main"><div className="ac-section-title"><div><BookOpenCheck size={16} /><h2>{contract ? `合同 ${contract.contract_id} · v${contract.revision}` : '新建合同草稿'}</h2></div>{contract && <span className={`ac-state-pill ${contract.state}`}>{contract.state}</span>}</div>
      <div className="ac-origin-grid"><label>基线快照 / Git 引用<input value={baseSnapshot} onChange={(e) => setBaseSnapshot(e.target.value)} disabled={!!contract} placeholder="来自实际任务的 commit 或已解析快照 ID" /></label><label>合同来源（每行一条 URL 或可追溯 ID）<textarea value={sourceIds} onChange={(e) => setSourceIds(e.target.value)} disabled={!!contract} rows={3} placeholder="Issue、PR、需求文档等实际来源" /></label></div>
      {!contract && <div className="ac-contract-empty"><p>合同草稿会以这些来源记录为依据；创建之后可补充逐条验收条件。</p><button className="btn btn-primary" onClick={createDraft} disabled={!!busy || !sourceIds.trim() || !baseSnapshot.trim()}>{busy ? <LoaderCircle size={14} className="spin" /> : <Plus size={14} />}建立合同草稿</button></div>}
      {contract && <><div className="ac-contract-hash"><span>不可变版本哈希</span><code>{contract.contract_hash}</code></div><div className="ac-condition-editor"><div className="ac-section-title"><div><ClipboardCheck size={16} /><h3>验收条件</h3></div><span>{conditions.length} 条 · 每条必须引用来源</span></div>
        {conditions.map((condition, index) => <div className="ac-condition-edit" key={condition.condition_id}><div className="ac-condition-edit-top"><code>{condition.condition_id}</code>{editable && <button className="icon-btn ac-remove" aria-label="删除条件" onClick={() => setConditions((prev) => prev.filter((_, i) => i !== index))}><X size={14} /></button>}</div><div className="ac-form-grid"><label>条件类型<select value={condition.kind} disabled={!editable} onChange={(e) => changeCondition(index, { kind: e.target.value as Condition['kind'] })}><option value="change">change · 需要改变</option><option value="preserve">preserve · 必须保留</option><option value="constraint">constraint · 范围约束</option><option value="clarification">clarification · 待澄清</option></select></label><label className="ac-check-label"><input type="checkbox" checked={condition.required} disabled={!editable} onChange={(e) => changeCondition(index, { required: e.target.checked })} />必须满足</label><label className="ac-form-wide">可验证的条件陈述<textarea rows={2} value={condition.statement} disabled={!editable} onChange={(e) => changeCondition(index, { statement: e.target.value })} placeholder="写明期望行为、边界或禁止变化" /></label><label className="ac-form-wide">来源引用（每行一条）<textarea rows={2} value={condition.source_refs.join('\n')} disabled={!editable} onChange={(e) => changeCondition(index, { source_refs: e.target.value.split('\n').map((x) => x.trim()).filter(Boolean) })} placeholder="对应的 issue/PR/文档段落 URL 或引用 ID" /></label></div></div>)}
        {editable && <button className="btn" onClick={() => setConditions((prev) => [...prev, { condition_id: `AC-${String(prev.length + 1).padStart(2, '0')}-${crypto.randomUUID().slice(0, 6)}`, kind: 'change', statement: '', source_refs: [], required: true, confirmation: 'unconfirmed', oracle: {} }])}><Plus size={14} />添加验收条件</button>}
      </div>{editable && <div className="ac-freeze-block"><label>范围排除项（每行一条）<textarea value={exclusions} onChange={(e) => setExclusions(e.target.value)} rows={2} placeholder="明确本次验收不覆盖的内容" /></label><div className="ac-form-actions">{error && <span className="ac-action-error"><CircleAlert size={14} />{error}</span>}{notice && <span className="ac-action-ok"><Check size={14} />{notice}</span>}<button className="btn" onClick={save} disabled={!!busy || !conditions.length}><Check size={14} />保存新版本</button><button className="btn btn-primary" onClick={freeze} disabled={!!busy || !conditions.length}><ShieldCheck size={14} />冻结验收条件</button></div><p className="ac-note">冻结后合同版本不可修改。调整标准需要创建新版本，历史验证将保留其原合同绑定。</p></div>}{!editable && <div className="ac-frozen-banner"><ShieldCheck size={16} />该合同已冻结。页面展示的是服务器记录的不可变版本。</div>}</>}
    </section></div>;
}
export function AcceptanceContractPage({ routeId, contractId: routeContractId }: { routeId?: string; contractId?: string }) {
  const { tasks, selected, setSelected, task } = useSelectedTask(routeId, 'contract');
  return <div className="ac-page"><PageHead eyebrow="ACCEPTANCE CONTRACT / 04" title="验收条件" lead="来源化维护验收标准，保存不可变版本并显式冻结；修复流程不能改写这些条件。" icon={BookOpenCheck} />
    <div className="ac-toolbar"><RemoteState loading={tasks.loading} error={tasks.error} onRetry={tasks.reload} />{!tasks.loading && !tasks.error && tasks.data?.length ? <TaskSelect tasks={tasks.data} value={selected} onChange={setSelected} /> : null}</div>
    {selected && <><RemoteState loading={task.loading} error={task.error} onRetry={task.reload} />{task.data && <><div className="ac-contract-task"><span className="ac-label">任务来源</span><strong>{taskTitle(task.data.task)}</strong><SourceRefs refs={task.data.task.source_refs} /></div><ContractEditor task={task.data.task} initialContractId={routeContractId} onSaved={task.reload} /></>}</>}
    {!tasks.loading && !tasks.error && !tasks.data?.length && <RemoteState loading={false} empty="当前没有可编辑验收合同的真实任务。" onRetry={tasks.reload} />}
  </div>;
}

export function CounterexamplePage({ routeId, candidateId: routeCandidateId }: { routeId?: string; candidateId?: string }) {
  const { tasks, selected, setSelected, task } = useSelectedTask(routeId, 'investigation');
  const [candidateId, setCandidateId] = useState(routeCandidateId || '');
  const candidates = task.data?.candidates || [];
  useEffect(() => {
    if (routeCandidateId && candidates.some((candidate) => candidate.candidate_id === routeCandidateId)) setCandidateId(routeCandidateId);
    else if (candidates.length && !candidates.some((candidate) => candidate.candidate_id === candidateId)) setCandidateId(candidates[0].candidate_id);
    else if (!candidates.length) setCandidateId('');
  }, [routeCandidateId, candidates, candidateId]);
  const verification = task.data?.latest_verification;
  const checks = useRemote(verification?.verification_id || 'no-verification', () => verification ? v1.checks(verification.verification_id) : Promise.reject(new Error('当前任务没有验证记录')));
  const findings = useRemote(`${selected || 'no-findings-task'}:${candidateId}`, () => selected ? v1.findings(selected, candidateId || undefined) : Promise.reject(new Error('请选择一个任务')));
  const [probePlan, setProbePlan] = useState<Awaited<ReturnType<typeof v1.probePlan>>>();
  const [probeBusy, setProbeBusy] = useState(false);
  const [probeError, setProbeError] = useState('');
  const createProbePlan = async () => {
    if (!selected) return;
    setProbeBusy(true); setProbeError('');
    try { setProbePlan(await v1.probePlan(selected, task.data?.task.issue_snapshot.title || '', 12)); }
    catch (error) { setProbeError(error instanceof Error ? error.message : String(error)); }
    finally { setProbeBusy(false); }
  };
  return <div className="ac-page"><PageHead eyebrow="COUNTEREXAMPLE INVESTIGATION / 03" title="反例调查" lead="沿着候选、合同、检查结果和证据来源检查失败或未知项；只有可验证证据才能形成判断。" icon={FileSearch} />
    <div className="ac-toolbar"><RemoteState loading={tasks.loading} error={tasks.error} onRetry={tasks.reload} />{!tasks.loading && !tasks.error && tasks.data?.length ? <><TaskSelect tasks={tasks.data} value={selected} onChange={(value) => { setSelected(value); setCandidateId(''); }} />{candidates.length > 1 && <label className="ac-select-label"><span>候选</span><select value={candidateId} aria-label="切换调查候选" onChange={(event) => { const value = event.target.value; setCandidateId(value); if (selected && value) go({ page: 'investigation', id: selected, candidateId: value }); }}><option value="">全部候选</option>{candidates.map((candidate) => <option key={candidate.candidate_id} value={candidate.candidate_id}>{candidate.candidate_id.slice(0, 25)} · {candidate.author_type}</option>)}</select></label>}</> : null}</div>
    {selected && <><RemoteState loading={task.loading} error={task.error} onRetry={task.reload} />{task.data && <><div className="ac-investigation-summary"><div><span className="ac-label">当前任务</span><strong>{taskTitle(task.data.task)}</strong><p>{task.data.task.issue_snapshot.repo_id} · {task.data.task.task_id}</p></div><div>{verification ? <>{fourStatus(verification)}<code className="ac-key-line">{verification.verification_key}</code></> : <Status label="验证记录" value="不存在" />}</div></div>
      {!verification ? <div className="ac-sheet ac-info-sheet"><CircleAlert size={18} /><div><strong>没有可调查的验证记录</strong><p>该任务当前没有服务端验证记录，不能推断其通过或失败。请先在代码审查页选择候选并创建验证任务。</p><RouteLink route={{ page: 'review', id: selected }}>前往代码审查</RouteLink></div></div> : <><div className="ac-sheet ac-gap-sheet"><div className="ac-section-title"><div><FileSearch size={16} /><h2>反例资源</h2></div><button className="btn" onClick={createProbePlan} disabled={probeBusy}>{probeBusy ? <LoaderCircle size={14} className="spin" /> : <Plus size={14} />}生成边界探针计划</button></div><p className="ac-note">计划只生成有界输入，不构成执行证据；执行后需保留重复观测并分类。</p>{probeError && <p className="ac-action-error"><CircleAlert size={14} />{probeError}</p>}{probePlan && <div className="ac-table-wrap"><table className="ac-table"><thead><tr><th>输入标签</th><th>输入</th><th>输入哈希</th></tr></thead><tbody>{probePlan.cases.map((item) => <tr key={item.input_hash}><td>{item.label}</td><td><code>{JSON.stringify(item.value)}</code></td><td><code>{item.input_hash.slice(0, 16)}…</code></td></tr>)}</tbody></table></div>}<RemoteState loading={findings.loading} error={findings.error} empty={!findings.loading && !findings.error && !findings.data?.available ? (findings.data?.reason || '反例执行资源暂不可用。') : findings.data?.items.length === 0 ? '暂无反例记录；这不构成“没有反例”或通过证明。' : undefined} onRetry={findings.reload} />{findings.data?.available && findings.data.items.length > 0 && <div className="ac-table-wrap"><table className="ac-table"><thead><tr><th>反例</th><th>状态</th><th>说明</th><th>证据</th></tr></thead><tbody>{findings.data.items.map((finding, i) => <tr key={finding.finding_id || i}><td><strong>{finding.title || finding.kind || finding.finding_id || `反例 ${i + 1}`}</strong></td><td><Status label="状态" value={finding.status || 'unknown'} tone={stateTone(finding.status)} /></td><td>{finding.message || '—'}</td><td><SourceRefs refs={finding.evidence_refs || []} /></td></tr>)}</tbody></table></div>}</div><RemoteState loading={checks.loading} error={checks.error} empty={!checks.loading && !checks.error && checks.data?.items.length === 0 ? `服务端目前没有持久化的逐项检查结果。${checks.data?.required_gaps.length ? ` 未完成项：${checks.data.required_gaps.join('、')}` : ''}` : undefined} onRetry={checks.reload} />{checks.data?.items.length ? <div className="ac-table-wrap"><table className="ac-table"><thead><tr><th>检查</th><th>结果</th><th>命令</th><th>退出码</th><th>证据引用</th></tr></thead><tbody>{checks.data.items.map((check, i) => <tr key={check.check_id || i}><td><strong>{check.variant} · {check.target}</strong><span className="ac-cell-sub">{check.suite_id} · {check.count} cases</span></td><td><Status label="结果" value={check.outcome || 'unknown'} tone={stateTone(check.outcome)} /></td><td><code>{check.command_argv?.join(' ') || '—'}</code></td><td>{check.return_code ?? '—'}</td><td><code>{check.details?.reason ? String(check.details.reason) : (check.duration_ms != null ? `${check.duration_ms} ms` : '—')}</code></td></tr>)}</tbody></table></div> : null}<section className="ac-sheet ac-gap-sheet"><h2><ShieldAlert size={16} />未完成与环境缺口</h2>{checks.data?.required_gaps.length ? <ul>{checks.data.required_gaps.map((gap) => <li key={gap}>{gap}</li>)}</ul> : <p>API 未报告 required_gaps；这不代表有通过证据，仍需以完整检查记录和验证结论为准。</p>}</section></>}
      </>}</>}
    {!tasks.loading && !tasks.error && !tasks.data?.length && <RemoteState loading={false} empty="暂无可调查的真实任务。" onRetry={tasks.reload} />}
  </div>;
}

export function DeliveryReportPage({ routeId, reportId: routeReportId }: { routeId?: string; reportId?: string }) {
  const { tasks, selected, setSelected, task } = useSelectedTask(routeId, 'report');
  const verification = task.data?.latest_verification;
  const checks = useRemote(verification?.verification_id || 'no-verification-report', () => verification ? v1.checks(verification.verification_id) : Promise.reject(new Error('当前任务没有验证记录')));
  const reports = useRemote(selected || 'no-reports-task', () => selected ? v1.reports(selected) : Promise.reject(new Error('请选择一个任务')));
  const [reportId, setReportIdState] = useState(routeReportId || ''); const [busy, setBusy] = useState(''); const [actionError, setActionError] = useState('');
  const setReportId = (value: string) => { setReportIdState(value); if (selected && value) go({ page: 'report', id: selected, reportId: value }); };
  const report = useRemote(reportId || 'no-report', () => reportId ? v1.report(reportId) : Promise.reject(new Error('请选择一份报告')));
  const content = useRemote(reportId || 'no-report-content', () => reportId ? v1.reportContent(reportId) : Promise.reject(new Error('请选择一份报告')));
  useEffect(() => {
    if (routeReportId && reports.data?.items.some((item) => item.report_id === routeReportId)) setReportIdState(routeReportId);
    else if (reports.data?.items.length && !reports.data.items.some((item) => item.report_id === reportId)) setReportId(reports.data.items[0].report_id);
  }, [reports.data, reportId, routeReportId]);
  const downloadReport = () => {
    if (!content.data?.content || !report.data?.report) return;
    const blob = new Blob([content.data.content], { type: report.data.report.format === 'html' ? 'text/html;charset=utf-8' : 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `patchpilot-${report.data.report.report_id}.${report.data.report.format === 'html' ? 'html' : 'md'}`;
    document.body.appendChild(link); link.click(); link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const create = async (format: 'markdown' | 'html') => {
    if (!verification) return; setBusy(format); setActionError('');
    try { const result = await v1.createReport(verification.verification_id, format); setReportId(result.report.report_id); reports.reload(); } catch (e) { setActionError(unwrapError(e)); } finally { setBusy(''); }
  };
  return <div className="ac-page"><PageHead eyebrow="EVIDENCE REPORT / 05" title="交付报告" lead="报告必须绑定确定的候选、冻结合同、verification_key 与真实检查证据。缺少证据时明确披露范围。" icon={FileText} />
    <div className="ac-toolbar"><RemoteState loading={tasks.loading} error={tasks.error} onRetry={tasks.reload} />{!tasks.loading && !tasks.error && tasks.data?.length ? <TaskSelect tasks={tasks.data} value={selected} onChange={(v) => { setSelected(v); setReportId(''); }} /> : null}</div>
    {selected && <><RemoteState loading={task.loading} error={task.error} onRetry={task.reload} />{task.data && <>{verification ? <>
      <div className="ac-report-actions"><div><span className="ac-label">当前验证</span><code>{verification.verification_id}</code><span className="ac-muted"> · key {verification.verification_key}</span></div><div>{actionError && <span className="ac-action-error"><CircleAlert size={14} />{actionError}</span>}<button className="btn" onClick={() => create('markdown')} disabled={!!busy}>{busy === 'markdown' ? <LoaderCircle size={14} className="spin" /> : <FileText size={14} />}生成 Markdown 报告</button><button className="btn" onClick={() => create('html')} disabled={!!busy}>{busy === 'html' ? <LoaderCircle size={14} className="spin" /> : <SquareArrowOutUpRight size={14} />}生成 HTML 报告</button></div></div>
      <RemoteState loading={reports.loading} error={reports.error} empty={!reports.loading && !reports.error && reports.data?.items.length === 0 ? '该验证尚未生成交付报告。只有通过真实报告 API 创建后才会显示。' : undefined} onRetry={reports.reload} />
      {!!reports.data?.items.length && <div className="ac-report-layout"><aside className="ac-sheet ac-report-list"><h2>已生成报告</h2>{reports.data.items.map((item) => <button className={`ac-report-item ${item.report_id === reportId ? 'selected' : ''}`} key={item.report_id} onClick={() => setReportId(item.report_id)}><strong>{item.format.toUpperCase()}</strong><span>{item.report_id}</span><small>{item.created_at}</small></button>)}</aside>{reportId && <article className="ac-report"><RemoteState loading={report.loading} error={report.error} onRetry={report.reload} />{report.data?.report && <><header><div><span className="ac-eyebrow"><FileText size={13} />VERIFICATION-BOUND RECORD</span><h2>{taskTitle(report.data.report.snapshot.task)}</h2><p>{report.data.report.snapshot.task.issue_snapshot.repo_id} · {report.data.report.report_id}</p></div><div className="ac-report-header-actions"><Status label="报告格式" value={report.data.report.format} /><button className="btn" onClick={downloadReport} disabled={!content.data?.content} aria-label="下载当前报告"><Download size={14} />下载报告</button></div></header><div className="ac-report-band"><div><span>报告绑定状态</span><div className="ac-four"><Status label="验证" value={report.data.report.verification_id} /><Status label="key" value={report.data.report.verification_key.slice(0, 16) + '…'} /><Status label="内容" value={report.data.report.content_sha.slice(0, 16) + '…'} /></div></div></div><section><h3>报告快照范围</h3><dl className="ac-report-facts"><div><dt>候选</dt><dd><code>{report.data.report.snapshot.verification.candidate_id}</code></dd></div><div><dt>验收合同</dt><dd><code>{report.data.report.snapshot.verification.contract_id}</code> · revision {report.data.report.snapshot.verification.contract_revision}</dd></div><div><dt>范围说明</dt><dd>{report.data.report.snapshot.scope_statement || '服务端未提供额外范围说明'}</dd></div></dl></section><section><h3>报告正文</h3><RemoteState loading={content.loading} error={content.error} onRetry={content.reload} />{content.data && <pre className="ac-code ac-report-content">{content.data.content}</pre>}</section><footer><span>创建于 {report.data.report.created_at}</span><span>content SHA256 <code>{report.data.report.content_sha}</code></span></footer></>}</article>}</div>}
    </> : <div className="ac-sheet ac-info-sheet"><FileText size={18} /><div><strong>当前任务没有可引用的验证记录</strong><p>此页面不会用静态演示内容生成报告。需要先建立来源清楚的验收合同和候选验证，报告才能绑定真实执行证据。</p><RouteLink route={{ page: 'review', id: selected }}>前往代码审查并创建验证记录</RouteLink></div></div>}</>}</>}
    {!tasks.loading && !tasks.error && !tasks.data?.length && <RemoteState loading={false} empty="暂无可生成交付报告的真实任务。" onRetry={tasks.reload} />}
  </div>;
}
