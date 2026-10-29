/**
 * Hand-written TypeScript types mirroring the backend's Pydantic schemas
 * (backend/schemas/*.py), cross-checked against `GET /openapi.json` while
 * the backend was running locally. Kept in one file per plan §C.3 —
 * `frontend/src/api/` may import these but must contain no UI imports, and
 * these types themselves import nothing.
 */

// ── Integrations (backend/schemas/integration.py) ──────────────────────

export interface IntegrationInstance {
  id: string;
  entity_id: string;
  domain: string;
  display_name: string;
  config: Record<string, unknown>;
  enabled: boolean;
  tags: string[];
  status: 'CONNECTED' | 'DEGRADED' | 'DISCONNECTED' | string;
  created_at: string;
  updated_at: string;
}

export interface IntegrationInstanceCreate {
  entity_id: string;
  domain: string;
  display_name: string;
  config: Record<string, unknown>;
  enabled?: boolean;
  tags?: string[];
}

export interface IntegrationInstanceUpdate {
  display_name?: string;
  config?: Record<string, unknown>;
  enabled?: boolean;
  tags?: string[];
}

export interface ConfigFieldSchema {
  type: 'string' | 'integer' | 'boolean' | 'float' | string;
  label: string;
  secret: boolean;
}

export interface IntegrationSchema {
  name: string;
  version: string;
  description: string;
  requires_oauth: boolean;
  config_schema: Record<string, ConfigFieldSchema>;
}

export type IntegrationSchemasResponse = Record<string, IntegrationSchema>;

export interface ZerosPreset {
  id: string;
  integration_id: string;
  preset_number: number;
  preset_name: string;
  description: string | null;
}

export interface ZerosPresetCreate {
  integration_id: string;
  preset_number: number;
  preset_name: string;
  description?: string | null;
}

// ── Automations & scripts (backend/schemas/automation.py) ───────────────

export interface AutomationFolder {
  id: string;
  name: string;
  parent_id: string | null;
  created_at: string;
}

export interface AutomationFolderCreate {
  name: string;
  parent_id?: string | null;
}

export type AutomationFolderUpdate = Partial<AutomationFolderCreate>;

