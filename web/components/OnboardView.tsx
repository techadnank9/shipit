"use client";

import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { CopyProfile, Onboarding, OnboardingStatus, Sweep, SweepPage, SweepTest } from "@/lib/types";
import { Crumbs, Empty, ErrorNote, Panel, Skeleton, useLoad, useWho } from "./ui";

const ACTIVE = new Set<OnboardingStatus>(["queued", "drafting", "booting", "revising", "sweeping"]);
const LABEL: Record<OnboardingStatus, string> = {
  not_started: "Not started",
  queued: "Queued",
  drafting: "Reading the system",
  booting: "Booting a copy",
  revising: "Fixing the recipe",
  sweeping: "Testing every page",
  ready: "Ready for approval",
  failed: "Failed",
};
const STEPS: { key: OnboardingStatus[]; label: string }[] = [
  { key: ["drafting"], label: "Read system" },
  { key: ["booting", "revising"], label: "Boot copy" },
  { key: ["sweeping"], label: "Test every page" },
  { key: ["ready"], label: "Approve" },
];

function tone(s: OnboardingStatus, approved: boolean) {
  if (approved && s === "ready") return "pass";
  if (s === "failed") return "fail";
  if (s === "ready") return "gate";
  if (s === "not_started") return "idle";
  return "run";
}

export default function OnboardView() {
  const id = useSearchParams().get("id") ?? "";
  const project = useLoad(() => api.getProject(id), [id]);
  const ob = useLoad<Onboarding>(() => api.onboarding(id), [id]);
  const sweep = useLoad<Sweep | null>(() => api.sweep(id).catch(() => null), [id]);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const st = ob.data?.onboarding.status ?? "not_started";
  const active = ACTIVE.has(st);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => void ob.reload(), 3000);
    return () => clearInterval(t);
  }, [active, ob.reload]);
  // When a sweep finishes, load its results.
  useEffect(() => {
    if (!active && ob.data?.onboarding.sweep) void sweep.reload();
  }, [active, ob.data?.onboarding.sweep, sweep.reload]);

  async function act(fn: () => Promise<Onboarding>) {
    setBusy(true);
    setErr(null);
    try {
      ob.setData(await fn());
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }

  const approved = !!ob.data?.approved_by && !!ob.data?.profile;
  const recipe = ob.data?.draft ?? ob.data?.profile ?? null;
  const o = ob.data?.onboarding;
  return (
    <>
      <Crumbs
        items={[
          { href: "/", label: "Projects" },
          { href: `/project?id=${encodeURIComponent(id)}`, label: project.data?.name ?? id },
          { label: "Onboarding" },
        ]}
      />
      <div className="page-head">
        <div className="min0">
          <h1>Onboarding</h1>
          <p className="muted small">
            Carbon Copy learns how to boot {project.data?.name ?? "this system"} from scratch, proves it on a fresh copy, then goes
            through every page like a manual tester. You approve the recipe once; every run after uses it.
          </p>
        </div>
        <div className="row-gap">
          {ob.data && (
            <span className={`pill pill-${tone(st, approved)}`}>
              <span className="pill-mark" aria-hidden="true" />
              {approved && st === "ready" ? "Approved" : LABEL[st]}
            </span>
          )}
          {!active && (
            <button type="button" className="btn btn-primary" disabled={busy} onClick={() => act(() => api.startOnboarding(id))}>
              {st === "not_started" ? "Start onboarding" : "Onboard again"}
            </button>
          )}
        </div>
      </div>
      <ErrorNote error={ob.error ?? project.error} onRetry={ob.reload} />
      <ErrorNote error={err} />
      {!ob.data && !ob.error && <Skeleton lines={6} />}

      {o && st !== "not_started" && (
        <>
          <ol className="ob-steps" aria-label="Onboarding progress">
            {STEPS.map((s, i) => {
              const idx = STEPS.findIndex((x) => x.key.includes(st));
              const state = st === "failed" ? (i === 0 ? "done" : "todo") : idx > i || (approved && st === "ready") ? "done" : idx === i ? "now" : "todo";
              return (
                <li key={s.label} className={`ob-step ob-${state}`}>
                  <span className="ob-dot" aria-hidden="true" />
                  {s.label}
                </li>
              );
            })}
          </ol>

          <div className="ob-grid">
            <div className="col-main">
              {st === "ready" && recipe && !approved && (
                <ApprovePanel onApprove={(who) => act(() => api.approveProfile(id, who))} busy={busy} />
              )}
              {st === "failed" && o.error && (
                <div className="note note-fail" role="alert">
                  <span>Onboarding stopped: {o.error.split("\n")[0]}</span>
                </div>
              )}
              <SweepPanel
                sweep={sweep.data}
                running={st === "sweeping"}
                canRun={!active && !!recipe}
                onRun={() => act(() => api.startSweep(id))}
                pid={id}
              />
            </div>
            <div className="ob-side">
              <Panel title="Live log" id="log" sub={active ? "Updating every few seconds" : undefined}>
                <ol className="ob-log">
                  {o.log.map((l, i) => (
                    <li key={i} className={l.error ? "ob-log-err" : undefined}>
                      <span className="mono xsmall muted">{new Date(l.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })}</span>
                      <span>{l.message}</span>
                      {l.error && <pre className="ob-pre">{l.error}</pre>}
                    </li>
                  ))}
                </ol>
              </Panel>
              {recipe && <RecipePanel recipe={recipe} fidelity={o.fidelity?.score} approvedBy={approved ? ob.data?.approved_by?.who : undefined} />}
            </div>
          </div>
        </>
      )}
      {o && st === "not_started" && (
        <Panel title="What happens">
          <ol className="ob-explain">
            <li>Carbon Copy reads the repo: pages, API routes, tables, schema files, settings.</li>
            <li>It writes a boot recipe: which databases, which setup steps in which order, and a small seed.</li>
            <li>It boots a fresh copy from the recipe. If a step fails it works out why and fixes the recipe.</li>
            <li>It goes through every page like a manual tester and reports what is broken.</li>
            <li>You review and approve the recipe once.</li>
          </ol>
        </Panel>
      )}
    </>
  );
}

