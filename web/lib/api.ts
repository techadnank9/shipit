import type {
  AuditResult,
  DbCheck,
  Health,
  Onboarding,
  Project,
  Run,
  SavedTest,
  Schedule,
  Sweep,
  SystemMap,
  Trigger,
} from "./types";

// Mock backend is loaded only in mock builds, so it stays out of the production bundle.
type MockModule = typeof import("./mock");
function mock<T>(fn: (m: MockModule) => T | Promise<T>): Promise<T> {
  return import("./mock").then(fn);
}

export const MOCK = process.env.NEXT_PUBLIC_MOCK === "1";
export const API_BASE = `${(process.env.NEXT_PUBLIC_API ?? "").replace(/\/$/, "")}/api`;

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

// Optional bearer token for servers started with CCOPY_API_TOKEN.
// Set once in the browser console: localStorage.setItem("ccopy.token", "…")
function token(): string | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage.getItem("ccopy.token");
  } catch {
    return null;
  }
}

export function hasToken(): boolean {
  return !!token();
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const t = token();
  if (t) headers.Authorization = `Bearer ${t}`;
  let res: Response;
  try {
    res = await fetch(API_BASE + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, `Cannot reach the Carbon Copy API at ${API_BASE}.`);
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j && typeof j.detail === "string") detail = j.detail;
      else if (j && j.detail) detail = JSON.stringify(j.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export const api = {
  health: (): Promise<Health> => (MOCK ? mock((m) => m.health()) : req("GET", "/health")),

  listProjects: (): Promise<Project[]> => (MOCK ? mock((m) => m.listProjects()) : req("GET", "/projects")),
  createProject: (b: { name: string; repo_path?: string; git_url?: string }): Promise<Project> =>
    MOCK ? mock((m) => m.createProject(b)) : req("POST", "/projects", b),
  getProject: (pid: string): Promise<Project> =>
    MOCK ? mock((m) => m.getProject(pid)) : req("GET", `/projects/${enc(pid)}`),
  getMap: (pid: string): Promise<SystemMap> =>
    MOCK ? mock((m) => m.getMap(pid)) : req("GET", `/projects/${enc(pid)}/map`),

  listTests: (pid: string): Promise<SavedTest[]> =>
    MOCK ? mock((m) => m.listTests(pid)) : req("GET", `/projects/${enc(pid)}/tests`),
  addTest: (pid: string, b: { name: string; instructions: string }): Promise<SavedTest> =>
    MOCK ? mock((m) => m.addTest(pid, b)) : req("POST", `/projects/${enc(pid)}/tests`, b),
  deleteTest: (tid: string): Promise<{ ok: boolean }> =>
    MOCK ? mock((m) => m.deleteTest(tid)) : req("DELETE", `/tests/${enc(tid)}`),

  listChecks: (pid: string): Promise<DbCheck[]> =>
    MOCK ? mock((m) => m.listChecks(pid)) : req("GET", `/projects/${enc(pid)}/checks`),
  addCheck: (pid: string, b: { description: string; sql: string; expect: string }): Promise<DbCheck> =>
    MOCK ? mock((m) => m.addCheck(pid, b)) : req("POST", `/projects/${enc(pid)}/checks`, b),

  requestChange: (pid: string, request: string): Promise<Run> =>
    MOCK ? mock((m) => m.requestChange(pid, request)) : req("POST", `/projects/${enc(pid)}/changes`, { request }),
  startTestRun: (pid: string, trigger: Trigger = "manual", test_ids?: string[]): Promise<Run> =>
    MOCK
      ? mock((m) => m.startTestRun(pid, trigger))
      : req("POST", `/projects/${enc(pid)}/test-runs`, test_ids ? { trigger, test_ids } : { trigger }),

  listRuns: (q: { project_id?: string; kind?: string; status?: string; limit?: number }): Promise<Run[]> => {
    if (MOCK) return mock((m) => m.listRuns(q));
    const p = new URLSearchParams();
    Object.entries(q).forEach(([k, v]) => v !== undefined && v !== "" && p.set(k, String(v)));
    return req("GET", `/runs?${p.toString()}`);
  },
  getRun: (rid: string): Promise<Run> => (MOCK ? mock((m) => m.getRun(rid)) : req("GET", `/runs/${enc(rid)}`)),
  approveRequirements: (rid: string, who: string): Promise<Run> =>
    MOCK ? mock((m) => m.approveRequirements(rid, who)) : req("POST", `/runs/${enc(rid)}/approve-requirements`, { who }),
  reject: (rid: string, who: string, reason: string): Promise<Run> =>
    MOCK ? mock((m) => m.reject(rid, who, reason)) : req("POST", `/runs/${enc(rid)}/reject`, { who, reason }),
  approveShip: (rid: string, who: string): Promise<Run> =>
    MOCK ? mock((m) => m.approveShip(rid, who)) : req("POST", `/runs/${enc(rid)}/approve-ship`, { who }),
  audit: (rid: string): Promise<AuditResult> =>
    MOCK ? mock((m) => m.audit(rid)) : req("GET", `/runs/${enc(rid)}/audit`),

  onboarding: (pid: string): Promise<Onboarding> => req("GET", `/projects/${enc(pid)}/onboarding`),
  startOnboarding: (pid: string): Promise<Onboarding> => req("POST", `/projects/${enc(pid)}/onboard`),
  approveProfile: (pid: string, who: string): Promise<Onboarding> => req("POST", `/projects/${enc(pid)}/profile/approve`, { who }),
  startSweep: (pid: string): Promise<Onboarding> => req("POST", `/projects/${enc(pid)}/sweep`),
  sweep: (pid: string): Promise<Sweep> => req("GET", `/projects/${enc(pid)}/sweep`),

  getSchedule: (pid: string): Promise<Schedule> =>
    MOCK ? mock((m) => m.getSchedule(pid)) : req("GET", `/projects/${enc(pid)}/schedule`),
  putSchedule: (pid: string, cron: string | null): Promise<Schedule> =>
    MOCK ? mock((m) => m.putSchedule(pid, cron)) : req("PUT", `/projects/${enc(pid)}/schedule`, { cron }),
};

function enc(s: string) {
  return encodeURIComponent(s);
}

/** URL of a run artifact (report / patch). */
export function artifactUrl(rid: string, which: "report" | "patch"): string {
  return `${API_BASE}/runs/${enc(rid)}/${which}`;
}

/** Mock mode only: open a generated stand-in for the report or patch. */
export async function openMockArtifact(rid: string, which: "report" | "patch") {
  const w = window.open("", "_blank");
  const url = await mock((m) => m.artifactBlobUrl(rid, which));
  if (w) w.location.href = url;
}

/** SSE is only used against a real server without a bearer token (EventSource cannot send headers). */
export function eventsUrl(rid: string): string | null {
  if (MOCK || hasToken()) return null;
  return `${API_BASE}/runs/${enc(rid)}/events`;
}
