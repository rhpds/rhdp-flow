export interface HealthResponse {
  status: string;
  oc_installed: boolean;
  oc_connected: boolean;
  cluster_url: string;
  user: string;
  message: string;
  base_domain: string;
  rhdp_api_reachable: boolean;
}

export interface WorkshopSchedule {
  ci_name: string;
  ci: string;
  namespace: string;
  users: number | null;
  enable_workshop_interface: boolean;
  password: string;
  activity: string;
  purpose: string;
  workshop_name: string;
  provisioning_date: string;
  auto_stop: string;
  auto_destroy: string;
  is_multi_asset: boolean;
  asset_cis: string;
  multi_workshop_name: string;
  concurrency: number | null;
  instances: number | null;
  salesforce_ids: string;
  salesforce_type: string;
  aws_regions: string;
  count: number | null;
  white_glove: boolean;
  redirect: boolean;
  catalog_namespace: string;
  showroom_repo: string;
  showroom_ref: string;
  showroom_novnc: boolean;
  showroom_zerotouch: boolean;
  item_type?: 'Workshop' | 'Cluster' | 'Tenant';
  cluster_link?: string;
  cluster_ci_override?: string | null;
  is_cluster?: boolean;
  is_tenant?: boolean;
  detected_cluster_ci?: string | null;
  detection_method?: 'csv_label' | 'naming' | 'none';
  cluster_ci_source?: 'override' | 'agnosticv' | 'naming' | null;
  auto_added?: boolean;
}

export interface UploadResponse {
  count: number;
  total_rows: number;
  skipped_rows: number;
  schedules: WorkshopSchedule[];
}

export interface LabagatorEventSummary {
  id: number;
  name: string;
  start_date: string;
  end_date: string;
  location: string;
}

export interface LabagatorEventsResponse {
  events: LabagatorEventSummary[];
  error: string | null;
}

export interface LabagatorPreviewResponse {
  event_name: string;
  session_count: number;
  csv_text: string;
}

export interface LabagatorSessionSummary {
  room_session_id: number;
  date: string;
  title: string;
  ci_name: string;
  deploy_on: string;
  users: string;
  item_type: string;
}

export interface LabagatorSessionsResponse {
  event_id: number;
  event_name: string;
  sessions: LabagatorSessionSummary[];
  error: string | null;
}

/** Built-in example schedule (GET /api/schedules/examples). */
export interface ScheduleExampleMeta {
  slug: string;
  label: string;
}

/** Parameter summary from a CatalogItem spec (openAPIV3Schema). */
export interface CatalogItemParameter {
  name: string;
  type?: string;
  default?: unknown;
  minimum?: unknown;
  maximum?: unknown;
  enum?: unknown[];
  description?: string;
}

/** Cluster CatalogItem row (GET /api/catalog/items). */
export interface CatalogItemEntry {
  id: string;
  display_name: string;
  catalog_namespace: string;
  description: string;
  category: string;
  parameters: CatalogItemParameter[];
}

export interface DeploymentResult {
  ci_name: string;
  ci: string;
  namespace: string;
  guid: string;
  url: string;
  status: string;
  provisioning_date: string;
  auto_stop: string;
  auto_destroy: string;
  timestamp: string;
  error_message: string;
  showroom_url: string;
  showroom_status: string;
  password: string;
  users: number | null;
  instances: number | null;
}

export interface JobResponse {
  job_id: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'paused';
  progress: number;
  message: string;
  results: DeploymentResult[] | null;
  error: string | null;
  log_file: string | null;
}

export interface DeployRequest {
  ci_filter?: string | null;
  dry_run?: boolean;
  resource_lock?: boolean;
  enable_resource_pools?: boolean;
  white_glove?: boolean;
  redirect?: boolean;
  showroom_novnc?: boolean;
  showroom_zerotouch?: boolean;
  deploy_delay_seconds?: number | null;
  target_cluster?: string | null;
}

export interface ClusterTarget {
  key: string;
  display_name: string;
}

export interface ClusterListResponse {
  allowed: boolean;
  user: string | null;
  clusters: ClusterTarget[];
  default?: string;
}

export interface OperationResponse {
  success: boolean;
  message: string;
  details: string[];
}

export interface ExtendRequest {
  days: number;
  hours: number;
  ci_filter?: string | null;
}

export interface ScaleRequest {
  target_count: number;
  ci_filter?: string | null;
}

export interface LockRequest {
  ci_filter?: string | null;
}

export interface DisableAutostopRequest {
  ci_filter?: string | null;
}

export interface ShowroomCleanupRequest {
  ci_filter?: string | null;
}

export interface ShowroomHealthRequest {
  ci_filter?: string | null;
}

export interface QARequest {
  type: '1' | '2' | '3' | 'both' | 'all';
  namespace?: string | null;
  namespaces?: string[];
}

export interface RetryRequest {
  ci_names: string[];
  dry_run?: boolean;
  resource_lock?: boolean;
  enable_resource_pools?: boolean;
  white_glove?: boolean;
  redirect?: boolean;
}

export interface QAResponse {
  count: number;
  results: QAResult[];
}

