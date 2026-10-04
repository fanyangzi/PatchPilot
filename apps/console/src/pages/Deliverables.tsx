import { useState } from 'react';
import { api, type ArtifactRecord, type RunRecord } from '../api';
import { DiffView, Empty, Loading, useAsync } from '../ui';

const KIND_NAME: Record<string, string> = { patch_diff: '补丁', pr_draft: 'PR 草稿', evidence_bundle: '证据包' };
const ORDER = ['patch_diff', 'pr_draft', 'evidence_bundle'];

export function Deliverables({ run }: { run: RunRecord }) {
  const list = useAsync(() => api.artifacts(run.id), [run.id]);
  const items = (list.data || []).slice().sort((a, b) => ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind));
  const [active, setActive] = useState<string | null>(null);
  const current = items.find((a) => a.id === active) ?? items[0];

  return <section>
    <h2 className="block-title">交付物</h2>
    <p className="block-sub">交给维护者的全部内容。每个文件都记录了 SHA-256，可与证据包逐一核对。</p>
    {list.loading ? <Loading /> : !items.length ? <Empty>这次运行没有产出交付物。</Empty> : <div className="deliver">
      <div className="deliver-tabs" role="tablist" aria-label="交付物">
        {items.map((a) => <button key={a.id} role="tab" aria-selected={current?.id === a.id} className={current?.id === a.id ? 'dtab on' : 'dtab'} onClick={() => setActive(a.id)}>
          <strong>{KIND_NAME[a.kind] || a.kind}</strong>
          <span>{a.name}，{a.size < 1024 ? `${a.size} B` : `${(a.size / 1024).toFixed(1)} KB`}</span>
        </button>)}
      </div>
      {current && <ArtifactBody key={current.id} runId={run.id} artifact={current} />}
    </div>}
  </section>;
}

function ArtifactBody({ runId, artifact }: { runId: string; artifact: ArtifactRecord }) {
  const body = useAsync(() => api.artifactContent(runId, artifact.id), [runId, artifact.id]);
  return <div className="deliver-body" role="tabpanel">
    <div className="hash">SHA-256 <code>{artifact.sha256}</code></div>
    {body.loading ? <Loading /> : body.error ? <Empty>{body.error}</Empty>
      : artifact.kind === 'patch_diff' ? <DiffView text={body.data!.content} />
      : artifact.kind === 'evidence_bundle' ? <pre className="code raw tall" tabIndex={0}>{pretty(body.data!.content)}</pre>
      : <pre className="code prose" tabIndex={0}>{body.data!.content}</pre>}
    {body.data?.truncated && <p className="block-sub">内容较长，只显示前 200 KB。</p>}
  </div>;
}

function pretty(text: string) {
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; }
}
