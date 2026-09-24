import type { RunStatus, RunState } from "./types";

export type Tone = "pass" | "fail" | "gate" | "run" | "idle";

export const STATUS_LABEL: Record<RunStatus, string> = {
  queued: "Queued",
  mapping: "Mapping system",
  copying: "Building copy",
  writing_requirements: "Writing requirements",
  awaiting_requirements: "Needs requirements approval",
  baseline_testing: "Baseline testing",
  agent_working: "Agent working",
  final_gate: "Final gate",
  awaiting_ship: "Needs ship approval",
  shipped: "Shipped",
  blocked: "Blocked",
  rejected: "Rejected",
  failed: "Failed",
  testing: "Testing",
  passed: "Passed",
};

export function statusTone(s: string): Tone {
  switch (s) {
    case "shipped":
    case "passed":
      return "pass";
    case "blocked":
    case "rejected":
    case "failed":
      return "fail";
    case "awaiting_requirements":
    case "awaiting_ship":
      return "gate";
    case "queued":
      return "idle";
    default:
      return "run";
  }
}

export const TERMINAL = new Set(["shipped", "blocked", "rejected", "failed", "passed"]);
export const GATES = new Set(["awaiting_requirements", "awaiting_ship"]);

export function statusLabel(s: string): string {
  return (STATUS_LABEL as Record<string, string>)[s] ?? s.replace(/_/g, " ");
}

// Pipeline steps shown in the run timeline.
export interface PipeStep {
  key: string;
  label: string;
  statuses: string[];
  gate?: boolean;
}

export const CHANGE_STEPS: PipeStep[] = [
  { key: "queued", label: "Queued", statuses: ["queued"] },
  { key: "mapping", label: "Map system", statuses: ["mapping"] },
  { key: "copying", label: "Build copy", statuses: ["copying"] },
  { key: "requirements", label: "Write requirements", statuses: ["writing_requirements"] },
  { key: "gate1", label: "Approve requirements", statuses: ["awaiting_requirements"], gate: true },
  { key: "baseline", label: "Baseline test", statuses: ["baseline_testing"] },
  { key: "agent", label: "Agent changes code", statuses: ["agent_working"] },
  { key: "final", label: "Final gate", statuses: ["final_gate"] },
  { key: "gate2", label: "Approve ship", statuses: ["awaiting_ship"], gate: true },
  { key: "done", label: "Shipped", statuses: ["shipped"] },
];

export const TEST_STEPS: PipeStep[] = [
  { key: "queued", label: "Queued", statuses: ["queued"] },
  { key: "copying", label: "Build copy", statuses: ["mapping", "copying"] },
  { key: "testing", label: "Run tests", statuses: ["testing"] },
  { key: "done", label: "Result", statuses: ["passed", "failed"] },
];

/**
 * Index of the step the run is on (or stopped at). For terminal failures the state
 * has no status history, so infer where it stopped from which results exist.
 */
export function currentStepIndex(kind: string, status: string, st?: Partial<RunState>): number {
  const steps = kind === "test" ? TEST_STEPS : CHANGE_STEPS;
  const i = steps.findIndex((s) => s.statuses.includes(status));
  if (i >= 0) return i;
  if (kind === "test") return st?.after || st?.before ? 2 : 1;
  const at = (k: string) => steps.findIndex((s) => s.key === k);
  if (status === "rejected") return st?.after || st?.final_checks ? at("gate2") : at("gate1");
  if (status === "blocked") return at("final");
  // failed: furthest stage with evidence
  if (st?.after || st?.final_checks) return at("final");
  if (st?.rounds && st.rounds.length) return at("agent");
  if (st?.before) return at("baseline");
  if (st?.requirements) return at("gate1");
  if (st?.fidelity) return at("requirements");
  return at("mapping");
}

export function fmtTime(ts?: number | null): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function fmtAgo(ts?: number | null): string {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function fmtMs(ms?: number | null): string {
  if (ms == null) return "";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function fmtUsd(n?: number | null): string {
  if (n == null) return "—";
  return `$${n.toFixed(2)}`;
}

export function fmtTrigger(t: string): string {
  return ({ manual: "Manual", pr: "Pull request", schedule: "Schedule", ci: "CI", mcp: "MCP" } as Record<string, string>)[t] ?? t;
}

/** Screenshots are base64 PNG per contract; mock data uses base64 SVG. */
export function imgSrc(b64: string): string {
  if (b64.startsWith("data:")) return b64;
  if (b64.startsWith("PHN2") || b64.startsWith("PD94")) return `data:image/svg+xml;base64,${b64}`;
  return `data:image/png;base64,${b64}`;
}

export function describeCron(cron: string | null): string {
  if (!cron) return "Manual only";
  const known: Record<string, string> = {
    "0 * * * *": "Every hour",
    "0 */6 * * *": "Every 6 hours",
    "0 8 * * *": "Daily at 8:00",
  };
  return known[cron] ?? `Custom: ${cron}`;
}

export function isValidCron(c: string): boolean {
  const parts = c.trim().split(/\s+/);
  if (parts.length !== 5) return false;
  return parts.every((p) => /^[\d*/,\-A-Za-z?]+$/.test(p));
}
