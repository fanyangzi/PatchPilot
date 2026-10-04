import { useEffect, useState } from 'react';
import type { ReactNode, CSSProperties } from 'react';
import { createRoot } from 'react-dom/client';
import { patchpilotApi, type ApiArtifact, type ApiEvent, type ApiGraphNode, type ApiRun } from './api';
import {
  Activity, AlertCircle, ArrowDownToLine, ArrowRight, ArrowUpRight, Ban, Beaker,
  Bot, Boxes, Check, CheckCircle2, ChevronDown, ChevronRight, CircleDot, Clock3,
  Code2, Command, Database, FileCode2, FileJson, GitBranch, GitPullRequest,
  HardDrive, Info, KeyRound, Layers3, LifeBuoy, ListChecks, Lock, Menu, MoreHorizontal,
  Network, Play, Plus, RefreshCw, RotateCcw, Search, ServerCog, Settings2, ShieldCheck,
  Sparkles, Square, Terminal, TestTube2, Timer, UploadCloud, Workflow, X, Zap
} from 'lucide-react';
import './styles.css';

type Page = 'runs' | 'detail' | 'new' | 'graph' | 'artifacts' | 'settings' | 'evaluation' | 'blocked' | 'review';
type Status = 'passed' | 'running' | 'review' | 'blocked' | 'queued';

const runs = [
  { id:'run_demo_01', title:'divide returns incorrect result', repo:'patchpilot/buggy-calculator', sha:'162b7b7', status:'passed' as Status, time:'2m 14s', retry:'1 retry', updated:'2 min ago', confidence:92, fixture:'Fixture A · happy path' },
  { id:'run_recovery_02', title:'parser drops escaped delimiters', repo:'patchpilot/flaky-parser', sha:'508f534', status:'review' as Status, time:'3m 08s', retry:'2 retries', updated:'18 min ago', confidence:74, fixture:'Fixture B · recovery' },
  { id:'run_risk_03', title:'release metadata injection', repo:'patchpilot/risky-change', sha:'ea28350', status:'blocked' as Status, time:'1m 42s', retry:'0 retries', updated:'42 min ago', confidence:41, fixture:'Fixture C · policy gate' },
  { id:'run_041', title:'timezone offset in report export', repo:'patchpilot/reporting', sha:'4ba2d11', status:'queued' as Status, time:'—', retry:'—', updated:'1 hr ago', confidence:0, fixture:'Public issue #118' },
];

const events = [
  { stage:'SNAPSHOT', icon:GitBranch, title:'Repository snapshot pinned', detail:'patchpilot/buggy-calculator @ 162b7b7', command:'git worktree add --detach .runs/run_demo_01 162b7b7', duration:'4.8s', status:'passed' as Status, artifact:'snapshot.json' },
  { stage:'PLAN', icon:Workflow, title:'Constraint-aware route selected', detail:'8 skills · policy gate passed · evidence gain 0.87', command:'route: inspect → reproduce → repair → verify', duration:'1.2s', status:'passed' as Status, artifact:'route.json' },
  { stage:'REPRODUCE', icon:TestTube2, title:'Issue reproduced', detail:'1 failed, 18 passed · target assertion captured', command:'pytest -q tests/test_calculator.py', duration:'12.4s', status:'passed' as Status, artifact:'repro.log' },
  { stage:'PATCH', icon:Code2, title:'Candidate patch generated', detail:'2 files changed · regression test added', command:'apply patch.diff --scope src/, tests/', duration:'8.7s', status:'passed' as Status, artifact:'patch.diff' },
  { stage:'VERIFY', icon:RefreshCw, title:'First verification failed · recovery started', detail:'REGRESSION · rolled back and rebuilt context', command:'pytest -q  →  1 failed: test_divide_zero_message', duration:'16.1s', status:'review' as Status, artifact:'failure.json' },
  { stage:'RECOVERY', icon:RotateCcw, title:'Recovery attempt 1 completed', detail:'Focused context + explicit exception contract', command:'rollback → enrich_context → regenerate', duration:'11.3s', status:'passed' as Status, artifact:'recovery-01.json' },
  { stage:'EVIDENCE', icon:ShieldCheck, title:'Evidence bundle sealed', detail:'5/5 checks passed · ready for maintainer review', command:'bundle --run run_demo_01 --sign local', duration:'3.1s', status:'passed' as Status, artifact:'evidence.zip' },
];

const skills = [
  { id:'issue', label:'Issue Intake', sub:'TaskSpec', kind:'input', x:34, y:52, status:'done' },
  { id:'snapshot', label:'Repo Snapshot', sub:'fixed commit', kind:'input', x:34, y:174, status:'done' },
  { id:'route', label:'Skill Router', sub:'constraint gate', kind:'skill', x:228, y:112, status:'done' },
  { id:'repro', label:'Reproduce Failure', sub:'pytest harness', kind:'skill', x:446, y:52, status:'done' },
  { id:'search', label:'Search Symbol', sub:'evidence pack', kind:'skill', x:446, y:174, status:'done' },
  { id:'patch', label:'Generate Patch', sub:'counterexample', kind:'skill', x:664, y:112, status:'done' },
  { id:'verify', label:'Verify + Recover', sub:'finite retries', kind:'verify', x:882, y:52, status:'done' },
  { id:'evidence', label:'Evidence Bundle', sub:'provenance DAG', kind:'verify', x:882, y:174, status:'done' },
];

type Run = typeof runs[number];
type Event = typeof events[number];
type Skill = typeof skills[number];

function normalizeStatus(value?: string): Status {
  const status = String(value || '').toLowerCase();
  if (status.includes('block')) return 'blocked';
  if (status.includes('review') || status.includes('need') || status.includes('fail')) return 'review';
  if (status.includes('run') || status.includes('progress')) return 'running';
  if (status.includes('queue')) return 'queued';
  return 'passed';
}

function displayRepo(value?: string): string {
  const raw = String(value || '').replace(/\\/g, '/').replace(/\/+$/, '');
  if (!raw || raw === '.' || raw === 'local run') return 'local run';
  if (raw.startsWith('patchpilot/')) return raw;
  const parts = raw.split('/').filter(Boolean);
  const name = parts[parts.length - 1] || 'repository';
  return `patchpilot/${name}`;
}

function displayTimestamp(value?: string | number): string {
  if (value == null || value === '') return 'just now';
  const raw = String(value).trim();
  const numeric = Number(raw);
  const date = Number.isFinite(numeric)
    ? new Date(numeric > 1_000_000_000_000 ? numeric : numeric * 1000)
    : new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date);
}

