/**
 * JSON shapes of the uc-hub HTTP API v1 (docs/ai-harness/API.md).
 *
 * The hub produces them with the `to_json()` methods in
 * hub/src/uc_hub/core/types.py; field names and nullability follow those
 * methods exactly, so a change on either side must be mirrored here.
 */

export type Tier = "R" | "S" | "L" | "C";
export type Protocol = "bacnet-uc" | "bacnet-ip" | "mqtt";
export type Role = "viewer" | "operator" | "commissioner" | "admin";
export type Quality = "good" | "stale" | "fault" | "offline";
export type PointKind = "input" | "output" | "value";
export type SafetyClass = "normal" | "critical" | "life-safety";
export type Datatype = "real" | "int" | "bool" | "enum" | "string";
/** A point value: REAL -> number, binary -> 0/1, BOOLEAN -> boolean, NULL -> null. */
export type Value = number | boolean | string | null;

export interface ApiErrorBody {
  error: { code: string; message: string; details?: unknown };
}

// ---------------------------------------------------------------- site

export interface Health {
  ok: boolean;
  version: string;
  site: string;
  llm: { provider: string; model: string; configured: boolean };
  dev_mode: boolean;
}

export interface Me {
  user: string;
  roles: Role[];
}

export interface Space {
  id: string;
  name: string;
  parent: string | null;
}

export interface SiteSummary {
  devices: number;
  online: number;
  points: number;
  unassigned: number;
  pending_changes: number;
}

export interface Device {
  name: string;
  protocol: Protocol;
  address: string;
  online: boolean;
  managed: boolean;
  model: string;
  firmware: string;
  hwid: string;
  instance: number | null;
  space: string | null;
  last_seen: number | null;
  /** Point count; present in `Site.devices` and `DeviceDescription.device`. */
  points?: number;
}

export interface Site {
  name: string;
  description: string;
  spaces: Space[];
  devices: Device[];
  summary: SiteSummary;
}

/** BACnet-uc `<app status>` map (hub/third_party/management-protocol.md). */
export interface AppStatus {
  name: string;
  file: string;
  state: "stopped" | "starting" | "running" | "failed";
  autostart: boolean;
  period_ms: number;
  heap_kb: number;
  stack_kb: number;
  perms: string[];
  ticks: number;
  events: number;
  errors: number;
  last_error: string;
  uptime_ms: number;
}

export interface DeviceDescription {
  device: Device;
  points: Point[];
  apps: AppStatus[];
  extra: Record<string, unknown>;
}

export interface Point {
  id: string;
  device: string;
  obj: string;
  name: string;
  kind: PointKind;
  datatype: Datatype;
  units: string | null;
  writable: boolean;
  commandable: boolean;
  tags: string[];
  safety: SafetyClass;
  description: string;
  source: string;
  space: string | null;
}

export interface Reading {
  id: string;
  value: Value;
  ts: number;
  quality: Quality;
  priority?: number;
  error?: string;
}

export type PointWithReading = Point & { reading?: Reading };

export interface PointsQuery {
  q?: string;
  device?: string;
  space?: string;
  tag?: string;
  limit?: number;
  offset?: number;
}

export interface PointsPage {
  total: number;
  points: PointWithReading[];
}

export interface DiscoveredDevice {
  protocol: Protocol;
  address: string;
  instance: number | null;
  name: string;
  model: string;
  hwid: string;
  bacnet_uc: boolean;
  extra: Record<string, unknown>;
  known: boolean;
}

// ------------------------------------------------ manifest, plan, tests

export interface ManifestInfo {
  live_revision: number;
  draft_revision: number | null;
  yaml: string;
  draft_yaml: string | null;
}

export interface ManifestRevision {
  revision: number;
  created_at: number;
  author: string;
  message: string;
  live: boolean;
}

export type ChangeKind =
  | "upload-doc"
  | "upload-file"
  | "install-app"
  | "remove-app"
  | "reload"
  | "bridge-add"
  | "bridge-remove"
  | "bridge-update"
  | "tags"
  | "device-config";

export interface Change {
  id: string;
  /** Device name, or "gateway". */
  target: string;
  kind: ChangeKind;
  summary: string;
  diff: string;
  tier: Tier;
}

export interface Plan {
  id: string;
  revision: number;
  base_revision: number;
  created_at: number;
  targets: string[];
  warnings: string[];
  changes: Change[];
}

export type TestStatus = "pass" | "fail" | "error" | "skipped" | "running" | "not-run";