export interface QAResult {
  ci_name: string;
  ci: string;
  namespace?: string;
  status: string;
  deployed: string;
  healthy?: boolean | string | null;
  expected_users?: number | string | null;
  actual_count?: number | string | null;
  lock_status?: boolean | null;
  actual_start?: string;
  actual_stop?: string;
  actual_destroy?: string;
  landing_page_url?: string;
  showroom_status?: string;
  showroom_url?: string;
  issues?: string;
  [key: string]: unknown;
}

export interface DiffEntry {
  ci_name: string;
  ci: string;
  namespace: string;
  change: 'added' | 'removed' | 'changed';
  details: string;
}

export interface DiffResponse {
  added: DiffEntry[];
  removed: DiffEntry[];
  changed: DiffEntry[];
  unchanged: number;
}

export interface NumUsersViolation {
  ci_name: string;
  ci: string;
  namespace: string;
  requested_users: number;
  maximum: number;
  minimum: number | null;
  default_value: number | null;
}

export interface UsersNotInCatalogAdvisory {
  ci_name: string;
  ci: string;
  namespace: string;
  users: number;
  enable_workshop_interface: boolean;
  instances: number | null;
  severity: 'high' | 'medium';
  message: string;
}

export interface NumUsersValidationResponse {
  violations: NumUsersViolation[];
  users_not_in_catalog: UsersNotInCatalogAdvisory[];
  checked: number;
  skipped: number;
  limits: Record<string, number>;
}

export interface PoolCapacityWarning {
  ci_name: string;
  ci: string;
  namespace: string;
  pool_name: string;
  pool_saturation_percent: number;
  placement_capacity_percent: number;
  message: string;
  severity: 'warning' | 'critical';
}

export interface PoolNotFoundWarning {
  ci_name: string;
  ci: string;
  namespace: string;
  base_ci: string;
  message: string;
}

export interface PoolCapacityValidationResponse {
  warnings: PoolCapacityWarning[];
  not_found: PoolNotFoundWarning[];
  tenant_items_checked: number;
  pools_queried: number;
}

export interface CatalogNamespaceMismatch {
  ci_name: string;
  ci: string;
  namespace: string;
  expected_catalog_namespace: string;
  found_catalog_namespace: string;
  suggestion: string;
}

export interface CatalogNotFoundItem {
  ci_name: string;
  ci: string;
  namespace: string;
  expected_catalog_namespace: string;
  message: string;
  /** Exactly one env-suffix alternate (.prod OR .event OR .dev). Null if ambiguous. */
  suggested_ci?: string | null;
  /** All published ci.{event,prod,dev} names found on the cluster. */
  suffix_options?: string[];
}

/** Non-blocking: schedule uses .prod instead of .event (info only). */
export interface ProdNotEventAdvisory {
  ci_name: string;
  ci: string;
  namespace: string;
  event_published: boolean;
  message: string;
}

export interface CatalogNamespaceValidationResponse {
  mismatches: CatalogNamespaceMismatch[];
  not_found: CatalogNotFoundItem[];
  /** Using .prod while .event is preferred for big events — never blocks deploy. */
  prod_not_event?: ProdNotEventAdvisory[];
  checked: number;
  skipped: number;
}

export interface ResourceStatus {
  exists: boolean;
  status: string;
  lifespan_end: string | null;
  count?: number | null;
  healthy?: boolean | null;
}

export interface DestroyCheckResult {
  ci_name: string;
  ci: string;
  namespace: string;
  scheduled_destroy: string;
  scheduled_stop: string;
  workshop: ResourceStatus;
  workshop_provision: ResourceStatus;
  resource_claim: ResourceStatus;
  overall_status: string;
  stop_status: string;
}

export interface DestroyCheckResponse {
  count: number;
  results: DestroyCheckResult[];
}

export interface SessionSummary {
  session_id: string;
  filename: string;
  schedule_count: number;
  result_count: number;
  timestamp: string;
  has_results: boolean;
  deploy_log_file: string | null;
  qa_log_file: string | null;
  override_count?: number;
}

export interface OperatorOverride {
  action: string;
  summary: string;
  detail: string;
  affected_count: number;
  timestamp: string;
  source: string;
}

export interface SessionDetail {
  session_id: string;
  filename: string;
  timestamp: string;
  schedules: WorkshopSchedule[];
  results: DeploymentResult[];
  qa_results: QAResult[];
  deploy_log_file: string | null;
  qa_log_file: string | null;
  operator_overrides?: OperatorOverride[];
}

export interface RegionPlan {
  region: string;
  users: number;
}

export interface DeployPreviewItem {
  ci_name: string;
  ci: string;
  namespace: string;
  users: number | null;
  instances: number | null;
  count: number | null;
  is_multi_asset: boolean;
  multi_region: boolean;
  regions: RegionPlan[];
}

export interface DeployPreviewResponse {
  schedules: DeployPreviewItem[];
}

export interface PoolInfo {
  pool_name: string;
  min_available: number;
  max_available: number | null;
  ready: number;
  available: number;
  claimed: number;
  provisioning: number;
  lifespan_default: string;
  lifespan_unclaimed: string;
  lifespan_maximum: string;
  provider_name: string;
  exists: boolean;
}

export interface PoolLookupResponse {
  catalog_item: string;
  pool: PoolInfo | null;
  has_pool: boolean;
}
