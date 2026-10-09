/** Typed access to the source-aware, versioned acceptance API. */
const BASE = `${(import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '')}/v1`;
const enc = encodeURIComponent;

export class V1Error extends Error {
  constructor(message: string, public code?: string, public status?: number, public requestId?: string) { super(message); }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      signal: init?.signal || AbortSignal.timeout(init?.method === 'POST' || init?.method === 'PUT' ? 30000 : 8000),
      headers: { Accept: 'application/json', ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...(init?.headers || {}) },
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new V1Error('无法连接到 PatchPilot v1 服务');
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new V1Error(body?.error?.message || `请求失败（${response.status}）`, body?.error?.code, response.status, body?.request_id);
  }
  return body as T;
}

export type Task = {
  task_id: string; issue_snapshot: { title?: string; body?: string; repo_id?: string; base_ref?: string; candidate_ref?: string; intake_id?: string; mode?: string; resolved_refs?: unknown };
  source_refs: string[]; mode: string; revision: number; created_at: string;
};
export type Candidate = {
  candidate_id: string; task_id: string; base_sha: string; head_sha?: string | null; tree_digest: string; patch_hash: string;
  author_type: string; parent_candidate_id?: string | null; created_at: string;
};
export type Condition = { condition_id: string; kind: 'change' | 'preserve' | 'constraint' | 'clarification'; statement: string; source_refs: string[]; required: boolean; confirmation: string; oracle: Record<string, unknown> };
export type Contract = { contract_id: string; task_id: string; revision: number; state: 'draft' | 'review' | 'frozen'; conditions: Condition[]; source_refs: string[]; contract_hash: string; created_at: string };
export type Verification = {
  verification_id: string; verification_key: string; candidate_id: string; contract_id: string; run_state: string; verdict: string;
  contract_revision: number; validity: string; review_decision: string; gaps: string[]; completed_checks: string[]; created_at: string;
};
export type InboxItem = {
  task?: Task; task_id: string; title: string; repo: string; mode: string; created_at: string; source_refs: string[];
  candidate?: Candidate | null; candidate_id?: string | null; candidate_count?: number; current_contract?: { contract_id: string; revision: number; state: string } | null; contract?: Contract | null;
  verification?: Verification | null; latest_verification?: Verification | null; status?: string; updated_at?: string;
  next_action?: string; next_action_reason?: string;
};
export type Check = {
  check_id: string; verification_id: string; suite_id: string; target: string; variant: 'base' | 'candidate';
  outcome: string; count: number; command_argv: string[]; return_code: number | null; duration_ms: number | null;
  stdout: string; stderr: string; artifact_refs: string[]; details: Record<string, unknown>; created_at: string;
};
export type Finding = { finding_id?: string; kind?: string; status?: string; title?: string; message?: string; evidence_refs?: string[]; created_at?: string; [key: string]: unknown };
export type ReportMeta = { report_id: string; task_id: string; verification_id: string; verification_key: string; format: 'markdown' | 'html'; content_type: string; content_sha: string; created_at: string };
export type Report = ReportMeta & { snapshot: { task: Task; candidate: Candidate; contract: Contract; verification: Verification; scope_statement?: string; generated_at?: string } };

