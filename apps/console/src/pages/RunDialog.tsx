import { useEffect, useRef, useState } from 'react';
import { LoaderCircle, X } from 'lucide-react';
import { api, type RunRecord, type Scenario } from '../api';
import { SCENARIO } from '../model';
import { Loading, Offline, useAsync } from '../ui';

export function RunDialog({ onClose, onFinished }: { onClose: () => void; onFinished: (run: RunRecord) => void }) {
  const tasks = useAsync(() => api.tasks(), []);
  const [picked, setPicked] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => { ref.current?.showModal(); }, []);
  useEffect(() => {
    if (!picked && tasks.data?.length) setPicked(tasks.data[0].task_id);
  }, [tasks.data, picked]);

  const start = async () => {
    if (!picked) return;
    setRunning(true); setError(null);
    try { onFinished(await api.startRun(picked)); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); setRunning(false); }
  };

  const groups = (['normal', 'failure', 'risk'] as Scenario[]).map((s) => ({ s, items: (tasks.data || []).filter((t) => t.scenario === s) }));

  return <dialog ref={ref} className="dialog" onCancel={(e) => { e.preventDefault(); if (!running) onClose(); }} aria-labelledby="dialog-title">
    <header className="dialog-head">
      <h2 id="dialog-title">运行一个任务</h2>
      <button className="icon-btn" onClick={onClose} disabled={running} aria-label="关闭"><X size={18} /></button>
    </header>

    {tasks.loading ? <Loading label="正在读取任务列表" /> : tasks.error ? <Offline message={tasks.error} onRetry={tasks.reload} /> : running ? <div className="dialog-running" aria-live="polite">
      <LoaderCircle className="spin" size={22} />
      <div>
        <strong>正在沙箱中执行</strong>
        <p>复现、补丁、验证和封存证据通常需要几秒到几十秒，完成后会直接打开这次运行的完整记录。</p>
      </div>
    </div> : <>
      <p className="dialog-sub">选择一个固定 commit 的任务。PatchPilot 会在隔离工作区里完整执行一次，结果写入证据库。</p>
      <div className="task-groups">
        {groups.map(({ s, items }) => items.length > 0 && <fieldset key={s} className="task-group">
          <legend>{SCENARIO[s].name}<span>{SCENARIO[s].promise}</span></legend>
          {items.map((t) => <label key={t.task_id} className={picked === t.task_id ? 'task on' : 'task'}>
            <input type="radio" name="task" value={t.task_id} checked={picked === t.task_id} onChange={() => setPicked(t.task_id)} />
            <span><strong>{t.issue_title}</strong><small>{t.repo}，commit <code>{t.commit}</code></small></span>
          </label>)}
        </fieldset>)}
      </div>
      {error && <p className="dialog-error" role="alert">运行失败：{error}</p>}
    </>}

    <footer className="dialog-foot">
      <button className="btn" onClick={onClose} disabled={running}>取消</button>
      <button className="btn btn-primary" onClick={start} disabled={!picked || running || !!tasks.error}>{running ? '执行中' : '开始运行'}</button>
    </footer>
  </dialog>;
}
