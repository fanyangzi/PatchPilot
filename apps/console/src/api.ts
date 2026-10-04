/** Typed client for the PatchPilot FastAPI service (src/patchpilot/api.py). */

export type Conclusion = 'TRUSTED_DELIVERY' | 'NEEDS_REVIEW' | 'FAILED' | 'UNSAFE_DELIVERY' | null;
export type Scenario = 'normal' | 'failure' | 'risk';

export type TaskInfo = {
  task_id: string; repo: string; issue_title: string; issue_body: string;
  commit: string; scenario: Scenario; test_command?: string;
  risk_policy?: { network?: boolean; max_runtime_sec?: number; allowed_paths?: string[] };
};

export type RunRecord = {
  id: string; run_id: string; task_id: string; title: string; repo: string; commit: string | null;
  status: string; conclusion: Conclusion; attempt: number; runtime_sec: number; updated_at: string;
  started_at?: number; event_count: number;
  metrics?: { attempts?: number; reproduction_rate?: number; final_repair_rate?: number; recovery_success_rate?: number; evidence_completeness?: number };
  task: TaskInfo | null;
};

export type RunEvent = {
  event_id: string; kind: string; status: string; title: string; ts: number;
  data: Record<string, any>;
};

export type ArtifactRecord = { id: string; kind: string; name: string; sha256: string; size: number };
export type ArtifactContent = { artifact_id: string; name: string; kind: string; truncated: boolean; content: string };

export type EvalRow = {
  task_id: string; scenario: Scenario; method: string; status: string;
  first_pass: number; functional_repair: number; trusted_delivery: number; recovery_success: number;
  policy_gate_pass: number; evidence_completeness: number; attempts: number;
};
export type EvalMethodSummary = {
  n: number; trusted_delivery_rate: number; functional_repair_rate: number; first_pass_rate: number;
  recovery_success_rate: number; policy_gate_pass_rate: number; mean_evidence_completeness: number;
  mean_attempts: number; mean_runtime_sec: number; mean_tool_calls: number;
};
export type EvalSummary = {
  task_count: number; method_count: number; row_count: number;
  methods: Record<string, EvalMethodSummary>; task_ids: string[]; limitations: string[]; rows: EvalRow[];
};

const API_BASE = (import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '');

// Reads fail fast so an unreachable backend shows a clear offline state instead of
// hanging. Fixture runs execute a real harness and get a longer budget.
const READ_TIMEOUT_MS = 5000;
const RUN_TIMEOUT_MS = 180000;

export class ApiError extends Error {
  constructor(message: string, public status?: number) { super(message); }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const timeout = init?.method === 'POST' ? RUN_TIMEOUT_MS : READ_TIMEOUT_MS;
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: AbortSignal.timeout(timeout),
      headers: { Accept: 'application/json', ...(init?.headers || {}) },
    });
  } catch {
    throw new ApiError('无法连接到 PatchPilot 服务');
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(body?.detail || `请求失败（${response.status}）`, response.status);
  }
  return response.json() as Promise<T>;
}

const enc = encodeURIComponent;

export const api = {
  health: () => request<{ status: string; skills: number; version: string }>('/health'),
  runs: () => request<RunRecord[]>('/runs'),
  run: (id: string) => request<RunRecord>(`/runs/${enc(id)}`),
  events: (id: string) => request<RunEvent[]>(`/runs/${enc(id)}/events`),
  artifacts: (id: string) => request<ArtifactRecord[]>(`/runs/${enc(id)}/artifacts`),
  artifactContent: (id: string, artifactId: string) =>
    request<ArtifactContent>(`/runs/${enc(id)}/artifacts/${enc(artifactId)}/content`),
  tasks: () => request<TaskInfo[]>('/tasks'),
  evalSummary: () => request<EvalSummary>('/eval/summary'),
  startRun: (taskId: string) => request<RunRecord>('/demo/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task_id: taskId }),
  }),
};