const json = (value: unknown) => JSON.stringify(value);
export const v1 = {
  health: () => request<{ status: string; service: string; api_version: string; python?: string }>('/health'),
  inbox: () => request<{ items: InboxItem[]; total: number; request_id: string }>('/inbox'),
  tasks: () => request<{ items: Task[]; total: number; next_cursor?: string | null }>('/tasks?limit=500'),
  task: (id: string) => request<{ task: Task; candidates: Candidate[]; contracts: Contract[]; latest_verification: Verification | null }>(`/tasks/${enc(id)}`),
  candidates: (taskId: string) => request<{ items: Candidate[] }>(`/tasks/${enc(taskId)}/candidates`),
  candidateDiff: (id: string) => request<{ candidate_id: string; patch_hash: string; content?: string | null; content_ref?: string | null; available: boolean }>(`/candidates/${enc(id)}/diff`),
  contracts: (taskId: string) => request<{ items: Contract[] }>(`/tasks/${enc(taskId)}/contracts`),
  verifications: (id: string) => request<{ verification: Verification; verification_key: string }>(`/verifications/${enc(id)}`),
  checks: (id: string) => request<{ items: Check[]; complete: boolean; required_gaps: string[] }>(`/verifications/${enc(id)}/checks`),
  executeVerification: (id: string, body: { repo_id?: string; repo_path?: string; suite_id?: string; command_argv?: string[][] }) => request<{ execution_id: string; source_verification_id: string; verification: Verification; checks: Check[]; status: string }>(`/verifications/${enc(id)}/execute`, { method: 'POST', body: json(body) }),
  findings: (taskId: string, candidateId?: string) => request<{ items: Finding[]; available: boolean; complete: boolean; reason?: string }>(`/tasks/${enc(taskId)}/findings${candidateId ? `?candidate_id=${enc(candidateId)}` : ''}`),
  reports: (taskId: string) => request<{ items: ReportMeta[] }>(`/tasks/${enc(taskId)}/reports`),
  report: (id: string) => request<{ report: Report }>(`/reports/${enc(id)}`),
  reportContent: (id: string) => request<{ report_id: string; format: string; content: string; content_sha: string }>(`/reports/${enc(id)}/content`),
  createReport: (verificationId: string, format: 'markdown' | 'html') => request<{ report: ReportMeta }>(`/verifications/${enc(verificationId)}/reports`, { method: 'POST', body: json({ format }) }),
  createDraft: (taskId: string, body: { source_ids: string[]; base_snapshot_id: string; model_profile: string; conditions: Condition[] }) => request<{ contract: Contract; status: string }>(`/tasks/${enc(taskId)}/contracts/draft`, { method: 'POST', body: json(body) }),
  updateContract: (id: string, body: { expected_revision: number; conditions: Condition[] }) => request<{ contract: Contract; status: string }>(`/contracts/${enc(id)}`, { method: 'PUT', body: json(body) }),
  freezeContract: (id: string, body: { expected_revision: number; confirmed_condition_ids: string[]; scope_exclusions: string[]; actor: string }) => request<{ contract: Contract; scope_exclusions: string[]; status: string }>(`/contracts/${enc(id)}/freeze`, { method: 'POST', body: json(body) }),
  createVerification: (taskId: string, body: { candidate_id: string; contract_id: string; suite_id: string; environment_id: string; policy_id: string; baseline_tests_hash: string; command_argv: string[][]; verifier_revision: string; probe_plan_hash: string; seed: number }) => request<{ verification: Verification; verification_key: string; status: string; job_id: string }>(`/tasks/${enc(taskId)}/verifications`, { method: 'POST', body: json(body) }),
  decision: (id: string, body: { decision: 'accepted' | 'rejected' | 'exception_accepted'; reason: string; expected_key: string; actor: string }) => request<{ decision: Record<string, unknown> }>(`/verifications/${enc(id)}/decisions`, { method: 'POST', body: json(body) }),
  createIntake: (body: { mode: 'local_patch' | 'pr' | 'issue_candidate'; repo_id: string; base_ref: string; patch_text?: string; pr_url?: string; issue_url?: string; candidate_ref?: string; issue_title?: string; issue_body?: string; source_refs?: string[] }) => request<{ intake_id: string; status: string; job_id: string; intake: Record<string, unknown> }>('/intakes', { method: 'POST', body: json(body) }),
  createTask: (body: { intake_id: string; mode?: 'verify' | 'repair' | 'review'; environment_id?: string; policy_id?: string }) => request<{ task: Task; status: string }>('/tasks', { method: 'POST', body: json(body) }),
  createCandidate: (taskId: string, body: { source: 'upload' | 'pr' | 'model_edit' | 'human'; base_sha: string; patch_text?: string; content_ref?: string; head_sha?: string; author_type?: string }) => request<{ candidate: Candidate; content_status: string }>(`/tasks/${enc(taskId)}/candidates`, { method: 'POST', body: json(body) }),
};