function redactHostPath(value?: string): string {
  return String(value || '').replace(/(?:\/Users\/[^\s"']+|\/private\/[^\s"']+|\/tmp\/[^\s"']+|[A-Za-z]:\\[^\s"']+)/g, '<local-path>');
}

function mapRun(item: ApiRun): Run {
  const status = normalizeStatus(item.conclusion || item.status);
  const id = item.id || item.run_id || 'remote-run';
  const runtime = item.duration || (item.runtime_sec != null ? `${Math.round(item.runtime_sec)}s` : '—');
  const retries = item.retries ?? item.retry_count;
  return { id, title: item.title || item.issue_title || id, repo: displayRepo(item.repo || item.repository || 'remote/repository'), sha: item.sha || item.commit || 'unknown', status, time: runtime, retry: retries == null ? '—' : `${retries} ${retries === 1 ? 'retry' : 'retries'}`, updated: displayTimestamp(item.updated || item.updated_at), confidence: item.confidence ?? item.patch_confidence ?? 0, fixture: item.fixture || 'Remote API run' };
}

function eventIcon(stage?: string) {
  const value = String(stage || '').toLowerCase();
  if (value.includes('snapshot')) return GitBranch;
  if (value.includes('plan') || value.includes('route')) return Workflow;
  if (value.includes('repro') || value.includes('test')) return TestTube2;
  if (value.includes('patch')) return Code2;
  if (value.includes('recover')) return RotateCcw;
  if (value.includes('evidence') || value.includes('risk')) return ShieldCheck;
  return RefreshCw;
}

function mapEvent(item: ApiEvent): Event {
  const stage = item.stage || item.kind || 'EVENT';
  return { stage: stage.toUpperCase(), icon: eventIcon(stage), title: redactHostPath(item.title || item.kind || 'Runtime event'), detail: redactHostPath(item.detail || item.summary || 'Event received from PatchPilot API'), command: redactHostPath(item.command || 'event recorded by orchestrator'), duration: item.duration || (item.runtime_sec != null ? `${Math.round(item.runtime_sec)}s` : '—'), status: normalizeStatus(item.status), artifact: item.artifact || item.artifact_ids?.[0] || 'event.json' };
}

function mapSkill(item: ApiGraphNode, index: number): Skill {
  const kind = item.kind === 'input' || item.kind === 'verify' ? item.kind : 'skill';
  return { id: item.id || `node-${index}`, label: item.label || item.name || `Skill ${index + 1}`, sub: item.sub || item.subtitle || 'runtime contract', kind, x: item.x ?? 34 + (index % 4) * 218, y: item.y ?? (index % 2 ? 174 : 52), status: 'done' };
}

function mapArtifact(item: ApiArtifact, index: number) {
  const kind = String(item.type || item.kind || '').toLowerCase();
  const Icon = kind.includes('test') ? TestTube2 : kind.includes('patch') || kind.includes('diff') ? Code2 : kind.includes('pr') ? GitPullRequest : kind.includes('sbom') || kind.includes('json') ? FileJson : kind.includes('script') ? Terminal : ShieldCheck;
  return { name: item.name || item.filename || item.title || `artifact-${index + 1}`, type: item.type || item.kind || 'Evidence artifact', size: typeof item.size === 'number' ? `${Math.round(item.size / 1024)} KB` : item.size || '—', detail: item.detail || item.status || 'linked to run', Icon };
}

function StatusPill({status, label}: {status: Status; label?: string}) {
  const map = {passed:['Passed','status-passed'], running:['Running','status-running'], review:['Needs review','status-review'], blocked:['Blocked','status-blocked'], queued:['Queued','status-queued']};
  const [text, cls] = map[status];
  return <span className={`status-pill ${cls}`}><span className="status-dot" />{label ?? text}</span>;
}

function Metric({label, value, note, tone='cyan', icon: Icon}: {label:string;value:string;note:string;tone?:string;icon: typeof Activity}) {
  return <div className="metric-card"><div className="metric-top"><span>{label}</span><Icon size={15}/></div><div className={`metric-value tone-${tone}`}>{value}</div><div className="metric-note">{note}</div></div>
}

function App() {
  const [page, setPage] = useState<Page>('runs');
  const [selectedRun, setSelectedRun] = useState(runs[0]);
  const [liveRuns, setLiveRuns] = useState<Run[]>(runs);
  const [liveEvents, setLiveEvents] = useState<Event[]>(events);
  const [liveSkills, setLiveSkills] = useState<Skill[]>(skills);
  const [liveArtifacts, setLiveArtifacts] = useState<ReturnType<typeof mapArtifact>[]>([]);
  const [apiLive, setApiLive] = useState(false);
  const [apiLoading, setApiLoading] = useState(true);
  const [apiError, setApiError] = useState<string | null>(null);
  const [expandedEvent, setExpandedEvent] = useState(4);
  const [selectedSkill, setSelectedSkill] = useState(skills[2]);
  const [showCommand, setShowCommand] = useState(false);
  const [demoPlaying, setDemoPlaying] = useState(false);
  const [railOpen, setRailOpen] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const remote = await patchpilotApi.listRuns();
        if (!remote.length) throw new Error('API returned no runs');
        const mapped = remote.map(mapRun);
        const active = mapped[0];
        const [eventItems, graph, artifactItems] = await Promise.all([
          patchpilotApi.getEvents(active.id).catch(() => []),
          patchpilotApi.getGraph(active.id).catch(() => ({ nodes: [], skills: [] })),
          patchpilotApi.getArtifacts(active.id).catch(() => []),
        ]);
        if (cancelled) return;
        setLiveRuns(mapped);
        setSelectedRun(active);
        if (eventItems.length) setLiveEvents(eventItems.map(mapEvent));
        const graphItems = graph.nodes || graph.skills || [];
        if (graphItems.length) setLiveSkills(graphItems.map(mapSkill));
        if (artifactItems.length) setLiveArtifacts(artifactItems.map(mapArtifact));
        setApiLive(true);
        setApiError(null);
      } catch (error) {
        if (cancelled) return;
        setApiLive(false);
        setApiError(error instanceof Error ? error.message : 'API unavailable');
      } finally {
        if (!cancelled) setApiLoading(false);
      }
    };
    void load();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!apiLive || !selectedRun.id) return;
    let cancelled = false;
    const refreshRunDetail = async () => {
      const [eventItems, graph, artifactItems] = await Promise.all([
        patchpilotApi.getEvents(selectedRun.id).catch(() => []),
        patchpilotApi.getGraph(selectedRun.id).catch(() => ({ nodes: [], skills: [] })),
        patchpilotApi.getArtifacts(selectedRun.id).catch(() => []),
      ]);
      if (cancelled) return;
      if (eventItems.length) setLiveEvents(eventItems.map(mapEvent));
      const graphItems = graph.nodes || graph.skills || [];
      if (graphItems.length) setLiveSkills(graphItems.map(mapSkill));
      if (artifactItems.length) setLiveArtifacts(artifactItems.map(mapArtifact));
    };
    void refreshRunDetail();
    return () => { cancelled = true; };
  }, [apiLive, selectedRun.id]);

  const nav = (next: Page) => setPage(next);
  const startDemoRun = async () => {
    setDemoPlaying(true);
    setPage('runs');
    try {
      const created = mapRun(await patchpilotApi.startDemo());
      setSelectedRun(created);
      setLiveRuns((previous) => [created, ...previous.filter((item) => item.id !== created.id)]);
      setApiLive(true);
    } catch {
      // Stable local playback remains the fallback for recording and offline review.
    }
  };
  const currentTitle = ({runs:'Runs',detail:'Run command center',new:'New run',graph:'Skill Graph',artifacts:'Artifacts',settings:'Settings',evaluation:'Evaluation lab',blocked:'Needs review',review:'Patch review'} as Record<Page,string>)[page];

  return <div className="app-shell">
    <header className="topbar">
      <div className="brand-wrap"><div className="brand-mark"><span>p</span></div><div><div className="brand">PatchPilot</div><div className="brand-caption">trusted maintenance runtime</div></div></div>
      <div className="workspace-select"><span className="workspace-dot"/> local / competition-lab <ChevronDown size={13}/></div>
      <div className="topbar-actions"><div className="connection"><span className="pulse"/> {apiLive?'API connected':'Demo playback'} </div><span className="model-tag"><Sparkles size={13}/> gpt-6.1-sol</span><button className="icon-button" onClick={()=>setShowCommand(true)} title="Command palette"><Command size={17}/><kbd>⌘K</kbd></button><div className="avatar">L</div></div>
    </header>
    <div className="app-body">
      <aside className="sidebar">
        <div className="sidebar-group"><div className="nav-label">Workspace</div>
          <NavItem icon={Activity} label="Runs" active={page==='runs' || page==='review' || page==='detail'} count="04" onClick={()=>nav('runs')}/>
          <NavItem icon={Plus} label="New run" active={page==='new'} onClick={()=>nav('new')}/>
          <NavItem icon={Boxes} label="Fixtures" active={false} badge="3" onClick={()=>nav('runs')}/>
        </div>
        <div className="sidebar-group"><div className="nav-label">System</div>
          <NavItem icon={Network} label="Skill Graph" active={page==='graph'} onClick={()=>nav('graph')}/>
          <NavItem icon={HardDrive} label="Artifacts" active={page==='artifacts'} onClick={()=>nav('artifacts')}/>
          <NavItem icon={Beaker} label="Evaluation lab" active={page==='evaluation'} onClick={()=>nav('evaluation')}/>
        </div>
        <div className="sidebar-spacer" />
        {page==='blocked' && <div className="blocked-mini" onClick={()=>nav('blocked')}><div className="blocked-mini-top"><AlertCircle size={15}/><span>Needs review</span><span className="blocked-count">1</span></div><div className="blocked-mini-text">release metadata injection</div></div>}
        <div className="sidebar-group sidebar-bottom"><NavItem icon={Settings2} label="Settings" active={page==='settings'} onClick={()=>nav('settings')}/><NavItem icon={LifeBuoy} label="Docs & support" active={false} onClick={()=>{}}/></div>
        <div className="runtime-card"><div className="runtime-head"><span className="runtime-label">RUNTIME</span><span className="runtime-live">LIVE</span></div><div className="runtime-row"><span><span className="mini-check"/> Docker Harness</span><span>ready</span></div><div className="runtime-row"><span><span className="mini-check"/> Evidence store</span><span>SQLite</span></div><div className="runtime-row"><span><span className="mini-check"/> Network policy</span><span>off</span></div></div>
      </aside>
      <main className={`main-workspace ${railOpen && (page==='runs'||page==='detail'||page==='review') ? 'with-rail':''}`}>
        {page==='runs' && <RunsPage runItems={liveRuns} selectedRun={selectedRun} onSelect={(r)=>{setSelectedRun(r); setPage('runs')}} onNew={()=>nav('new')} onDemo={()=>{setDemoPlaying(true);setPage('runs')}} playing={demoPlaying} onStop={()=>setDemoPlaying(false)} onReview={()=>nav('review')} onOpenDetail={()=>nav('detail')} loading={apiLoading} error={apiError} />}
        {page==='detail' && <RunDetail run={selectedRun} eventItems={liveEvents} onReview={()=>nav('review')} />}
        {page==='new' && <NewRunPage onBack={()=>nav('runs')} onStart={startDemoRun} />}
        {page==='graph' && <GraphPage graphSkills={liveSkills} selectedSkill={selectedSkill} onSelectSkill={setSelectedSkill} />}
        {page==='artifacts' && <ArtifactsPage artifactItems={liveArtifacts} onReview={()=>nav('review')} />}
        {page==='evaluation' && <EvaluationPage />}
        {page==='blocked' && <BlockedPage onBack={()=>nav('runs')} />}
        {page==='review' && <ReviewPage run={selectedRun} onBack={()=>nav('runs')} />}
        {page==='settings' && <SettingsPage />}
      </main>
      {(page==='runs'||page==='detail'||page==='review') && railOpen && <EvidenceRail run={selectedRun} onClose={()=>setRailOpen(false)} onBlocked={()=>nav('blocked')} />}
      {(page==='runs'||page==='detail'||page==='review') && !railOpen && <button className="rail-toggle" onClick={()=>setRailOpen(true)}><ShieldCheck size={15}/> Evidence</button>}
    </div>
    {showCommand && <CommandPalette onClose={()=>setShowCommand(false)} onNew={()=>{setShowCommand(false);nav('new')}} onReview={()=>{setShowCommand(false);nav('review')}} />}
  </div>
}

