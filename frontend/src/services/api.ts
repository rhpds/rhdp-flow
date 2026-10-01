import type {
  HealthResponse,
  UploadResponse,
  WorkshopSchedule,
  DeploymentResult,
  JobResponse,
  DeployRequest,
  OperationResponse,
  ExtendRequest,
  ScaleRequest,
  LockRequest,
  DisableAutostopRequest,
  ShowroomCleanupRequest,
  ShowroomHealthRequest,

  QARequest,
  QAResponse,
  QAScopesResponse,
  RetryRequest,
  NumUsersValidationResponse,
  PoolCapacityValidationResponse,
  SessionSummary,
  SessionDetail,
  ScheduleExampleMeta,
  CatalogItemEntry,
  DeployPreviewResponse,
  PoolInfo,
  PoolLookupResponse,
  LabagatorEventsResponse,
  LabagatorPreviewResponse,
  LabagatorSessionsResponse,
  ClusterListResponse,
} from '../types';

export interface TenantClusterRef {
  ci: string;
  namespace: string;
  cluster_ref: string;
  cluster_ci_from_csv: string;
  workshop_name: string;
  pool_exists: boolean;
  has_cluster_row: boolean;
}

const API = '/api';
let selectedTarget = '';
export const getSelectedTarget = () => selectedTarget;
export function selectTargetCluster(target: string) {
  selectedTarget = target;
  clearApiCache();
  window.dispatchEvent(new Event('rhdp-target-change'));
}


export function getApiKey(): string | null {
  // Use sessionStorage — cleared on tab close, not vulnerable to persistent XSS
  return sessionStorage.getItem('rhdp-api-key');
}

function getApiKeyHeader(): Record<string, string> {
  const apiKey = getApiKey();
  return { ...(apiKey ? { 'X-API-Key': apiKey } : {}), ...(selectedTarget ? { 'X-RHDP-Target-Cluster': selectedTarget } : {}) };
}

export function setApiKey(key: string): void {
  sessionStorage.setItem('rhdp-api-key', key);
}

export function clearApiKey(): void {
  sessionStorage.removeItem('rhdp-api-key');
}

async function responseError(res: Response): Promise<Error> {
  const body = await res.text();
  const status = `HTTP ${res.status}`;
  if (res.headers?.get('content-type')?.includes('text/html') || /^\s*</.test(body)) {
    return new Error(
      [502, 503, 504].includes(res.status)
        ? `RHDP-Flow backend is unavailable (${status}). Validation and dry-run both require the backend. Wait for service recovery, then retry.`
        : `Expected an API response but received a web page (${status}). Reload to check your sign-in session, then retry.`,
    );
  }
  try {
    const data = JSON.parse(body);
    if (typeof data.detail === 'string') return new Error(data.detail);
  } catch {
    // Plain-text errors are also supported.
  }
  return new Error(body || res.statusText || status);
}

async function request<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...opts.headers as Record<string, string>,
  };
  Object.assign(headers, getApiKeyHeader());
  const target = selectedTarget;
  const res = await fetch(`${API}${path}`, { ...opts, headers });
  if (selectedTarget !== target) throw new Error('Target changed. Re-run this check for the selected cluster.');
  if (!res.ok || res.headers?.get('content-type')?.includes('text/html')) {
    throw await responseError(res);
  }
  return res.json();
}

// ── Response cache (5s TTL) for GET endpoints ──
const _cache = new Map<string, { data: unknown; expiry: number }>();

async function cachedRequest<T>(path: string, ttlMs = 5000): Promise<T> {
  const key = `${selectedTarget}:${path}`;
  const entry = _cache.get(key);
  if (entry && Date.now() < entry.expiry) return entry.data as T;
  const data = await request<T>(path);
  _cache.set(key, { data, expiry: Date.now() + ttlMs });
  return data;
}

export function clearApiCache() {
  _cache.clear();
}

