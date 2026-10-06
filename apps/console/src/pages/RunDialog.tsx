import { useEffect, useRef, useState } from 'react';
import { CircleAlert, LoaderCircle, Play, X } from 'lucide-react';
import { api, type RunRecord, type Scenario } from '../api';
import { SCENARIO } from '../model';
import { Loading, Offline, useAsync } from '../ui';

export function RunDialog({ onClose, onFinished }: { onClose: () => void; onFinished: (run: RunRecord) => void }) {
  const tasks = useAsync(() => api.tasks(), []);
  const [mode, setMode] = useState<'fixture' | 'repository'>('fixture');
  const [picked, setPicked] = useState<string | null>(null);
  const [repo, setRepo] = useState('');
  const [commit, setCommit] = useState('HEAD');
  const [issueTitle, setIssueTitle] = useState('');
  const [issueBody, setIssueBody] = useState('');
  const [testCommand, setTestCommand] = useState('pytest -x -q');
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => { ref.current?.showModal(); }, []);
  useEffect(() => {
    if (!picked && tasks.data?.length) setPicked(tasks.data[0].task_id);
  }, [tasks.data, picked]);

  const start = async () => {
    if (mode === 'fixture' && !picked) return;
    if (mode === 'repository' && (!repo.trim() || !issueTitle.trim() || !commit.trim())) return;
    setRunning(true); setError(null);
    try {
      const result = mode === 'fixture'
        ? await api.startRun(picked!)
        : await api.startAdHocRun({ repo: repo.trim(), commit: commit.trim(), issue_title: issueTitle.trim(), issue_body: issueBody.trim(), test_command: testCommand.trim() || undefined });
      onFinished(result);
    }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); setRunning(false); }
  };

  const groups = (['normal', 'failure', 'risk'] as Scenario[]).map((s) => ({ s, items: (tasks.data || []).filter((t) => t.scenario === s) }));

  return <dialog ref={ref} className="dialog" onCancel={(e) => { e.preventDefault(); if (!running) onClose(); }} aria-labelledby="dialog-title">
    <header className="dialog-head">
      <div className="dialog-title">
        <Play size={18} />
        <div>
          <h2 id="dialog-title">运行一个任务</h2>
          <small>在隔离工作区里完整走一遍流水线</small>
        </div>
      </div>
      <button className="icon-btn" onClick={onClose} disabled={running} aria-label="关闭"><X size={18} /></button>
    </header>

    {tasks.loading && mode === 'fixture' ? <Loading label="正在读取任务列表" /> : tasks.error && mode === 'fixture' ? <Offline message={tasks.error} onRetry={tasks.reload} /> : running ? <div className="dialog-running" aria-live="polite">
      <LoaderCircle className="spin" size={22} />
      <div>
        <strong>正在沙箱中执行</strong>
        <p>复现、补丁、验证和封存证据通常需要几秒到几十秒，完成后会直接打开这次运行的完整记录。</p>
      </div>
    </div> : <>
      <p className="dialog-sub">选择固定夹具，或输入一个本地仓库路径启动真实运行。每次结果都会写入证据库。</p>
      <div className="mode-tabs" role="tablist" aria-label="运行来源">
        <button role="tab" aria-selected={mode === 'fixture'} className={mode === 'fixture' ? 'mode-tab on' : 'mode-tab'} onClick={() => setMode('fixture')}>固定夹具</button>
        <button role="tab" aria-selected={mode === 'repository'} className={mode === 'repository' ? 'mode-tab on' : 'mode-tab'} onClick={() => setMode('repository')}>本地仓库</button>
      </div>
      {mode === 'fixture' ? <div className="task-groups">
          {groups.map(({ s, items }) => {
            const Icon = SCENARIO[s].icon;
            return items.length > 0 && <fieldset key={s} className="task-group">
              <legend>
                <Icon size={14} />
                {SCENARIO[s].name}
                <span>{SCENARIO[s].promise}</span>
              </legend>
              {items.map((t) => <label key={t.task_id} className={picked === t.task_id ? 'task on' : 'task'}>
                <input type="radio" name="task" value={t.task_id} checked={picked === t.task_id} onChange={() => setPicked(t.task_id)} />
                <span><strong>{t.issue_title}</strong><small>{t.repo}，commit <code>{t.commit}</code></small></span>
              </label>)}
            </fieldset>;
          })}
        </div> : <div className="adhoc-form">
          <label><span>仓库路径</span><input value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="/Users/you/projects/repository" /></label>
          <label><span>基线 commit</span><input value={commit} onChange={(e) => setCommit(e.target.value)} placeholder="HEAD 或 commit SHA" /></label>
          <label><span>Issue 标题</span><input value={issueTitle} onChange={(e) => setIssueTitle(e.target.value)} placeholder="要修复的问题" /></label>
          <label><span>Issue 描述</span><textarea value={issueBody} onChange={(e) => setIssueBody(e.target.value)} rows={4} placeholder="复现步骤、预期行为和验收条件" /></label>
          <label><span>测试命令</span><input value={testCommand} onChange={(e) => setTestCommand(e.target.value)} placeholder="pytest -x -q" /></label>
          <p className="form-hint">本地仓库模式只接受后端进程所在机器可以访问的路径；网络权限仍由任务策略决定。</p>
        </div>}
      {(error || (mode === 'fixture' && tasks.data?.length === 0)) && <p className="dialog-error" role="alert">
        <CircleAlert size={14} />{error ? `运行失败：${error}` : '没有可运行的任务，请检查 fixtures/tasks 目录。'}
      </p>}
    </>}

    <footer className="dialog-foot">
      <button className="btn" onClick={onClose} disabled={running}>取消</button>
      <button className="btn btn-primary" onClick={start} disabled={(mode === 'fixture' ? !picked || !!tasks.error : !repo.trim() || !issueTitle.trim() || !commit.trim()) || running}>{running ? '执行中' : '开始运行'}</button>
    </footer>
  </dialog>;
}