function NavItem({icon:Icon,label,active,count,badge,onClick}: {icon:typeof Activity;label:string;active:boolean;count?:string;badge?:string;onClick:()=>void}) { return <button className={`nav-item ${active?'active':''}`} onClick={onClick}><Icon size={17}/><span>{label}</span>{count&&<span className="nav-count">{count}</span>}{badge&&<span className="nav-badge">{badge}</span>}</button> }

function PageHeader({eyebrow,title,description,actions}: {eyebrow?:string;title:string;description?:string;actions?:ReactNode}) { return <div className="page-header"><div>{eyebrow&&<div className="eyebrow">{eyebrow}</div>}<h1>{title}</h1>{description&&<p>{description}</p>}</div>{actions&&<div className="page-actions">{actions}</div>}</div> }
function Button({children,primary=false,ghost=false,onClick,icon:Icon,disabled=false}: {children:ReactNode;primary?:boolean;ghost?:boolean;onClick?:()=>void;icon?:typeof Plus;disabled?:boolean}) { return <button disabled={disabled} onClick={onClick} className={`button ${primary?'primary':''} ${ghost?'ghost':''}`}>{Icon&&<Icon size={15}/>} {children}</button> }

function RunsPage({runItems,selectedRun,onSelect,onNew,onDemo,playing,onStop,onReview,onOpenDetail,loading,error}: {runItems:Run[];selectedRun:Run;onSelect:(r:Run)=>void;onNew:()=>void;onDemo:()=>void;playing:boolean;onStop:()=>void;onReview:()=>void;onOpenDetail:()=>void;loading?:boolean;error?:string|null}) {
  return <div className="page-content runs-page"><PageHeader title="Maintenance runs" description="From issue to evidence-backed patch." actions={<><Button ghost icon={Activity} onClick={onOpenDetail}>Command center</Button><Button ghost icon={Play} onClick={onDemo}>{playing?'Demo playing':'Open demo fixture'}</Button><Button primary icon={Plus} onClick={onNew}>New run</Button></>}/>
    <section className="hero-run-card"><div className="hero-run-main"><div className="hero-kicker"><span className="repo-glyph"><GitBranch size={13}/></span> CURRENT DEMO RUN <span className="divider-dot"/> {selectedRun.id}</div><div className="hero-title-row"><h2>{selectedRun.title}</h2><StatusPill status={playing?'running':selectedRun.status} label={playing?'Playback':undefined}/></div><div className="hero-meta"><span><Code2 size={14}/> {selectedRun.repo}</span><span><GitBranch size={14}/> {selectedRun.sha}</span><span><Clock3 size={14}/> {playing?'live playback':selectedRun.time}</span></div><div className="hero-steps"><Step label="Snapshot" done/><Step label="Plan" done/><Step label="Reproduce" done/><Step label="Patch" done/><Step label="Verify" done/><Step label="Evidence" done last/></div></div><div className="hero-confidence"><div className="confidence-ring" style={{'--progress':`${selectedRun.confidence}%`} as CSSProperties}><div><strong>{selectedRun.confidence}</strong><span>/100</span></div></div><div className="confidence-label">Patch confidence</div><div className="confidence-sub">Evidence complete</div><button className="text-link" onClick={onReview}>Open patch review <ArrowUpRight size={13}/></button></div></section>
    <div className="metrics-row"><Metric label="Reproduction" value="100%" note="issue reproduced in harness" icon={TestTube2}/><Metric label="Regression" value="18 / 18" note="suite passed after recovery" tone="violet" icon={ListChecks}/><Metric label="Evidence" value="5 / 5" note="checks bound to artifacts" tone="blue" icon={ShieldCheck}/><Metric label="Recovery" value="1 / 1" note="one controlled retry" tone="amber" icon={RotateCcw}/></div>
    <div className="content-grid"><section className="panel runs-panel"><div className="panel-heading"><div><h3>Recent runs</h3><span className="panel-sub">{loading?'Syncing with FastAPI…':error?'Demo fixtures · API unavailable':'Live runs from PatchPilot API'}</span></div><button className="more-button"><MoreHorizontal size={17}/></button></div><div className="run-table"><div className="run-table-head"><span>Run</span><span>State</span><span>Retries</span><span>Updated</span><span></span></div>{runItems.map(r=><button key={r.id} className={`run-row ${selectedRun.id===r.id?'selected':''}`} onClick={()=>onSelect(r)}><span className="run-name"><span className={`row-status ${r.status}`}/><span><strong>{r.title}</strong><small>{r.repo} · {r.sha}</small></span></span><StatusPill status={r.status}/><span className="muted mono">{r.retry}</span><span className="muted">{r.updated}</span><ChevronRight size={15} className="row-chevron"/></button>)}</div></section><section className="panel fixture-panel"><div className="panel-heading"><div><h3>Guided fixture</h3><span className="panel-sub">A stable path for your next review</span></div><Sparkles size={17} className="panel-icon"/></div><div className="fixture-visual"><div className="fixture-orbit orbit-one"/><div className="fixture-orbit orbit-two"/><div className="fixture-core"><ShieldCheck size={24}/></div></div><div className="fixture-copy"><strong>Issue → verified patch</strong><p>See the router, recovery branch, and evidence rail in one 90-second playback.</p></div><Button primary icon={Play} onClick={onDemo}>{playing?'Restart playback':'Run guided fixture'}</Button><div className="fixture-foot"><span><Timer size={13}/> ~90 seconds</span><span><Lock size={13}/> local playback</span></div></section></div>
  </div>
}
function Step({label,done,last}: {label:string;done?:boolean;last?:boolean}) { return <div className="step"><span className={`step-node ${done?'done':''}`}>{done&&<Check size={11}/>}</span><span>{label}</span>{!last&&<span className="step-line"/>}</div> }