function ApprovePanel({ onApprove, busy }: { onApprove: (who: string) => void; busy: boolean }) {
  const [who, setWho] = useWho();
  const [err, setErr] = useState("");
  return (
    <Panel title="Approve the recipe" tone="gate" sub="Every copy of this system will boot from this recipe. Review it on the right.">
      <form
        className="row-gap"
        onSubmit={(e) => {
          e.preventDefault();
          if (!who.trim()) return setErr("Enter your name first. Approvals are signed in the audit log.");
          onApprove(who.trim());
        }}
      >
        <div className="field grow">
          <label htmlFor="ob-who">Your name</label>
          <input id="ob-who" value={who} onChange={(e) => setWho(e.target.value)} placeholder="maria.chen" autoComplete="off" />
        </div>
        <button type="submit" className="btn btn-primary" disabled={busy}>
          Approve recipe
        </button>
      </form>
      {err && <p className="small fail-ink">{err}</p>}
    </Panel>
  );
}

function RecipePanel({ recipe, fidelity, approvedBy }: { recipe: CopyProfile; fidelity?: number; approvedBy?: string }) {
  const unset = Object.entries(recipe.env_not_needed);
  const unavailable = Object.entries(recipe.env_unavailable);
  return (
    <Panel title="Boot recipe" id="recipe" sub={approvedBy ? `Approved by ${approvedBy}` : "Draft, waiting for approval"}>
      <div className="stats">
        <div className="stat">
          <span className="stat-n">{fidelity ?? "–"}%</span>
          <span className="stat-l">fidelity</span>
        </div>
        <div className="stat">
          <span className="stat-n">{recipe.setup.length}</span>
          <span className="stat-l">setup steps</span>
        </div>
        <div className="stat">
          <span className="stat-n">{recipe.port}</span>
          <span className="stat-l">app port</span>
        </div>
      </div>
      <h3>Services</h3>
      <ul className="chips">
        {recipe.services.map((s) => (
          <li className="chip" key={s}>
            {s}
          </li>
        ))}
      </ul>
      <h3>App</h3>
      <p className="mono small">{recipe.build === "node" ? recipe.start : "repo Dockerfile"} · health {recipe.health}</p>
      <h3>Setup, in order</h3>
      <ol className="ob-setup">
        {recipe.setup.map((s, i) => (
          <li key={i}>
            <span className="chip">{s.kind === "postgres_sql" ? "sql" : s.kind}</span>
            <span className="mono small ob-wrap">{s.value}</span>
          </li>
        ))}
      </ol>
      {unset.length > 0 && (
        <>
          <h3>Left unset on purpose</h3>
          <ul className="ob-env">
            {unset.map(([k, why]) => (
              <li key={k}>
                <span className="mono small">{k}</span>
                <span className="muted small">{why}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {unavailable.length > 0 && (
        <>
          <h3>Not available in copies</h3>
          <ul className="ob-env">
            {unavailable.map(([k, why]) => (
              <li key={k}>
                <span className="mono small">{k}</span>
                <span className="muted small">{why}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {recipe.notes && (
        <>
          <h3>Notes</h3>
          <p className="small">{recipe.notes}</p>
        </>
      )}
    </Panel>
  );
}

function SweepPanel({ sweep, running, canRun, onRun, pid }: { sweep: Sweep | null; running: boolean; canRun: boolean; onRun: () => void; pid: string }) {
  const [filter, setFilter] = useState<"all" | "issues" | "failing">("all");
  const t = sweep?.totals;
  const pages = (sweep?.pages ?? []).filter((p) =>
    filter === "issues" ? p.issues.length > 0 : filter === "failing" ? p.results.some((r) => !r.passed) : true,
  );
  return (
    <Panel
      title="Every page, tested"
      id="sweep"
      sub="Each page opened in a real browser: errors recorded, screen reviewed, its main flows tested."
      actions={
        canRun && (
          <button type="button" className="btn btn-small" onClick={onRun}>
            {sweep ? "Test every page again" : "Test every page"}
          </button>
        )
      }
    >
      {running && <p className="small muted">Going through the pages now. Results appear when the sweep finishes.</p>}
      {!sweep && !running && <Empty>No page sweep yet.</Empty>}
      {t && (
        <>
          <div className="stats">
            <div className="stat">
              <span className="stat-n">{t.pages}</span>
              <span className="stat-l">pages</span>
            </div>
            <div className="stat">
              <span className="stat-n fail-ink">{t.issues.high}</span>
              <span className="stat-l">high issues</span>
            </div>
            <div className="stat">
              <span className="stat-n">{t.issues.medium + t.issues.low}</span>
              <span className="stat-l">other issues</span>
            </div>
            <div className="stat">
              <span className="stat-n">
                {t.tests_passed}/{t.tests}
              </span>
              <span className="stat-l">tests passed</span>
            </div>
          </div>
          <div className="ob-filter" role="group" aria-label="Filter pages">
            {(["all", "issues", "failing"] as const).map((f) => (
              <button key={f} type="button" className={`btn btn-small${filter === f ? " btn-primary" : ""}`} onClick={() => setFilter(f)}>
                {f === "all" ? "All pages" : f === "issues" ? "With issues" : "Failing tests"}
              </button>
            ))}
          </div>
          {sweep.unvisited_routes.length > 0 && (
            <p className="small muted">
              Not reached (no link found to a real example): <span className="mono">{sweep.unvisited_routes.join(", ")}</span>
            </p>
          )}
          <ul className="ob-pages">
            {pages.map((p) => (
              <PageCard key={p.path} page={p} pid={pid} />
            ))}
          </ul>
        </>
      )}
    </Panel>
  );
}

function PageCard({ page, pid }: { page: SweepPage; pid: string }) {
  const [open, setOpen] = useState(page.issues.some((i) => i.severity === "high") || page.results.some((r) => !r.passed));
  const [big, setBig] = useState(false);
  const failing = page.results.filter((r) => !r.passed).length;
  const ok = typeof page.status === "number" && page.status < 400;
  return (
    <li className="ob-page">
      <button type="button" className="ob-page-head" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="mono">{page.path}</span>
        <span className="row-gap small">
          <span className={`pill pill-${ok ? "pass" : "fail"}`}>{String(page.status)}</span>
          {page.issues.length > 0 && <span className="pill pill-gate">{page.issues.length} issue{page.issues.length === 1 ? "" : "s"}</span>}
          {page.results.length > 0 && (
            <span className={`pill pill-${failing ? "fail" : "pass"}`}>
              {page.results.length - failing}/{page.results.length} tests
            </span>
          )}
        </span>
      </button>
      {open && (
        <div className="ob-page-body">
          {page.screenshot && (
            <button type="button" className={`ob-shot${big ? " ob-shot-big" : ""}`} onClick={() => setBig(!big)} aria-label="Enlarge screenshot">
              <img src={`data:image/jpeg;base64,${page.screenshot}`} alt={`Screenshot of ${page.path}`} />
            </button>
          )}
          <div className="ob-page-info">
            {page.summary && <p className="small">{page.summary}</p>}
            {page.issues.length > 0 && (
              <ul className="ob-issues">
                {page.issues.map((i, n) => (
                  <li key={n}>
                    <span className={`pill pill-${i.severity === "high" ? "fail" : i.severity === "medium" ? "gate" : "idle"}`}>{i.severity}</span>
                    <span className="small">
                      <strong>{i.title}</strong> {i.detail}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {(page.console_errors.length > 0 || page.failed_requests.length > 0 || page.page_errors.length > 0) && (
              <pre className="ob-pre">{[...page.page_errors, ...page.console_errors, ...page.failed_requests].join("\n")}</pre>
            )}
            {page.results.map((r) => (
              <TestResult key={r.name} test={r} pid={pid} />
            ))}
          </div>
        </div>
      )}
    </li>
  );
}

function TestResult({ test, pid }: { test: SweepTest; pid: string }) {
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const failed = test.steps.find((s) => !s.ok && !s.retried);
  return (
    <div className="ob-test">
      <div className="row-gap">
        <span className={`pill pill-${test.passed ? "pass" : "fail"}`}>{test.passed ? "Pass" : "Fail"}</span>
        <strong className="small grow">{test.name}</strong>
        <button
          type="button"
          className="btn btn-small btn-quiet"
          disabled={saved}
          onClick={async () => {
            try {
              await api.addTest(pid, { name: test.name, instructions: test.instructions });
              setSaved(true);
            } catch (e) {
              setErr(e);
            }
          }}
        >
          {saved ? "Saved" : "Save as test"}
        </button>
      </div>
      <p className="xsmall muted">{test.instructions}</p>
      {failed && (
        <p className="xsmall fail-ink">
          Failed at {failed.action} {failed.target}: {failed.error}
        </p>
      )}
      <ErrorNote error={err} />
    </div>
  );
}
