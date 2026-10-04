/** Thin adapter for the PatchPilot FastAPI contract.
 *
 * The console intentionally keeps a deterministic fixture fallback. A run is
 * rendered from the API whenever the backend is reachable; otherwise the UI
 * remains useful in Demo Playback and clearly exposes that mode.
 */
export type ApiRun = {
  id?: string; run_id?: string; title?: string; issue_title?: string;
  repo?: string; repository?: string; sha?: string; commit?: string;
  status?: string; conclusion?: string; duration?: string; runtime_sec?: number;
  retries?: number; retry_count?: number; updated_at?: string | number; updated?: string | number;
  started_at?: string | number; ended_at?: string | number;
  confidence?: number; patch_confidence?: number; fixture?: string;
};
export type ApiEvent = {
  event_id?: string; kind?: string; stage?: string; title?: string;
  summary?: string; detail?: string; command?: string; duration?: string;
  runtime_sec?: number; status?: string; artifact?: string; artifact_ids?: string[];
};
export type ApiGraphNode = {
  id?: string; label?: string; name?: string; sub?: string; subtitle?: string;
  kind?: string; status?: string; x?: number; y?: number;
};
export type ApiGraph = { nodes?: ApiGraphNode[]; skills?: ApiGraphNode[] };
export type ApiArtifact = {
  id?: string; name?: string; filename?: string; title?: string; type?: string;
  kind?: string; size?: string | number; detail?: string; status?: string;
};

const API_BASE = (import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '');

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { Accept: 'application/json', ...(init?.headers || {}) },
  });
  if (!response.ok) throw new Error(`API ${response.status}: ${path}`);
  return response.json() as Promise<T>;
}

function unwrap<T>(payload: T | {data?: T; items?: T; runs?: T; events?: T; graph?: T; artifacts?: T}): T {
  if (payload && typeof payload === 'object') {
    const p = payload as Record<string, unknown>;
    return (p.data ?? p.items ?? p.runs ?? p.events ?? p.graph ?? p.artifacts ?? payload) as T;
  }
  return payload as T;
}

export const patchpilotApi = {
  baseUrl: API_BASE,
  listRuns: async (): Promise<ApiRun[]> => {
    const payload = await request<unknown>('/runs');
    const value = unwrap<unknown>(payload);
    return Array.isArray(value) ? value as ApiRun[] : [];
  },
  getRun: async (runId: string): Promise<ApiRun> => unwrap(await request<ApiRun>(`/runs/${encodeURIComponent(runId)}`)),
  getEvents: async (runId: string): Promise<ApiEvent[]> => {
    const payload = await request<unknown>(`/runs/${encodeURIComponent(runId)}/events`);
    const value = unwrap<unknown>(payload);
    return Array.isArray(value) ? value as ApiEvent[] : [];
  },
  getGraph: async (runId: string): Promise<ApiGraph> => unwrap(await request<ApiGraph>(`/runs/${encodeURIComponent(runId)}/graph`)),
  getArtifacts: async (runId: string): Promise<ApiArtifact[]> => {
    const payload = await request<unknown>(`/runs/${encodeURIComponent(runId)}/artifacts`);
    const value = unwrap<unknown>(payload);
    return Array.isArray(value) ? value as ApiArtifact[] : [];
  },
  startDemo: async (taskId = 'issue-001-normal'): Promise<ApiRun> => {
    return unwrap(await request<ApiRun>('/demo/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId }),
    }));
  },
};