function NewRunPage({onBack,onStart}:{onBack:()=>void;onStart:()=>void}) { const [step,setStep]=useState(1); return <div className="page-content new-page"><PageHeader eyebrow="CREATE A MAINTENANCE RUN" title="Start with a trusted context" description="Three decisions. One bounded execution path." actions={<Button ghost icon={X} onClick={onBack}>Cancel</Button>}/><div className="wizard-steps"><WizardStep num="01" label="Issue" active={step===1} done={step>1} onClick={()=>setStep(1)}/><WizardStep num="02" label="Policy" active={step===2} done={step>2} onClick={()=>setStep(2)}/><WizardStep num="03" label="Review" active={step===3} done={false} onClick={()=>setStep(3)}/></div><div className="new-layout"><section className="panel new-card">{step===1&&<><div className="card-kicker">STEP 01 / ISSUE</div><h2>What should PatchPilot maintain?</h2><p className="card-intro">Paste a public Issue or choose a pinned fixture. The run will be anchored to an immutable commit.</p><label className="field-label">Issue URL or fixture</label><div className="input-with-icon"><Search size={16}/><input defaultValue="fixture://buggy-calculator/issue-001" /></div><div className="parse-result"><div className="parse-icon"><GitPullRequest size={18}/></div><div><strong>divide returns incorrect result</strong><span>patchpilot/buggy-calculator · Python · 162b7b7</span></div><CheckCircle2 size={17} className="parse-check"/></div><div className="field-grid"><div><label className="field-label">Test command</label><div className="field-readonly"><Terminal size={14}/> pytest -q</div></div><div><label className="field-label">Pinned commit</label><div className="field-readonly mono"><GitBranch size={14}/> 162b7b7</div></div></div></>}{step===2&&<><div className="card-kicker">STEP 02 / POLICY</div><h2>Set the execution boundary</h2><p className="card-intro">The model proposes. Policy decides what may execute.</p><div className="policy-list"><PolicyRow icon={Network} title="Network access" value="Disabled" detail="No outbound calls inside harness"/><PolicyRow icon={FileCode2} title="Write scope" value="src/ · tests/" detail="Patch and regression test only"/><PolicyRow icon={Timer} title="Runtime budget" value="180 seconds" detail="60 seconds per command"/><PolicyRow icon={ShieldCheck} title="Maintainer gate" value="Required" detail="Never auto-merge a patch"/></div></>}{step===3&&<><div className="card-kicker">STEP 03 / REVIEW</div><h2>Everything is bounded.</h2><p className="card-intro">Review the plan before a single tool is invoked.</p><div className="review-summary"><SummaryRow label="Issue" value="divide returns incorrect result"/><SummaryRow label="Snapshot" value="buggy-calculator @ 162b7b7"/><SummaryRow label="Route" value="8 skills · policy gate passed"/><SummaryRow label="Model" value="gpt-6.1-sol · remote API"/><SummaryRow label="Artifacts" value="patch · test report · evidence bundle"/></div></>}</section><aside className="panel why-card"><div className="why-icon"><Info size={17}/></div><h3>{step===1?'Why pin a commit?':step===2?'Why set a policy?':'Why review first?'}</h3><p>{step===1?'A moving repository can make a correct patch impossible to reproduce. PatchPilot keeps the exact state, dependencies, and test command with every evidence record.':step===2?'Skills are granted the smallest tool access they need. This keeps model output inside an auditable execution boundary.':'The final review makes the model, harness, scope, and expected artifacts explicit before execution begins.'}</p><div className="why-divider"/><div className="why-detail"><span className="mini-check"/> Immutable task context</div><div className="why-detail"><span className="mini-check"/> Evidence-first output</div></aside></div><div className="wizard-footer"><button className="text-button" onClick={step===1?onBack:()=>setStep(step-1)}>{step===1?'← Back':'← Previous'}</button>{step<3?<Button primary icon={ArrowRight} onClick={()=>setStep(step+1)}>Continue</Button>:<Button primary icon={Play} onClick={onStart}>Start run</Button>}</div></div> }
function WizardStep({num,label,active,done,onClick}:{num:string;label:string;active:boolean;done:boolean;onClick:()=>void}){return <button className={`wizard-step ${active?'active':''}`} onClick={onClick}><span className={`wizard-num ${done?'done':''}`}>{done?<Check size={13}/>:num}</span><span>{label}</span></button>}
function PolicyRow({icon:Icon,title,value,detail}:{icon:typeof Network;title:string;value:string;detail:string}){return <div className="policy-row"><div className="policy-icon"><Icon size={16}/></div><div><strong>{title}</strong><span>{detail}</span></div><b>{value}</b><ChevronRight size={15}/></div>}
function SummaryRow({label,value}:{label:string;value:string}){return <div className="summary-row"><span>{label}</span><strong>{value}</strong></div>}

