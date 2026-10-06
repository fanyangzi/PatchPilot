import { useState } from 'react';
import { FileDiff, FileText, Hash, PackageCheck, ScrollText } from 'lucide-react';
import { api, type ArtifactRecord, type RunRecord } from '../api';
import { bytes } from '../model';
import { DiffView, Empty, Loading, Offline, Section, useAsync } from '../ui';

const KIND: Record<string, { name: string; icon: typeof PackageCheck; note: string }> = {
  patch_diff: { name: '补丁', icon: FileDiff, note: '可应用到基线的 unified diff' },
  pr_draft: { name: 'PR 草稿', icon: FileText, note: '交给维护者的说明，不代为合并' },
  evidence_bundle: { name: '证据包', icon: PackageCheck, note: '每一步的原始记录与哈希' },
};
const ORDER = ['patch_diff', 'pr_draft', 'evidence_bundle'];

export function Deliverables({ run }: { run: RunRecord }) {
  const list = useAsync(() => api.artifacts(run.id), [run.id]);
  const items = (list.data || []).slice().sort((a, b) => ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind));
  const [active, setActive] = useState<string | null>(null);
  const current = items.find((a) => a.id === active) ?? items[0];

  return <Section icon={ScrollText} title="交付物" note={items.length ? `${items.length} 个文件` : undefined}
    sub="交给维护者的全部内容。每个文件都记录了 SHA-256，可与证据包逐一核对。">
    {list.loading ? <Loading label="正在读取交付物" /> : list.error ? <Offline message={list.error} onRetry={list.reload} /> : !items.length ? <Empty>这次运行没有产出交付物。</Empty> : <div className="deliver">
      <div className="deliver-tabs" role="tablist" aria-label="交付物">
        {items.map((a) => {
          const meta = KIND[a.kind];
          const Icon = meta?.icon ?? FileText;
          const on = current?.id === a.id;
          return <button key={a.id} role="tab" aria-selected={on} className={on ? 'dtab on' : 'dtab'} onClick={() => setActive(a.id)}>
            <Icon size={16} />
            <span><strong>{meta?.name ?? a.kind}</strong><span>{a.name}，{bytes(a.size)}</span></span>
          </button>;
        })}
      </div>
      {current && <ArtifactBody key={current.id} runId={run.id} artifact={current} />}
    </div>}
  </Section>;
}

function ArtifactBody({ runId, artifact }: { runId: string; artifact: ArtifactRecord }) {
  const body = useAsync(() => api.artifactContent(runId, artifact.id), [runId, artifact.id]);
  return <div className="deliver-body" role="tabpanel">
    <div className="hash"><Hash size={13} />SHA-256 <code>{artifact.sha256}</code></div>
    {body.loading ? <Loading label="正在读取文件内容" /> : body.error ? <Offline message={body.error} onRetry={body.reload} />
      : artifact.kind === 'patch_diff' ? <DiffView text={body.data!.content} />
      : <pre className="code prose" tabIndex={0}>{artifact.kind === 'evidence_bundle' ? pretty(body.data!.content) : body.data!.content}</pre>}
    {body.data?.truncated && <p className="artifact-meta">内容较长，只显示前 200 KB。</p>}
  </div>;
}

function pretty(text: string) {
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; }
}
