// Shapes from INTERFACES.md (contract v0.1). Optional fields are defensive:
// the server is built in parallel, so the UI never assumes a field is present.

export type RunStatus =
  | "queued"
  | "mapping"
  | "copying"
  | "writing_requirements"
  | "awaiting_requirements"
  | "baseline_testing"
  | "agent_working"
  | "final_gate"
  | "awaiting_ship"
  | "shipped"
  | "blocked"
  | "rejected"
  | "failed"
  | "testing"
  | "passed";

export type Trigger = "manual" | "pr" | "schedule" | "ci" | "mcp";

export interface Project {
  id: string;
  name: string;
  repo_path: string | null;
  git_url: string | null;
  created_at: number;
  last_run: Run | null;
}

export interface SavedTest {
  id: string;
  project_id: string;
  name: string;
  instructions: string;
  created_at: number;
}

export interface DbCheck {
  id: string;
  project_id: string;
  description: string;
  sql: string;
  expect: string;
  created_at: number;
}

export interface Schedule {
  project_id: string;
  cron: string | null;
  next_run_at: number | null;
}

export interface Step {
  kind: "ACT" | "ASSERT" | string;
  action: string;
  target: string;
  value: string;
  why: string;
  ok: boolean;
  error: string | null;
  ms: number;
  screenshot?: string | null;
}

export interface BrowserTestResult {
  name: string;
  instructions: string;
  passed: boolean;
  video?: string | null;
  steps: Step[];
}

export interface DbCheckResult {
  description: string;
  sql: string;
  expect: string;
  got: string;
  passed: boolean;
}

export interface TestSuiteResult {
  tests: BrowserTestResult[];
  db_checks: DbCheckResult[];
  passed: boolean;
}

export interface CheckResult {
  name: string;
  passed: boolean;
  summary: string;
  details?: string[];
}

export interface Round {
  round: number;
  passed: boolean;
  checks: CheckResult[];
}

export interface Requirements {
  title: string;
  summary: string;
  risk: "low" | "medium" | "high";
  acceptance_criteria: string[];
  api_tests?: string[];
  browser_tests: { name: string; instructions: string }[];
  db_invariants: { description: string; sql: string; expect: string }[];
  infra_changes?: string[];
}

export interface RunState {
  id: string;
  kind: "change" | "test";
  status: RunStatus;
  repo: string;
  request: string;
  created_at: number;
  updated_at: number;
  requirements: Requirements | null;
  fidelity: { score: number; missing_env: string[]; unemulated_aws: string[] } | null;
  before: TestSuiteResult | null;
  after: TestSuiteResult | null;
  final_checks: CheckResult[] | null;
  rounds: Round[];
  agent: { summary: string; cost_usd: number; edited_files: string[] } | null;
  error: string | null;
  report: string | null;
  patch: string | null;
}

export interface Run {
  id: string;
  project_id: string;
  kind: "change" | "test";
  status: RunStatus;
  trigger: Trigger | string;
  title: string;
  created_at: number;
  updated_at: number;
  passed: boolean | null;
  state?: Partial<RunState>;
}

export interface SystemMap {
  repo?: string;
  routes?: { method: string; path: string; handler: string; file?: string }[];
  tables?: string[];
  env_vars?: string[];
  aws_services?: string[];
  infrastructure?: { type: string; name: string; file?: string }[];
  schema_files?: string[];
  has_dockerfile?: boolean;
}

export interface AuditResult {
  verified: boolean;
  entries: { event?: string; who?: string; ts?: number; [k: string]: unknown }[];
}

export interface Health {
  ok: boolean;
  ai: "standin" | "claude" | string;
}

export type RunEvent =
  | { event: "status"; data: { status: RunStatus } }
  | { event: "log"; data: { message: string } }
  | { event: "round"; data: Round }
  | { event: "tool"; data: { tool: string; target: string } }
  | { event: "browser"; data: { test: string; passed: boolean } };