function RunDetail({run,eventItems,onReview}:{run:Run;eventItems:Event[];onReview:()=>void}) { const needsReview=run.status==='review'||run.status==='blocked'; const found=eventItems.findIndex((e)=>e.status==='blocked'||e.status==='review'); const focusIndex=found>=0?found:Math.min(4,Math.max(0,eventItems.length-1)); return <div className="run-detail"><div className="detail-head"><div><div className="detail-kicker"><span className="repo-glyph"><GitBranch size={12}/></span> {run.id} <span className="slash">/</span> {run.fixture}</div><h1>{run.title}</h1><div className="detail-meta"><span>{run.repo}</span><span>·</span><span className="mono">{run.sha}</span><span>·</span><StatusPill status={run.status} label={needsReview?'Needs review':'Ready for review'}/></div></div><div className="detail-actions"><Button ghost icon={ArrowDownToLine}>Export</Button><Button primary icon={GitPullRequest} onClick={onReview}>{needsReview?'Inspect review':'Review patch'}</Button></div></div><div className="stage-progress">{['Snapshot','Plan','Reproduce','Patch','Verify','Evidence'].map((s,i)=><div key={s} className="stage"><span className={`stage-dot ${needsReview&&s==='Evidence'?'wait':'done'}`}>{needsReview&&s==='Evidence'?<AlertCircle size={11}/>:<Check size={11}/>}</span><span>{s}</span>{i<5&&<span className={`stage-connector ${needsReview&&s==='Verify'?'wait':'done'}`}/>}</div>)}</div><div className="detail-body"><section className="timeline-col"><div className="timeline-head"><div><h2>Execution timeline</h2><span>Every action is bound to a policy and an artifact.</span></div><div className="live-tag"><span className="pulse"/> {eventItems.length?'API events':'replayed 2m 14s'}</div></div><div className="event-list">{eventItems.map((e,i)=><EventCard key={`${e.title}-${i}`} event={e} expanded={i===focusIndex} onToggle={()=>{}} />)}</div></section></div></div> }

function EventCard({event,expanded,onToggle}:{event:Event;expanded:boolean;onToggle:()=>void}){const Icon=event.icon; const blocked=event.status==='blocked'; const review=event.status==='review'; return <div className={`event-card ${expanded?'expanded':''} ${review||blocked?'event-review':''}`}><button className="event-main" onClick={onToggle}><span className={`event-icon ${event.status}`}><Icon size={16}/></span><span className="event-copy"><span className="event-stage">{event.stage}</span><strong>{event.title}</strong><span>{event.detail}</span></span><span className="event-time"><span>{event.duration}</span>{expanded?<ChevronDown size={15}/>:<ChevronRight size={15}/>}</span></button>{expanded&&<div className="event-expanded"><div className="event-command"><Terminal size={13}/><code>{event.command}</code><span className="exit-badge">{blocked?'blocked → review':review?'failed → recovery':'recorded'}</span></div>{blocked?<div className="recovery-callout"><div className="recovery-icon"><AlertCircle size={14}/></div><div><strong>Policy gate blocked delivery</strong><p>The verifier kept the evidence bundle, but the candidate crossed a declared boundary. A maintainer must review the run before delivery.</p></div><ArrowRight size={15}/></div>:<div className="recovery-callout"><div className="recovery-icon"><RotateCcw size={14}/></div><div><strong>{review?'Controlled recovery':'Execution evidence'}</strong><p>{review?'The failure was classified and the worktree can be retried with bounded recovery.':'This event is linked to the run evidence graph and remains reproducible.'}</p></div><ArrowRight size={15}/></div>}<div className="event-links"><span><FileJson size={13}/> {event.artifact}</span><span><Activity size={13}/> Open full log</span></div></div>}</div>}