export interface TestResult {
  name: string;
  status: TestStatus;
  duration_ms: number;
  failed_step: number | null;
  detail: string;
  target: "live" | "sim";
}

export interface TestsInfo {
  results: TestResult[];
  updated_at: number | null;
}

// --------------------------------------------------------------- runs

export type RunState = "running" | "waiting_approval" | "waiting_answer" | "idle" | "failed" | "cancelled";

export interface RunSummary {
  id: string;
  title: string;
  state: RunState;
  created_at: number;
  updated_at: number;
  created_by: string;
  last_seq: number;
  model: string;
}

export type ApprovalState = "pending" | "approved" | "rejected" | "expired";

export interface Approval {
  id: string;
  run_id: string;
  call_id: string;
  tool: string;
  tier: Tier;
  title: string;
  summary: string[];
  diff: string;
  rollback: string;
  plan_id: string | null;
  state: ApprovalState;
  requested_at: number;
  expires_at: number;
  requested_by: string;
  decided_by: string | null;
  decided_at: number | null;
  comment: string | null;
}

export interface AuditEntry {
  ts: number;
  user: string;
  run_id: string | null;
  action: string;
  tool: string | null;
  tier: Tier | null;
  args: unknown;
  outcome: string;
  detail: string;
}

// --------------------------------------------------------- run events

interface RunEventBase {
  seq: number;
  run_id: string;
  ts: number;
}

export interface RunStateEvent extends RunEventBase {
  type: "run.state";
  state: RunState;
  reason?: string;
}

export interface MessageUserEvent extends RunEventBase {
  type: "message.user";
  text: string;
  user: string;
}

export interface ThinkingDeltaEvent extends RunEventBase {
  type: "thinking.delta";
  text: string;
}

export interface MessageDeltaEvent extends RunEventBase {
  type: "message.delta";
  text: string;
}

export interface MessageDoneEvent extends RunEventBase {
  type: "message.done";
  text: string;
}

export interface ToolCallEvent extends RunEventBase {
  type: "tool.call";
  call_id: string;
  tool: string;
  tier: Tier;
  /** `{}` until the model has finished streaming the arguments. */
  args: Record<string, unknown>;
}

export interface ToolArgsDeltaEvent extends RunEventBase {
  type: "tool.args.delta";
  call_id: string;
  /** Raw JSON fragment. */
  delta: string;
}

export interface ToolResultEvent extends RunEventBase {
  type: "tool.result";
  call_id: string;
  ok: boolean;
  summary: string;
  duration_ms: number;
  data?: unknown;
  handle?: string;
}

export interface ApprovalRequestEvent extends RunEventBase {
  type: "approval.request";
  approval: Approval;
}

export interface ApprovalDecidedEvent extends RunEventBase {
  type: "approval.decided";
  approval: Approval;
}

export interface QuestionEvent extends RunEventBase {
  type: "question";
  question_id: string;
  call_id: string;
  text: string;
  /** Free text is allowed when empty. */
  options: string[];
}

export interface QuestionAnsweredEvent extends RunEventBase {
  type: "question.answered";
  question_id: string;
  answer: string;
  user: string;
}

export interface PlanUpdatedEvent extends RunEventBase {
  type: "plan.updated";
  plan_id: string | null;
  revision: number | null;
  /** Number of changes in the plan. */
  changes: number;
}

export interface TestsUpdatedEvent extends RunEventBase {
  type: "tests.updated";
  results: TestResult[];
}

export interface ErrorEvent extends RunEventBase {
  type: "error";
  message: string;
  code?: string;
}

export type RunEvent =
  | RunStateEvent
  | MessageUserEvent
  | ThinkingDeltaEvent
  | MessageDeltaEvent
  | MessageDoneEvent
  | ToolCallEvent
  | ToolArgsDeltaEvent
  | ToolResultEvent
  | ApprovalRequestEvent
  | ApprovalDecidedEvent
  | QuestionEvent
  | QuestionAnsweredEvent
  | PlanUpdatedEvent
  | TestsUpdatedEvent
  | ErrorEvent;

export type RunEventType = RunEvent["type"];

// ------------------------------------------------------ request bodies

export interface CreateRunRequest {
  message: string;
  playbook?: string;
}

export interface DecisionRequest {
  decision: "approve" | "reject";
  comment?: string;
}

export interface DiscoverRequest {
  protocol?: Protocol;
  timeout_s?: number;
}