export const api = {
  health: () => cachedRequest<HealthResponse>('/health'),

  /** Cluster catalog items (prod + event + dev). Cached 60s — dropdowns share one fetch. */
  listCatalogItems: () => cachedRequest<CatalogItemEntry[]>('/catalog/items', 60_000),

  /** Lookup ResourcePool for a catalog item. */
  lookupPool: (catalogItem: string) =>
    request<PoolLookupResponse>(`/pools/lookup?catalog_item=${encodeURIComponent(catalogItem)}`),

  /** List all ResourcePools in the cluster. */
  listAllPools: () => request<{ pools: PoolInfo[] }>('/pools/all'),

  // Schedules
  uploadCSV: async (file: File): Promise<UploadResponse> => {
    const form = new FormData();
    form.append('file', file);
    const res = await fetch(`${API}/schedules/upload`, {
      method: 'POST',
      body: form,
      headers: getApiKeyHeader(),
    });
    if (!res.ok || res.headers?.get('content-type')?.includes('text/html')) throw await responseError(res);
    return res.json();
  },
  /** List upcoming Labagator events. `days` bounds the look-ahead window (0 = all upcoming). */
  listLabagatorEvents: (days = 7) =>
    request<LabagatorEventsResponse>(`/labagator/events?days=${days}`),

  /** List a Labagator event's Flow-eligible room sessions for the import picker. */
  listLabagatorSessions: (params: {
    event_id: number;
    event_name?: string;
    filter_date?: string;
  }): Promise<LabagatorSessionsResponse> => {
    const qs = new URLSearchParams({ event_id: String(params.event_id) });
    if (params.event_name) qs.set('event_name', params.event_name);
    if (params.filter_date) qs.set('filter_date', params.filter_date);
    return request<LabagatorSessionsResponse>(`/schedules/labagator-sessions?${qs.toString()}`);
  },

  previewLabagatorImport: (params: {
    event_id: number;
    namespace: string;
    event_name: string;
    enable_workshop_interface: boolean;
    concurrency: number;
    white_glove: boolean;
    auto_stop_days: number;
    auto_destroy_days: number;
    room_session_ids?: number[];
  }): Promise<LabagatorPreviewResponse> => {
    const qs = new URLSearchParams({
      event_id: String(params.event_id),
      namespace: params.namespace,
      event_name: params.event_name,
      enable_workshop_interface: String(params.enable_workshop_interface),
      concurrency: String(params.concurrency),
      white_glove: String(params.white_glove),
      auto_stop_days: String(params.auto_stop_days),
      auto_destroy_days: String(params.auto_destroy_days),
    });
    if (params.room_session_ids && params.room_session_ids.length > 0) {
      qs.set('room_session_ids', params.room_session_ids.join(','));
    }
    return request<LabagatorPreviewResponse>(`/schedules/labagator-preview?${qs.toString()}`);
  },

  /** Legacy manual-upload path: POST a Labagator session-export CSV, transformed server-side. */
  importLabagatorCSV: async (
    file: File,
    options?: {
      default_ci?: string;
      default_users?: number;
      default_redirect?: boolean;
      default_white_glove?: boolean;
      buffer_hours?: number;
    }
  ): Promise<UploadResponse> => {
    const form = new FormData();
    form.append('file', file);

    // Build query params from options
    const params = new URLSearchParams();
    if (options?.default_ci) params.append('default_ci', options.default_ci);
    if (options?.default_users !== undefined) params.append('default_users', options.default_users.toString());
    if (options?.default_redirect !== undefined) params.append('default_redirect', options.default_redirect.toString());
    if (options?.default_white_glove !== undefined) params.append('default_white_glove', options.default_white_glove.toString());
    if (options?.buffer_hours !== undefined) params.append('buffer_hours', options.buffer_hours.toString());

    const url = params.toString() ? `${API}/schedules/import-labagator?${params}` : `${API}/schedules/import-labagator`;
    const res = await fetch(url, {
      method: 'POST',
      body: form,
      headers: getApiKeyHeader(),
    });
    if (!res.ok || res.headers?.get('content-type')?.includes('text/html')) throw await responseError(res);
    return res.json();
  },

  importFromLabagator: (csvText: string, filename: string): Promise<UploadResponse> =>
    request<UploadResponse>('/schedules/import-from-labagator', {
      method: 'POST',
      body: JSON.stringify({ csv_text: csvText, filename }),
    }),
  uploadPasswordsCSV: async (file: File): Promise<{count: number; message: string}> => {
    const form = new FormData();
    form.append('file', file);
    const res = await fetch(`${API}/schedules/upload-passwords`, {
      method: 'POST',
      body: form,
      headers: getApiKeyHeader(),
    });
    if (!res.ok || res.headers?.get('content-type')?.includes('text/html')) throw await responseError(res);
    return res.json();
  },
  listScheduleExamples: () => cachedRequest<ScheduleExampleMeta[]>('/schedules/examples'),
  loadScheduleExample: (slug: string) =>
    request<UploadResponse>(
      `/schedules/load-example/${encodeURIComponent(slug)}`,
      { method: 'POST', body: '{}' },
    ),
  getSchedules: () => cachedRequest<WorkshopSchedule[]>('/schedules'),
  validateNamespaces: () =>
    request<{ namespaces: Record<string, boolean>; missing: string[] }>('/schedules/validate-namespaces', { method: 'POST', body: '{}' }),
  validateNumUsers: () =>
    request<NumUsersValidationResponse>('/schedules/validate-num-users', { method: 'POST', body: '{}' }),
  validateParameters: () =>
    request<import('../types').ParameterValidationResponse>('/schedules/validate-parameters', { method: 'POST', body: '{}' }),
  validateCatalogNamespaces: () =>
    request<import('../types').CatalogNamespaceValidationResponse>('/schedules/validate-catalog-namespaces', { method: 'POST', body: '{}' }),
  validateClusterTenant: () =>
    request<any>('/schedules/validate-cluster-tenant', { method: 'POST', body: '{}' }),

  getClusterNeeds: (targetCluster?: string | null) =>
    request<{
      needs: Array<{
        cluster_ci: string;
        tenant_ci_example: string;
        tenant_count: number;
        capacity_per_cluster: number;
        clusters_needed: number;
        clusters_in_csv: number;
        deficit: number;
        pool_available: number | null;
      }>;
      total_tenant_count: number;
      total_deficit: number;
    }>(targetCluster ? `/schedules/cluster-needs?target_cluster=${encodeURIComponent(targetCluster)}` : '/schedules/cluster-needs'),
  checkTenantClusterRefs: (targetCluster?: string | null) =>
    request<{
      missing_refs: TenantClusterRef[];
      ref_no_pool: TenantClusterRef[];
      ready: TenantClusterRef[];
      total_tenant_count: number;
      checked: boolean;
    }>(targetCluster ? `/schedules/tenant-cluster-refs?target_cluster=${encodeURIComponent(targetCluster)}` : '/schedules/tenant-cluster-refs'),
  checkPoolStatus: (cluster_cis: string[]) =>
    request<{
      results: Array<{
        name: string;
        exists: boolean;
        enabled: boolean;
        available_clusters: number;
        action_preview: string;
      }>;
    }>('/schedules/check-pool-status', {
      method: 'POST',
      body: JSON.stringify({ cluster_cis }),
    }),
  createTenantClusterPools: (body: {
    cluster_cis: string[];
    enabled?: boolean;
    min_clusters?: number;
    max_clusters?: number;
    min_available_sandbox_placements?: number;
    max_placements?: number;
    environment_level?: string;
    cloud?: string;
    apply_to_cluster?: boolean;
  }) =>
    request<{
      yaml: string;
      applied: boolean;
      results: Array<{ name: string; success: boolean; action: string; output: string; error: string }>;
      count: number;
    }>('/schedules/create-tenant-cluster-pools', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  autoFixClusterTenantTiming: (bufferHours = 4.0) =>
    request<{
      fixed_count: number;
      skipped_count: number;
      fixed_items: any[];
      skipped_items: any[];
      warnings: string[];
      message: string
    }>(`/schedules/auto-fix-cluster-tenant?buffer_hours=${bufferHours}`, { method: 'POST' }),
  validatePoolCapacity: () =>
    request<PoolCapacityValidationResponse>('/schedules/validate-pool-capacity', { method: 'POST', body: '{}' }),
  diffSchedules: async (file: File): Promise<import('../types').DiffResponse> => {
    const form = new FormData();
    form.append('file', file);
    const res = await fetch(`${API}/schedules/diff`, {
      method: 'POST',
      body: form,
      headers: getApiKeyHeader(),
    });
    if (!res.ok || res.headers?.get('content-type')?.includes('text/html')) throw await responseError(res);
    return res.json();
  },

  // Multi-cluster deploy targets (Feature 2 — identity-gated picker)
  getClusters: () => request<ClusterListResponse>('/clusters'),

  // Deploy
  deploy: (body: DeployRequest) =>
    request<JobResponse>('/deploy', { method: 'POST', body: JSON.stringify(body) }),
  dryRun: (body: DeployRequest) =>
    request<DeploymentResult[]>('/deploy/dry-run', { method: 'POST', body: JSON.stringify(body) }),

  /** POST dry-run with YAML export; triggers browser download of combined manifests. */
  downloadDryRunYaml: async (body: DeployRequest): Promise<void> => {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...getApiKeyHeader(),
    };
    const res = await fetch(`${API}/deploy/dry-run-yaml`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    });
    if (!res.ok || res.headers?.get('content-type')?.includes('text/html')) throw await responseError(res);
    const blob = await res.blob();
    const cd = res.headers.get('Content-Disposition');
    const m = cd?.match(/filename="([^"]+)"/);
    const filename = m?.[1] ?? 'rhdp-dry-run-manifests.yaml';
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  },
  deployStatus: (jobId: string) => request<JobResponse>(`/deploy/status/${jobId}`),
  deployResults: () => cachedRequest<DeploymentResult[]>('/deploy/results'),
  deployStream: (jobId: string) => new EventSource(`${API}/deploy/stream/${jobId}`),
  deployWebSocket: (jobId: string) => {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const apiKey = getApiKey();
    const qs = apiKey ? `?api_key=${encodeURIComponent(apiKey)}` : '';
    return new WebSocket(`${proto}//${location.host}${API}/deploy/ws/${jobId}${qs}`);
  },
  deployCancel: (jobId: string) =>
    request<{ message: string }>(`/deploy/cancel/${jobId}`, { method: 'POST', body: '{}' }),
  deployPause: (jobId: string) =>
    request<{ message: string }>(`/deploy/pause/${jobId}`, { method: 'POST', body: '{}' }),
  deployResume: (jobId: string) =>
    request<{ message: string }>(`/deploy/resume/${jobId}`, { method: 'POST', body: '{}' }),
  deployPreview: (body: DeployRequest) =>
    request<DeployPreviewResponse>('/deploy/preview', { method: 'POST', body: JSON.stringify(body) }),
  retry: (body: RetryRequest) =>
    request<JobResponse>('/deploy/retry', { method: 'POST', body: JSON.stringify(body) }),
  deleteResults: (items: Array<{ ci: string; namespace: string }>) =>
    request<{ deleted: number; remaining: number }>('/deploy/results/delete', {
      method: 'POST',
      body: JSON.stringify({ items }),
    }),

  // Operations
  lock: (body: LockRequest) =>
    request<OperationResponse>('/operations/lock', { method: 'POST', body: JSON.stringify(body) }),
  unlock: (body: LockRequest) =>
    request<OperationResponse>('/operations/unlock', { method: 'POST', body: JSON.stringify(body) }),
  extendStop: (body: ExtendRequest) =>
    request<OperationResponse>('/operations/extend-stop', { method: 'POST', body: JSON.stringify(body) }),
  extendDestroy: (body: ExtendRequest) =>
    request<OperationResponse>('/operations/extend-destroy', { method: 'POST', body: JSON.stringify(body) }),
  disableAutostop: (body: DisableAutostopRequest) =>
    request<OperationResponse>('/operations/disable-autostop', { method: 'POST', body: JSON.stringify(body) }),
  scale: (body: ScaleRequest) =>
    request<OperationResponse>('/operations/scale', { method: 'POST', body: JSON.stringify(body) }),
  showroomCleanup: (body: ShowroomCleanupRequest) =>
    request<OperationResponse>('/operations/showroom-cleanup', { method: 'POST', body: JSON.stringify(body) }),
  showroomHealth: (body: ShowroomHealthRequest) =>
    request<OperationResponse>('/operations/showroom-health', { method: 'POST', body: JSON.stringify(body) }),
  showroomPreflight: (body: { ci_filter?: string }) =>
    request<OperationResponse>('/operations/showroom-preflight', { method: 'POST', body: JSON.stringify(body) }),
  // QA
  qaNamespaces: () => cachedRequest<string[]>('/qa/namespaces'),
  qaScopes: (namespace?: string) => {
    const qs = namespace ? `?namespace=${encodeURIComponent(namespace)}` : '';
    return cachedRequest<QAScopesResponse>(`/qa/scopes${qs}`);
  },
  /** Start a QA run as a background job; returns a job id to stream/poll. */
  runQA: (body: QARequest) =>
    request<JobResponse>('/qa/run', { method: 'POST', body: JSON.stringify(body) }),
  qaStatus: (jobId: string) => request<JobResponse>(`/qa/status/${jobId}`),
  qaStream: (jobId: string) => new EventSource(`${API}/qa/stream/${jobId}`),
  qaCancel: (jobId: string) =>
    request<{ message: string }>(`/qa/cancel/${jobId}`, { method: 'POST', body: '{}' }),
  qaResults: () => cachedRequest<QAResponse>('/qa/results'),
  destroyCheck: (namespace?: string) =>
    request<import('../types').DestroyCheckResponse>('/qa/destroy-check', {
      method: 'POST',
      body: JSON.stringify({ namespace: namespace || undefined }),
    }),
  destroyCheckResults: () =>
    cachedRequest<{ count: number; results: import('../types').DestroyCheckResult[] }>('/qa/destroy-check/results'),

  // Templates
  templateURL: `${API}/templates/schedule`,

  // Export
  exportResultsURL: `${API}/export/results`,
  exportStudentsURL: `${API}/export/students`,

  // Logs
  logURL: (filename: string) => `${API}/logs/${filename}`,

  // Sessions
  getSessions: () => cachedRequest<SessionSummary[]>('/sessions'),
  getSession: (id: string) => request<SessionDetail>(`/sessions/${id}`),
  clearSession: () =>
    request<{ message: string; session_count: number }>('/sessions/clear', { method: 'POST', body: '{}' }),
  getOperatorOverrides: () =>
    request<import('../types').OperatorOverride[]>('/sessions/current/operator-overrides'),
  addOperatorOverride: (body: {
    action: string;
    summary: string;
    detail?: string;
    affected_count?: number;
    source?: string;
  }) =>
    request<import('../types').OperatorOverride>('/sessions/current/operator-overrides', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  clearOperatorOverrides: () =>
    request<{ message: string; cleared: number }>('/sessions/current/operator-overrides', {
      method: 'DELETE',
    }),

  // Schedule Management
  updateSchedules: (schedules: WorkshopSchedule[]) =>
    request<{ message: string }>('/schedules', { method: 'PUT', body: JSON.stringify(schedules) }),
  deleteSchedule: (index: number) =>
    request<{ message: string }>(`/schedules/${index}`, { method: 'DELETE' }),
  fillMissingDates: (dates: { provisioning_date: string; auto_stop: string; auto_destroy: string }) =>
    request<{ message: string; updated_count: number }>('/schedules/fill-missing-dates', { method: 'PATCH', body: JSON.stringify(dates) }),
};