export interface Automation {
  id: string;
  folder_id: string | null;
  alias: string;
  enabled: boolean;
  trigger_yaml: string;
  condition_yaml: string | null;
  action_yaml: string;
  last_triggered_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AutomationCreate {
  folder_id?: string | null;
  alias: string;
  enabled?: boolean;
  trigger_yaml: string;
  condition_yaml?: string | null;
  action_yaml: string;
}

export type AutomationUpdate = Partial<AutomationCreate>;

export interface AutomationRun {
  id: string;
  automation_id: string;
  triggered_at: string;
  trigger_event: Record<string, unknown> | null;
  status: string;
  failed_action_index: number | null;
  error: string | null;
  created_at: string;
}

export interface ValidateRequest {
  trigger_yaml: string;
  condition_yaml?: string | null;
  action_yaml: string;
}

export interface ValidateResponse {
  valid: boolean;
  errors: string[];
}

export interface TriggerResponse {
  automation_id: string;
  status: string;
  failed_action_index: number | null;
  error: string | null;
  actions_executed: number;
}

// ── Integration test-connection (backend `POST /{entity_id}/test`) ──────
export interface IntegrationTestResult {
  ok: boolean;
  message?: string;
  detail?: string;
}

// ── Audit log (backend/schemas/audit.py `AuditLogRead`) ──────────────────

export interface AuditLogEntry {
  id: string;
  user_id: string | null;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  changes: Record<string, unknown> | null;
  ip_address: string | null;
  created_at: string;
}

export interface AuditLogFilters {
  user?: string;
  action?: string;
  resource_type?: string;
  start_date?: string;
  end_date?: string;
  limit?: number;
  offset?: number;
}

// ── Cluster / integration status (plan §11 `GET /api/v1/status`) ─────────
// Shape is provisional pending the parallel backend wave (plan finding 1.7)
// — kept optional/defensive so the UI degrades gracefully if a field is
// renamed or omitted.

export interface ClusterLeaderInfo {
  is_leader?: boolean;
  leader_address?: string | null;
}

export interface IntegrationHealthEntry {
  entity_id?: string;
  status?: string;
  last_event_at?: number | null;
}

export interface ClusterStatus {
  leader?: ClusterLeaderInfo;
  integrations?: IntegrationHealthEntry[];
}

export interface Script {
  id: string;
  name: string;
  description: string | null;
  action_yaml: string;
  created_at: string;
  updated_at: string;
}

export interface ScriptCreate {
  name: string;
  description?: string | null;
  action_yaml: string;
}

export type ScriptUpdate = Partial<ScriptCreate>;

// ── Timers (backend/schemas/timer.py) ───────────────────────────────────

export interface TimerInstance {
  id: string;
  entity_id: string;
  display_name: string;
  field_set_id: number;
  field_id: number;
  tags: string[];
  enabled: boolean;
  duration_s: number;
  created_at: string;
  updated_at: string;
  prompter_token: string;
}

export interface TimerInstanceCreate {
  entity_id: string;
  display_name: string;
  field_set_id: number;
  field_id: number;
  tags?: string[];
  enabled?: boolean;
  duration_s?: number;
}

export interface TimerInstanceUpdate {
  display_name?: string;
  field_set_id?: number;
  field_id?: number;
  tags?: string[];
  enabled?: boolean;
  duration_s?: number;
  regenerate_token?: boolean;
}

export interface PrompterCue {
  id: string;
  timer_entity_id: string;
  content: string;
  type: 'script' | 'note' | 'runsheet' | string;
  sort_order: number;
  is_active: boolean;
  created_by: string | null;
  created_at: string;
}

export interface PrompterCueCreate {
  timer_entity_id: string;
  content: string;
  type?: string;
  sort_order?: number;
  is_active?: boolean;
}

export interface PrompterCueUpdate {
  content?: string;
  type?: string;
  sort_order?: number;
  is_active?: boolean;
}

// ── Overlays (backend/schemas/overlay.py) ───────────────────────────────

export interface OverlayInstance {
  id: string;
  entity_id: string;
  display_name: string;
  field_set_id: number;
  tags: string[];
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface OverlayInstanceCreate {
  entity_id: string;
  display_name: string;
  field_set_id: number;
  tags?: string[];
  enabled?: boolean;
}

export type OverlayInstanceUpdate = Partial<OverlayInstanceCreate>;

export interface OverlayPreview {
  entity_id: string;
  match: Record<string, unknown> | null;
  teams: string[];
}

// ── Teams (backend/schemas/team.py) ─────────────────────────────────────

export interface TeamProfile {
  team_number: string;
  pit_location: string | null;
  bio: string | null;
  robot_name: string | null;
  video_360_s3_key: string | null;
  video_processing_status: 'NONE' | 'PROCESSING' | 'DONE' | 'FAILED' | string;
  cached_stats: Record<string, unknown> | null;
  extra_notes: string | null;
  updated_at: string;
}

export interface TeamProfileUpdate {
  pit_location?: string | null;
  extra_notes?: string | null;
}

export interface VideoProcessingStatus {
  team_number: string;
  video_processing_status: string;
  video_360_s3_key: string | null;
  error: string | null;
}

export interface VideoUploadAccepted {
  team_number: string;
  video_processing_status: string;
}

export interface BatchVideoUrlsResponse {
  videos: Record<string, string | null>;
}

// ── Settings (backend/schemas/settings.py) ──────────────────────────────

export interface RolePermission {
  authentik_group: string;
  permission: string;
}

export interface ApiKey {
  id: string;
  name: string;
  permissions: string[];
  created_by: string | null;
  last_used_at: string | null;
  created_at: string;
  revoked: boolean;
}

export interface ApiKeyCreate {
  name: string;
  permissions: string[];
}

export interface ApiKeyCreateResponse extends ApiKey {
  raw_key: string;
}

export interface SystemSetting {
  key: string;
  value: Record<string, unknown>;
  updated_by: string | null;
  updated_at: string;
}

// ── Auth (backend/routers/auth.py `whoami`) ─────────────────────────────

export interface WhoAmI {
  authenticated: boolean;
  subject?: string;
  is_admin_local?: boolean;
  permissions?: string[];
}

// ── Health (backend/schemas/health.py) ──────────────────────────────────

export interface HealthResponse {
  status: string;
}

export interface ReadyResponse {
  status: 'ok' | 'degraded' | string;
  db: boolean;
  redis: boolean;
}

// ── Event bus (backend/schemas/events.py, plan §C.4) — FROZEN interface ─

export interface EventBusMessage {
  entity_id: string;
  entity_tags: string[];
  type: string;
  timestamp: number;
  payload: Record<string, unknown>;
}

// ── Rooms + notifications (backend/schemas/room.py, notification.py) ────

export interface Room {
  slug: string;
  name: string;
  stream_url: string | null;
  teams: string[];
}

export interface RoomInput {
  name: string;
  stream_url: string | null;
  teams: string[];
}

export interface NotificationStats {
  push_configured: boolean;
  team_subscriptions: number;
  staff_subscriptions: number;
  subscriptions_by_room: Record<string, number>;
}

export interface BroadcastRequest {
  title: string;
  message: string;
  /** null = every room (and every team subscriber). */
  rooms: string[] | null;
  push: boolean;
  modal: boolean;
}

export interface BroadcastResult {
  rooms: number;
  pushed: number;
}

export type StaffCategory = 'system' | 'broadcast';

// ── Button pages (issue #11) ──────────────────────────────────────────

export interface ButtonItem {
  id: string;
  page_id: string;
  label: string;
  color: string;
  position: number;
  action_yaml: string;
}

export interface ButtonInput {
  label: string;
  color: string;
  position: number;
  action_yaml: string;
}

export interface ButtonPage {
  id: string;
  name: string;
  position: number;
  columns: number;
  buttons: ButtonItem[];
}

export interface PressResult {
  status: 'success' | 'failed';
  error: string | null;
  failed_action_index: number | null;
  actions_executed: number;
}