function EvidenceRail({run,onClose,onBlocked}:{run:Run;onClose:()=>void;onBlocked:()=>void}){const needsReview=run.status==='review'||run.status==='blocked'; const evidence=[['Issue reproduced','ev_0017','12.4s',true],['Patch scope within policy','ev_0021','0.3s',!needsReview],['Target test passed','ev_0024','4.2s',!needsReview],['Regression suite passed','ev_0025','18 tests',!needsReview],['License / SBOM checked','ev_0028','1.1s',!needsReview]] as const; const passed=evidence.filter(([,,,ok])=>ok).length; return <aside className="evidence-rail"><div className="rail-head"><div><div className="rail-kicker"><ShieldCheck size={13}/> EVIDENCE RAIL</div><h2>{needsReview?'Needs maintainer review':'Trusted delivery'}</h2></div><button className="icon-button small" onClick={onClose}><X size={16}/></button></div><div className="rail-score"><div className="rail-score-value">{passed}<span>/{evidence.length}</span></div><div><strong>{needsReview?'Evidence incomplete':'Evidence complete'}</strong><span>{needsReview?'A policy boundary needs review.':'Every claim has a source.'}</span></div></div><div className="evidence-list">{evidence.map(([title,id,time,pass])=><button className="evidence-item" key={id}><span className={`evidence-check ${pass?'pass':''}`}>{pass?<Check size={12}/>:<X size={12}/>}</span><span><strong>{title}</strong><small>{id} · {time}</small></span><ChevronRight size={14}/></button>)}</div><div className="rail-divider"/><div className="rail-section-title">RUN POLICY</div><div className="rail-policy"><PolicyCheck label="Network disabled" icon={Network}/><PolicyCheck label="Write scope enforced" icon={Lock}/><PolicyCheck label="Maintainer review required" icon={GitPullRequest}/></div><div className="rail-note"><div className="rail-note-icon"><Sparkles size={14}/></div><div><strong>Why this matters</strong><p>A patch is only trusted when the issue, change, and verification evidence travel together.</p></div></div><button className="rail-blocked" onClick={onBlocked}><AlertCircle size={14}/> {needsReview?'Open review state':'Preview blocked state'} <ArrowRight size={14}/></button></aside>}
function PolicyCheck({label,icon:Icon}:{label:string;icon:typeof Network}){return <div><span className="mini-check"><Icon size={10}/></span>{label}</div>}

function GraphPage({graphSkills,selectedSkill,onSelectSkill}:{graphSkills:Skill[];selectedSkill:Skill;onSelectSkill:(s:Skill)=>void}){return <div className="page-content graph-page"><PageHeader eyebrow="ROUTING SYSTEM" title="Constraint-aware Skill Graph" description="The model proposes. Contracts and policy decide." actions={<Button ghost icon={UploadCloud}>Export graph</Button>}/><div className="graph-layout"><section className="panel graph-panel"><div className="graph-toolbar"><div><span className="live-tag"><span className="pulse"/> run_demo_01</span><span className="graph-caption">{graphSkills.length} nodes · 7 edges · all contracts satisfied</span></div><div className="graph-legend"><span><i className="legend-dot input"/> context</span><span><i className="legend-dot skill"/> skill</span><span><i className="legend-dot verify"/> evidence</span></div></div><div className="dag"><svg className="dag-lines" viewBox="0 0 1070 280" preserveAspectRatio="none"><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L0,6 L6,3 z" fill="#5de4d0"/></marker></defs><path d="M160 82 C210 82, 210 142, 228 142"/><path d="M160 204 C210 204, 210 142, 228 142"/><path d="M410 142 C430 142, 425 82, 446 82"/><path d="M410 142 C430 142, 425 204, 446 204"/><path d="M628 82 C650 82, 650 142, 664 142"/><path d="M628 204 C650 204, 650 142, 664 142"/><path d="M846 142 C866 142, 865 82, 882 82"/><path d="M846 142 C866 142, 865 204, 882 204"/></svg>{graphSkills.map(s=><button key={s.id} onClick={()=>onSelectSkill(s)} className={`skill-node ${s.kind} ${selectedSkill.id===s.id?'selected':''}`} style={{left:s.x,top:s.y}}><span className="skill-node-icon">{s.kind==='input'?<Database size={15}/>:s.kind==='verify'?<ShieldCheck size={15}/>:<Zap size={15}/>}</span><span><strong>{s.label}</strong><small>{s.sub}</small></span><span className="node-check"><Check size={10}/></span></button>)}</div><div className="graph-footer"><span><CheckCircle2 size={14}/> route verified against policy</span><span className="mono">score 0.87 · 8/8 evidence links</span></div></section><aside className="panel inspector"><div className="inspector-kicker">SKILL INSPECTOR</div><div className="inspector-title"><div className={`inspector-icon ${selectedSkill.kind}`}><Zap size={18}/></div><div><h2>{selectedSkill.label}</h2><span>{selectedSkill.sub} · v0.4.2</span></div></div><div className="route-score"><div><span>Route score</span><strong>0.87</strong></div><div className="score-bar"><i style={{width:'87%'}}/></div><small>Highest evidence gain for current state</small></div><InspectorRow label="Why selected" value="Test command is known and network is disabled."/><InspectorRow label="Preconditions" value="python_project · test_command_known"/><InspectorRow label="Allowed tools" value="repo.read · harness.run"/><InspectorRow label="Risk" value="low · reversible"/><InspectorRow label="Success signal" value="repro_confirmed · failure_captured"/><div className="inspector-evidence"><div className="rail-section-title">EVIDENCE PRODUCED</div><div><FileJson size={14}/> ev_0017 <span>repro.log</span></div></div></aside></div></div>}
function InspectorRow({label,value}:{label:string;value:string}){return <div className="inspector-row"><span>{label}</span><strong>{value}</strong></div>}

function CheckLine({label,detail}:{label:string;detail:string}){return <div className="check-line"><span><span className="evidence-check pass"><Check size={11}/></span><strong>{label}</strong></span><small>{detail}</small></div>}

function ReviewPage({run,onBack}:{run:Run;onBack:()=>void}){const needsReview=run.status==='review'||run.status==='blocked'; const score=run.confidence; return <div className="page-content review-page"><PageHeader eyebrow="DELIVERY REVIEW" title={needsReview?'Maintainer review required':'Ready for maintainer review'} description={needsReview?'The evidence chain is preserved, but the policy gate did not approve delivery.':'The patch, test, and evidence chain are linked.'} actions={<><Button ghost icon={ArrowDownToLine}>Download bundle</Button><Button primary icon={GitPullRequest} disabled={needsReview}>Create PR draft</Button></>}/><div className={`review-banner ${needsReview?'review-banner-warning':''}`}><div className="banner-check">{needsReview?<AlertCircle size={18}/>:<Check size={18}/>}</div><div><strong>{needsReview?'Needs review · ':'Verified patch · '}{score} / 100 confidence</strong><span>{needsReview?'Policy boundary reached; no PR draft is marked ready.':'Issue reproduced · regression suite passed · policy scope clean'}</span></div><StatusPill status={run.status} label={needsReview?'Needs review':'Evidence complete'}/></div><div className="review-layout"><section className="panel diff-panel"><div className="diff-head"><div><h3>Patch diff</h3><span className="panel-sub">{run.repo} · {run.sha} · selected run</span></div><div className="diff-tabs"><button className="active">Unified</button><button>Split</button></div></div><div className="diff-file"><div className="file-row"><FileCode2 size={14}/> candidate patch <span className={`file-status ${needsReview?'warning':''}`}>{needsReview?'review required':'policy checked'}</span></div><pre className="diff-code"><code><span className="line"><i>—</i> <em>{run.title}</em></span><span className="line added"><i>+</i> <b>Evidence-bound change captured in the selected run.</b></span><span className="line added"><i>+</i> <b>{needsReview?'Maintainer review is required before delivery.':'Patch scope and verification checks passed.'}</b></span></code></pre></div><div className="diff-file"><div className="file-row"><TestTube2 size={14}/> evidence chain <span className={`file-status ${needsReview?'warning':''}`}>{needsReview?'incomplete':'sealed'}</span></div><pre className="diff-code small"><code><span className="line"><i>•</i> reproduction · {run.fixture}</span><span className="line"><i>•</i> commit · {run.sha}</span><span className="line"><i>•</i> conclusion · {needsReview?'NEEDS_REVIEW':'TRUSTED_DELIVERY'}</span></code></pre></div></section><aside className="panel review-summary-panel"><h3>Verification summary</h3><div className="summary-score"><strong>{score}</strong><span>/100</span><div className="score-bar"><i style={{width:`${score}%`}}/></div></div><div className="check-list"><CheckLine label="Issue reproduced" detail="evidence linked"/><CheckLine label="Target test" detail={needsReview?'awaiting maintainer review':'passed'}/><CheckLine label="Regression suite" detail={needsReview?'not approved':'passed'}/><CheckLine label="Diff scope" detail={needsReview?'policy boundary':'approved'}/><CheckLine label="License / SBOM" detail={needsReview?'review required':'checked'}/></div><div className="review-links"><button><FileJson size={14}/> evidence.json <ArrowUpRight size={12}/></button><button><Terminal size={14}/> reproduce.sh <ArrowUpRight size={12}/></button><button><GitPullRequest size={14}/> pr_draft.md <ArrowUpRight size={12}/></button></div></aside></div><button className="text-button" onClick={onBack}>← Back to runs</button></div>}

function ArtifactsPage({artifactItems,onReview}:{artifactItems:ReturnType<typeof mapArtifact>[];onReview:()=>void}){const arts=artifactItems.length?artifactItems:[{name:'evidence.zip',type:'Evidence bundle',size:'42 KB',detail:'sealed · 5 evidence links',Icon:ShieldCheck},{name:'patch.diff',type:'Unified patch',size:'1.8 KB',detail:'2 files · policy clean',Icon:Code2},{name:'test-report.html',type:'Test report',size:'8 KB',detail:'18 passed · 1 recovered',Icon:TestTube2},{name:'pr_draft.md',type:'PR draft',size:'3 KB',detail:'ready for review',Icon:GitPullRequest},{name:'sbom.cdx.json',type:'SBOM',size:'12 KB',detail:'MIT · 0 findings',Icon:FileJson},{name:'reproduce.sh',type:'Reproduction script',size:'0.7 KB',detail:'deterministic fixture',Icon:Terminal}]; return <div className="page-content"><PageHeader title="Artifacts" description="Every output is traceable to a run and a source event." actions={<Button primary icon={ArrowDownToLine}>Download all</Button>}/><div className="artifact-toolbar"><div className="search-box"><Search size={15}/><input placeholder="Search artifacts"/></div><span className="toolbar-meta">{arts.length} artifacts · run_demo_01</span></div><div className="artifact-grid">{arts.map((item)=><button className="artifact-card" key={item.name} onClick={item.type==='Unified patch'?onReview:undefined}><div className="artifact-icon"><item.Icon size={19}/></div><div className="artifact-info"><strong>{item.name}</strong><span>{item.type}</span><small>{item.size} · {item.detail}</small></div><ArrowDownToLine size={15} className="artifact-download"/></button>)}</div></div>}

function EvaluationPage(){return <div className="page-content"><PageHeader eyebrow="EVIDENCE LAB" title="Evaluation lab" description="Measured comparisons across nine pinned tasks." actions={<Button ghost icon={RefreshCw}>Run evaluation</Button>}/><div className="eval-hero panel"><div><div className="eyebrow">CURRENT SNAPSHOT</div><h2>Full runtime preserves evidence and policy boundaries</h2><p>9 tasks · 3 fixtures · 3 controlled runners · 27 measured rows</p></div><div className="eval-date"><span>Last run</span><strong>Generated from artifacts/eval</strong><span className="status-passed-text">● reproducible</span></div></div><div className="eval-grid"><section className="panel chart-panel"><div className="panel-heading"><div><h3>Repair outcomes</h3><span className="panel-sub">Trusted delivery rate · fixed task set</span></div><span className="chart-legend"><i className="bar-key direct"/> Direct LLM <i className="bar-key linear"/> Linear agent <i className="bar-key full"/> PatchPilot</span></div><div className="bar-chart"><div className="y-axis"><span>100%</span><span>75%</span><span>50%</span><span>25%</span><span>0%</span></div><div className="bars-area"><div className="gridline g1"/><div className="gridline g2"/><div className="gridline g3"/><div className="gridline g4"/><div className="bar-group"><Bar h="44" label="Direct LLM" values={['44%']} tone="direct"/><Bar h="78" label="Linear agent" values={['78%']} tone="linear"/><Bar h="78" label="PatchPilot" values={['78%']} tone="full"/></div><div className="bar-labels"><span>trusted delivery rate · measured</span></div></div></div></section><section className="panel score-panel"><div className="panel-heading"><div><h3>Ablation signal</h3><span className="panel-sub">Evidence and policy behavior</span></div></div><Ablation label="Skill Graph" value="12 nodes" width="88%" tone="violet"/><Ablation label="Verifier" value="100% gate" width="91%" tone="cyan"/><Ablation label="Recovery" value="3 / 3" width="64%" tone="amber"/><Ablation label="Evidence links" value="0.91 mean" width="91%" tone="blue"/><div className="score-foot"><Info size={13}/> Values are generated from the current JSONL, CSV and SVG artifacts; the sample is small.</div></section></div><div className="metric-strip"><Metric label="First pass" value="44%" note="4 / 9 PatchPilot tasks" tone="violet" icon={Zap}/><Metric label="Recovery success" value="100%" note="3 / 3 controlled retries" tone="amber" icon={RotateCcw}/><Metric label="Policy gate" value="100%" note="risk paths reviewed" tone="blue" icon={Activity}/><Metric label="Evidence completeness" value="91%" note="PatchPilot mean" icon={ShieldCheck}/></div></div>}
function Bar({h,label,values,tone}:{h:string;label:string;values:string[];tone:string}){return <div className="bar-col"><div className={`bar ${tone}`} style={{height:`${h}%`}}><span>{values[0]}</span></div><small>{label}</small></div>}
function Ablation({label,value,width,tone}:{label:string;value:string;width:string;tone:string}){return <div className="ablation"><div><span>{label}</span><strong>{value}</strong></div><div className="ablation-track"><i className={tone} style={{width}}/></div></div>}

function BlockedPage({onBack}:{onBack:()=>void}){return <div className="page-content blocked-page"><PageHeader eyebrow="POLICY GATE" title="Needs maintainer review" description="A boundary was reached. The system kept the evidence it could prove." actions={<Button ghost icon={ArrowDownToLine}>Export review pack</Button>}/><div className="blocked-hero"><div className="blocked-symbol"><Ban size={28}/></div><div><div className="blocked-label">BLOCKED · LICENSE_OR_RISK</div><h2>Patch touched a protected release path</h2><p>release metadata injection attempted to modify <code>docs/release_notes.md</code>, outside the declared write scope.</p></div><StatusPill status="blocked"/></div><div className="blocked-grid"><section className="panel blocked-story"><div className="story-step"><span className="story-num pass"><Check size={13}/></span><div><strong>Issue reproduced</strong><p>Original failure captured in the isolated harness.</p></div></div><div className="story-step"><span className="story-num pass"><Check size={13}/></span><div><strong>Candidate patch generated</strong><p>Model proposed a change with a clean local diff.</p></div></div><div className="story-step"><span className="story-num fail"><X size={13}/></span><div><strong>Policy gate blocked delivery</strong><p>The diff crossed a protected path. No PR draft was marked ready.</p></div></div><div className="story-step"><span className="story-num wait"><Clock3 size={13}/></span><div><strong>Next action</strong><p>Maintainer reviews the file scope and either widens policy or rejects the patch.</p></div></div></section><aside className="panel next-actions"><h3>What you can do next</h3><Button primary icon={Search}>Inspect diff</Button><Button ghost icon={Settings2}>Adjust policy</Button><Button ghost icon={ArrowDownToLine}>Download evidence</Button><div className="safe-note"><Lock size={14}/><span>No artifact was deleted. The worktree is preserved for review.</span></div></aside></div><button className="text-button" onClick={onBack}>← Back to runs</button></div>}

function SettingsPage(){return <div className="page-content"><PageHeader title="Settings" description="Runtime preferences for this local workspace."/><div className="settings-grid"><section className="panel settings-card"><div className="setting-heading"><div className="setting-icon"><KeyRound size={17}/></div><div><h3>Model connection</h3><span>OpenAI-compatible remote adapter</span></div><span className="connected-pill">connected</span></div><div className="settings-row"><span>Base URL</span><code>https://hk.getelucid.com/v1</code></div><div className="settings-row"><span>Model</span><code>gpt-6.1-sol</code></div><div className="settings-row"><span>Credential</span><span className="redacted">••••••••••••••••</span></div></section><section className="panel settings-card"><div className="setting-heading"><div className="setting-icon"><ServerCog size={17}/></div><div><h3>Execution harness</h3><span>Bounded local runtime</span></div><span className="connected-pill">ready</span></div><div className="settings-row"><span>Container</span><code>patchpilot/python:3.12</code></div><div className="settings-row"><span>Network</span><span className="setting-good"><Check size={13}/> disabled by default</span></div><div className="settings-row"><span>Artifact store</span><code>SQLite WAL + JSONL</code></div></section><section className="panel settings-card full"><div className="setting-heading"><div className="setting-icon"><ShieldCheck size={17}/></div><div><h3>Policy defaults</h3><span>Applied to new runs</span></div></div><div className="toggle-row"><div><strong>Require maintainer review</strong><span>Never auto-merge generated patches</span></div><div className="toggle on"><i/></div></div><div className="toggle-row"><div><strong>Record evidence provenance</strong><span>Bind every claim to an artifact ID</span></div><div className="toggle on"><i/></div></div></section></div></div>}

function CommandPalette({onClose,onNew,onReview}:{onClose:()=>void;onNew:()=>void;onReview:()=>void}){return <div className="modal-backdrop" onClick={onClose}><div className="command-palette" onClick={e=>e.stopPropagation()}><div className="command-search"><Search size={17}/><input autoFocus placeholder="Type a command..."/><kbd>esc</kbd></div><div className="command-section">QUICK ACTIONS</div><button onClick={onNew}><Plus size={16}/><span><strong>New maintenance run</strong><small>Start with a pinned Issue and policy</small></span><kbd>⌘ N</kbd></button><button onClick={onReview}><GitPullRequest size={16}/><span><strong>Open current patch review</strong><small>Inspect diff and evidence</small></span><kbd>⌘ R</kbd></button><button onClick={onClose}><ShieldCheck size={16}/><span><strong>Toggle evidence rail</strong><small>Show or hide trusted delivery checks</small></span><kbd>⌘ E</kbd></button><div className="command-footer"><span><Command size={12}/> Navigate</span><span><ArrowRight size={12}/> Select</span><span><X size={12}/> Close</span></div></div></div>}

createRoot(document.getElementById('root')!).render(<App />);

export default App;
