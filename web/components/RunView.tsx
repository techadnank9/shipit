"use client";

import { useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, artifactUrl, MOCK, openMockArtifact } from "@/lib/api";
import {
  CHANGE_STEPS,
  currentStepIndex,
  fmtAgo,
  fmtMs,
  fmtTime,
  fmtTrigger,
  fmtUsd,
  GATES,
  imgSrc,
  TERMINAL,
  TEST_STEPS,
} from "@/lib/format";
import type {
  AuditResult,
  BrowserTestResult,
  CheckResult,
  Requirements,
  Round,
  Run,
  RunState,
  Step,
  TestSuiteResult,
} from "@/lib/types";
import { Crumbs, Empty, ErrorNote, Panel, ResultPill, Skeleton, StatusPill, useLoad, useWho } from "./ui";
import { useRunLive, type LiveLine, type LiveMode } from "./useRunLive";

export default function RunView() {
  const id = useSearchParams().get("id") ?? "";
  const { run, setRun, error, lines, mode, reload } = useRunLive(id);
  const project = useLoad(() => (run?.project_id ? api.getProject(run.project_id) : Promise.resolve(null)), [run?.project_id]);

  if (!run) {
    return (
      <>
        <Crumbs items={[{ href: "/", label: "Projects" }, { label: id || "Run" }]} />
        <ErrorNote error={error} onRetry={reload} />
        {!error && <Skeleton lines={6} />}
      </>
    );
  }

  const st: Partial<RunState> = run.state ?? {};
  const isTest = run.kind === "test";
  const results = isTest ? st.after ?? st.before ?? null : null;

  return (
    <>
      <Crumbs
        items={[
          { href: "/", label: "Projects" },
          { href: `/project?id=${encodeURIComponent(run.project_id)}`, label: project.data?.name ?? run.project_id },
          { label: run.id },
        ]}
      />
      <RunHeader run={run} st={st} mode={mode} />
      <ErrorNote error={error} onRetry={reload} />
      <Timeline run={run} st={st} />

      {st.error && (
        <div className={`note ${run.status === "rejected" ? "note-gate" : "note-fail"}`} role="status">
          <strong>{run.status === "rejected" ? "Rejected." : "Stopped."}</strong> {st.error}
        </div>
      )}

      {run.status === "awaiting_requirements" && (
        <RequirementsGate run={run} st={st} onDone={(r) => { setRun(r); void reload(); }} />
      )}
      {run.status === "awaiting_ship" && <ShipGate run={run} st={st} onDone={(r) => { setRun(r); void reload(); }} />}

      {run.status !== "awaiting_requirements" && st.requirements && (
        <details className="panel fold">
          <summary>
            <h2>Requirements</h2>
            <span className="muted small">
              {st.requirements.acceptance_criteria.length} criteria · {st.requirements.browser_tests.length} browser tests ·{" "}
              {st.requirements.db_invariants.length} DB checks · risk {st.requirements.risk}
            </span>
          </summary>
          <RequirementsBody reqs={st.requirements} fidelity={st.fidelity ?? null} />
        </details>
      )}

      {!isTest && (st.before || st.after) && <Comparison before={st.before ?? null} after={st.after ?? null} />}

      {isTest && results && <TestRunSummary res={results} />}

      {!isTest && (st.agent || (st.rounds && st.rounds.length > 0)) && <AgentPanel st={st} />}

      {isTest && results && <Evidence label="Results" res={results} idPrefix="res" />}
      {!isTest && st.before && <Evidence label="Before the change (original code)" res={st.before} idPrefix="before" />}
      {!isTest && st.after && <Evidence label="After the change (fresh copy)" res={st.after} idPrefix="after" />}

      {st.final_checks && st.final_checks.length > 0 && <FinalChecks checks={st.final_checks} />}

      {lines.length > 0 && <LiveLog lines={lines} />}

      {!st.requirements && !st.before && !st.after && !results && !TERMINAL.has(run.status) && (
        <Panel title="Working" id="working">
          <p className="muted">
            Carbon Copy is {run.status === "queued" ? "waiting for a free worker" : "building the copy"}. This page updates by itself.
          </p>
        </Panel>
      )}
    </>
  );
}

// ---------------------------------------------------------------- header

function RunHeader({ run, st, mode }: { run: Run; st: Partial<RunState>; mode: LiveMode }) {
  const [audit, setAudit] = useState<AuditResult | null>(null);
  const [auditErr, setAuditErr] = useState<unknown>(null);
  const [auditBusy, setAuditBusy] = useState(false);

  async function verify() {
    setAuditBusy(true);
    setAuditErr(null);
    try {
      setAudit(await api.audit(run.id));
    } catch (e) {
      setAuditErr(e);
    } finally {
      setAuditBusy(false);
    }
  }

  return (
    <div className="run-head">
      <div className="run-head-top">
        <StatusPill status={run.status} />
        <span className="muted small">
          {run.kind === "change" ? "Change run" : "Test run"} · {fmtTrigger(run.trigger)} · started{" "}
          <time dateTime={new Date(run.created_at * 1000).toISOString()} title={fmtTime(run.created_at)}>
            {fmtAgo(run.created_at)}
          </time>
        </span>
        <LiveBadge mode={mode} />
      </div>
      <h1 className="run-title">{run.title || st.request || run.id}</h1>
      {st.request && st.request !== run.title && <p className="lede">{st.request}</p>}
      <div className="run-links">
        {st.report && <ArtifactLink rid={run.id} which="report" label="Open proof report" />}
        {st.patch && <ArtifactLink rid={run.id} which="patch" label="View patch" />}
        <button type="button" className="btn btn-small" onClick={verify} disabled={auditBusy}>
          {auditBusy ? "Verifying…" : "Verify audit chain"}
        </button>
        {st.fidelity && (
          <span className="small muted" title="How closely the copy matches production">
            Copy fidelity <span className="mono ink">{st.fidelity.score}%</span>
          </span>
        )}
      </div>
      <ErrorNote error={auditErr} />
      {audit && (
        <div className={`note ${audit.verified ? "note-pass" : "note-fail"}`} role="status">
          <span>
            <strong>{audit.verified ? "Audit chain verified." : "Audit chain broken."}</strong>{" "}
            {audit.entries.length} entr{audit.entries.length === 1 ? "y" : "ies"}
            {audit.verified ? ", every hash links to the previous one." : ". An entry was altered or removed."}
          </span>
          <button type="button" className="btn btn-small btn-quiet" onClick={() => setAudit(null)}>
            Hide
          </button>
        </div>
      )}
    </div>
  );
}

function LiveBadge({ mode }: { mode: LiveMode }) {
  if (mode === "idle") return null;
  return (
    <span className="live small" title={mode === "sse" ? "Streaming events from the server" : "Checking for updates every few seconds"}>
      <span className="live-dot" aria-hidden="true" />
      {mode === "sse" ? "Live" : "Auto-refresh"}
    </span>
  );
}

function ArtifactLink({ rid, which, label }: { rid: string; which: "report" | "patch"; label: string }) {
  if (MOCK) {
    return (
      <button type="button" className="btn btn-small" onClick={() => void openMockArtifact(rid, which)}>
        {label}
      </button>
    );
  }
  return (
    <a className="btn btn-small" href={artifactUrl(rid, which)} target="_blank" rel="noopener noreferrer">
      {label}
    </a>
  );
}

// ---------------------------------------------------------------- timeline

function Timeline({ run, st }: { run: Run; st: Partial<RunState> }) {
  const steps = run.kind === "test" ? TEST_STEPS : CHANGE_STEPS;
  const cur = currentStepIndex(run.kind, run.status, st);
  const terminal = TERMINAL.has(run.status);
  const good = run.status === "shipped" || run.status === "passed";
  const stopped = terminal && !good;
  const navRef = useRef<HTMLElement>(null);

  // On narrow screens the timeline scrolls sideways; keep the current step in view.
  useEffect(() => {
    const nav = navRef.current;
    const li = nav?.querySelectorAll("li")[cur] as HTMLElement | undefined;
    if (nav && li && nav.scrollWidth > nav.clientWidth) {
      nav.scrollLeft = li.offsetLeft - nav.clientWidth / 2 + li.clientWidth / 2;
    }
  }, [cur]);

  return (
    <nav className="timeline" aria-label="Pipeline progress" ref={navRef}>
      <ol>
        {steps.map((s, i) => {
          let state: "done" | "current" | "todo" | "stopped" | "gate" = "todo";
          if (i < cur || (i === cur && good)) state = "done";
          else if (i === cur && stopped) state = "stopped";
          else if (i === cur && GATES.has(run.status)) state = "gate";
          else if (i === cur) state = "current";
          let label = s.label;
          if (s.key === "done" && run.kind === "test" && terminal) label = good ? "Passed" : "Failed";
          const sr = { done: "done", current: "in progress", gate: "waiting for you", stopped: "stopped here", todo: "not started" }[state];
          return (
            <li key={s.key} className={`tl tl-${state}${s.gate ? " tl-isgate" : ""}`} aria-current={i === cur ? "step" : undefined}>
              <span className="tl-mark" aria-hidden="true" />
              <span className="tl-label">
                {label}
                <span className="sr-only">: {sr}</span>
              </span>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

// ---------------------------------------------------------------- gates

function Approver({ who, setWho, id }: { who: string; setWho: (v: string) => void; id: string }) {
  return (
    <div className="field field-short">
      <label htmlFor={id}>Your name (recorded in the audit log)</label>
      <input id={id} value={who} onChange={(e) => setWho(e.target.value)} placeholder="maria.chen" autoComplete="username" />
    </div>
  );
}

function GateActions({
  run,
  approveLabel,
  onApprove,
  onDone,
}: {
  run: Run;
  approveLabel: string;
  onApprove: (who: string) => Promise<Run>;
  onDone: (r: Run) => void;
}) {
  const [who, setWho] = useWho();
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const reasonRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (rejecting) reasonRef.current?.focus();
  }, [rejecting]);

  const needWho = () => {
    if (!who.trim()) {
      setErr(new Error("Enter your name first. Approvals and rejections are signed in the audit log."));
      document.getElementById(`who-${run.id}`)?.focus();
      return true;
    }
    return false;
  };

  async function approve() {
    if (needWho()) return;
    setBusy("approve");
    setErr(null);
    try {
      onDone(await onApprove(who.trim()));
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(null);
    }
  }

  async function reject(e: React.FormEvent) {
    e.preventDefault();
    if (needWho()) return;
    setBusy("reject");
    setErr(null);
    try {
      onDone(await api.reject(run.id, who.trim(), reason.trim()));
    } catch (x) {
      setErr(x);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="gate-actions">
      <Approver who={who} setWho={setWho} id={`who-${run.id}`} />
      <ErrorNote error={err} />
      {!rejecting ? (
        <div className="form-actions">
          <button type="button" className="btn btn-primary" onClick={approve} disabled={busy !== null}>
            {busy === "approve" ? "Approving…" : approveLabel}
          </button>
          <button type="button" className="btn btn-danger" onClick={() => setRejecting(true)} disabled={busy !== null}>
            Reject
          </button>
        </div>
      ) : (
        <form className="form" onSubmit={reject}>
          <div className="field">
            <label htmlFor={`reason-${run.id}`}>Reason for rejecting (recorded in the audit log)</label>
            <textarea id={`reason-${run.id}`} ref={reasonRef} rows={2} value={reason} onChange={(e) => setReason(e.target.value)} />
          </div>
          <div className="form-actions">
            <button type="submit" className="btn btn-danger-solid" disabled={busy !== null}>
              {busy === "reject" ? "Rejecting…" : "Reject and stop this run"}
            </button>
            <button type="button" className="btn" onClick={() => setRejecting(false)} disabled={busy !== null}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

function RequirementsGate({ run, st, onDone }: { run: Run; st: Partial<RunState>; onDone: (r: Run) => void }) {
  const reqs = st.requirements;
  return (
    <Panel
      title="Approve requirements"
      id="gate-req"
      tone="gate"
      sub="Gate 1 of 2. Nothing changes until you approve. The agent will be held to exactly these criteria and tests."
    >
      {reqs ? <RequirementsBody reqs={reqs} fidelity={st.fidelity ?? null} /> : <Empty>Requirements are missing from this run.</Empty>}
      <GateActions run={run} approveLabel="Approve requirements" onApprove={(who) => api.approveRequirements(run.id, who)} onDone={onDone} />
    </Panel>
  );
}

function ShipGate({ run, st, onDone }: { run: Run; st: Partial<RunState>; onDone: (r: Run) => void }) {
  const after = st.after;
  const checks = st.final_checks ?? [];
  const failing = checks.filter((c) => !c.passed).length + (after ? after.tests.filter((t) => !t.passed).length + after.db_checks.filter((d) => !d.passed).length : 0);
  const total = checks.length + (after ? after.tests.length + after.db_checks.length : 0);
  return (
    <Panel title="Approve & ship" id="gate-ship" tone="gate" sub="Gate 2 of 2. Review the evidence below, then approve to ship this patch.">
      <dl className="facts">
        <div>
          <dt>Gate checks</dt>
          <dd>
            <ResultPill passed={failing === 0} label={failing === 0 ? `${total} of ${total} pass` : `${failing} of ${total} fail`} />
          </dd>
        </div>
        {st.agent && (
          <>
            <div>
              <dt>Files changed</dt>
              <dd className="mono">{st.agent.edited_files.length}</dd>
            </div>
            <div>
              <dt>Agent rounds</dt>
              <dd className="mono">{st.rounds?.length ?? 0}</dd>
            </div>
            <div>
              <dt>Agent cost</dt>
              <dd className="mono">{fmtUsd(st.agent.cost_usd)}</dd>
            </div>
          </>
        )}
      </dl>
      {st.patch && (
        <p className="small">
          <ArtifactLink rid={run.id} which="patch" label="Read the patch before approving" />
        </p>
      )}
      <GateActions run={run} approveLabel="Approve & ship" onApprove={(who) => api.approveShip(run.id, who)} onDone={onDone} />
    </Panel>
  );
}

function RequirementsBody({ reqs, fidelity }: { reqs: Requirements; fidelity: RunState["fidelity"] }) {
  return (
    <div className="reqs">
      <div className="reqs-head">
        <span className={`pill pill-${reqs.risk === "high" ? "fail" : reqs.risk === "medium" ? "gate" : "idle"}`}>
          <span className="pill-mark" aria-hidden="true" />
          Risk: {reqs.risk}
        </span>
        {fidelity && (
          <span className="small muted">
            Copy fidelity <span className="mono ink">{fidelity.score}%</span>
            {fidelity.missing_env.length > 0 && (
              <>
                {" "}
                · missing env <span className="mono">{fidelity.missing_env.join(", ")}</span>
              </>
            )}
            {fidelity.unemulated_aws.length > 0 && (
              <>
                {" "}
                · not emulated <span className="mono">{fidelity.unemulated_aws.join(", ")}</span>
              </>
            )}
          </span>
        )}
      </div>
      <p>{reqs.summary}</p>

      <h3>Acceptance criteria</h3>
      <ol className="criteria">
        {reqs.acceptance_criteria.map((c, i) => (
          <li key={i}>{c}</li>
        ))}
      </ol>

      <h3>AI browser tests ({reqs.browser_tests.length})</h3>
      <ul className="items">
        {reqs.browser_tests.map((b, i) => (
          <li key={i} className="item">
            <div className="item-body">
              <span className="item-title">{b.name}</span>
              <p className="item-text">{b.instructions}</p>
            </div>
          </li>
        ))}
      </ul>

      <h3>Database checks ({reqs.db_invariants.length})</h3>
      <ul className="items">
        {reqs.db_invariants.map((d, i) => (
          <li key={i} className="item">
            <div className="item-body">
              <span className="item-title">{d.description}</span>
              <code className="sql">{d.sql}</code>
              <span className="small muted">
                expect <span className="mono ink">{d.expect}</span>
              </span>
            </div>
          </li>
        ))}
      </ul>

      {reqs.infra_changes && reqs.infra_changes.length > 0 && (
        <>
          <h3>Infrastructure changes</h3>
          <ul className="bullets">
            {reqs.infra_changes.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </>
      )}
      {reqs.api_tests && reqs.api_tests.length > 0 && (
        <>
          <h3>API tests the agent must add</h3>
          <ul className="chips">
            {reqs.api_tests.map((t) => (
              <li key={t} className="chip mono">
                {t}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- before / after

interface Row {
  kind: "Screen" | "Database";
  name: string;
  expect?: string;
  before?: { passed: boolean; got?: string; error?: string | null };
  after?: { passed: boolean; got?: string; error?: string | null };
}

function rowsFor(before: TestSuiteResult | null, after: TestSuiteResult | null): Row[] {
  const rows: Row[] = [];
  const byName = new Map<string, Row>();
  const add = (side: "before" | "after", res: TestSuiteResult | null) => {
    res?.tests.forEach((t) => {
      const k = "t:" + t.name;
      let r = byName.get(k);
      if (!r) {
        r = { kind: "Screen", name: t.name };
        byName.set(k, r);
        rows.push(r);
      }
      r[side] = { passed: t.passed, error: t.steps.find((s) => !s.ok)?.error ?? null };
    });
    res?.db_checks.forEach((d) => {
      const k = "d:" + d.description;
      let r = byName.get(k);
      if (!r) {
        r = { kind: "Database", name: d.description, expect: d.expect };
        byName.set(k, r);
        rows.push(r);
      }
      r[side] = { passed: d.passed, got: d.got };
    });
  };
  add("before", before);
  add("after", after);
  return rows;
}

function Cell({ v, db }: { v?: Row["before"]; db: boolean }) {
  if (!v) return <span className="muted small">Not run</span>;
  return (
    <span className="cell-result">
      <ResultPill passed={v.passed} />
      {db && v.got !== undefined && (
        <span className="small">
          got <span className={`mono ${v.passed ? "ink" : "fail-text"}`}>{v.got || "(empty)"}</span>
        </span>
      )}
    </span>
  );
}

function Comparison({ before, after }: { before: TestSuiteResult | null; after: TestSuiteResult | null }) {
  const rows = rowsFor(before, after);
  const hidden = before
    ? before.db_checks.filter((d) => !d.passed).length > 0 && before.tests.some((t) => t.passed)
    : false;
  const firstHidden = before?.db_checks.find((d) => !d.passed);
  return (
    <Panel
      title="Before vs after"
      id="compare"
      sub="Same AI browser tests and database checks, run on the original code and again on the changed code."
    >
      {hidden && firstHidden && (
        <div className="note note-gate">
          <span>
            <strong>The screen alone would have missed this.</strong> Before the change, a browser test passed while the database check
            &ldquo;{firstHidden.description}&rdquo; expected <span className="mono">{firstHidden.expect}</span> and got{" "}
            <span className="mono">{firstHidden.got}</span>.
          </span>
        </div>
      )}
      <div className="table-wrap">
        <table className="table stack compare">
          <thead>
            <tr>
              <th scope="col">Check</th>
              <th scope="col">Layer</th>
              <th scope="col">Expected</th>
              <th scope="col">Before</th>
              <th scope="col">After</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <th scope="row" data-label="Check" className="cell-main">
                  {r.name}
                </th>
                <td data-label="Layer">{r.kind}</td>
                <td data-label="Expected">{r.expect !== undefined ? <span className="mono">{r.expect}</span> : <span className="muted small">test passes</span>}</td>
                <td data-label="Before">
                  <Cell v={r.before} db={r.kind === "Database"} />
                </td>
                <td data-label="After">
                  <Cell v={r.after} db={r.kind === "Database"} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function TestRunSummary({ res }: { res: TestSuiteResult }) {
  const tf = res.tests.filter((t) => !t.passed).length;
  const df = res.db_checks.filter((d) => !d.passed).length;
  return (
    <Panel title="Summary" id="summary">
      <dl className="facts">
        <div>
          <dt>Browser tests</dt>
          <dd>
            <ResultPill passed={tf === 0} label={tf === 0 ? `${res.tests.length} pass` : `${tf} of ${res.tests.length} fail`} />
          </dd>
        </div>
        <div>
          <dt>Database checks</dt>
          <dd>
            <ResultPill passed={df === 0} label={df === 0 ? `${res.db_checks.length} pass` : `${df} of ${res.db_checks.length} fail`} />
          </dd>
        </div>
      </dl>
      {res.db_checks.length > 0 && (
        <div className="table-wrap">
          <table className="table stack">
            <thead>
              <tr>
                <th scope="col">Database check</th>
                <th scope="col">Expected</th>
                <th scope="col">Got</th>
                <th scope="col">Result</th>
              </tr>
            </thead>
            <tbody>
              {res.db_checks.map((d, i) => (
                <tr key={i}>
                  <th scope="row" data-label="Check" className="cell-main">
                    {d.description}
                    <code className="sql">{d.sql}</code>
                  </th>
                  <td data-label="Expected" className="mono">
                    {d.expect}
                  </td>
                  <td data-label="Got" className={`mono ${d.passed ? "" : "fail-text"}`}>
                    {d.got || "(empty)"}
                  </td>
                  <td data-label="Result">
                    <ResultPill passed={d.passed} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------- agent

function AgentPanel({ st }: { st: Partial<RunState> }) {
  const a = st.agent;
  const rounds = st.rounds ?? [];
  return (
    <Panel title="Coding agent" id="agent" sub="The agent edits code with no shell. Each round it can only run the gate checks on the copy.">
      {a && (
        <>
          <p>{a.summary}</p>
          <dl className="facts">
            <div>
              <dt>Cost</dt>
              <dd className="mono">{fmtUsd(a.cost_usd)}</dd>
            </div>
            <div>
              <dt>Rounds</dt>
              <dd className="mono">{rounds.length}</dd>
            </div>
            <div className="facts-wide">
              <dt>Edited files</dt>
              <dd>
                <ul className="chips">
                  {a.edited_files.map((f) => (
                    <li key={f} className="chip mono">
                      {f}
                    </li>
                  ))}
                </ul>
              </dd>
            </div>
          </dl>
        </>
      )}
      {rounds.length > 0 && (
        <ol className="rounds">
          {rounds.map((r) => (
            <RoundRow key={r.round} r={r} />
          ))}
        </ol>
      )}
    </Panel>
  );
}

function RoundRow({ r }: { r: Round }) {
  return (
    <li className="round">
      <div className="round-head">
        <span className="round-n">Round {r.round}</span>
        <ResultPill passed={r.passed} label={r.passed ? "All checks pass" : `${r.checks.filter((c) => !c.passed).length} failing`} />
      </div>
      <ul className="checklist">
        {r.checks.map((c, i) => (
          <CheckLine key={i} c={c} />
        ))}
      </ul>
    </li>
  );
}

function CheckLine({ c }: { c: CheckResult }) {
  const hasDetails = c.details && c.details.length > 0;
  const inner = (
    <>
      <span className={`mark ${c.passed ? "mark-pass" : "mark-fail"}`} aria-label={c.passed ? "Pass" : "Fail"}>
        {c.passed ? "PASS" : "FAIL"}
      </span>
      <span className="check-name">{c.name}</span>
      <span className="muted small check-sum">{c.summary}</span>
    </>
  );
  if (!hasDetails) return <li className="check">{inner}</li>;
  return (
    <li className="check">
      <details>
        <summary>{inner}</summary>
        <pre className="details-pre">{c.details!.join("\n")}</pre>
      </details>
    </li>
  );
}

function FinalChecks({ checks }: { checks: CheckResult[] }) {
  const failing = checks.filter((c) => !c.passed).length;
  return (
    <Panel
      title="Final gate checks"
      id="final"
      tone={failing ? "fail" : undefined}
      sub="Run on a fresh copy after the agent finished: tests, infrastructure policy, IaC scan, secret scan."
    >
      <ul className="checklist">
        {checks.map((c, i) => (
          <CheckLine key={i} c={c} />
        ))}
      </ul>
    </Panel>
  );
}

// ---------------------------------------------------------------- evidence

function Evidence({ label, res, idPrefix }: { label: string; res: TestSuiteResult; idPrefix: string }) {
  const [shot, setShot] = useState<{ src: string; caption: string } | null>(null);
  const dlg = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    if (shot) dlg.current?.showModal();
  }, [shot]);

  return (
    <Panel title={label} id={`ev-${idPrefix}`} sub="Every step the AI tester took, with the screen it saw.">
      {res.tests.length === 0 && <Empty>No browser tests in this run.</Empty>}
      <div className="tests">
        {res.tests.map((t, i) => (
          <TestEvidence key={i} t={t} onOpen={setShot} />
        ))}
      </div>
      <dialog
        ref={dlg}
        className="lightbox"
        onClose={() => setShot(null)}
        onClick={(e) => {
          if (e.target === dlg.current) dlg.current?.close();
        }}
        aria-label="Screenshot"
      >
        {shot && (
          <figure>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={shot.src} alt={shot.caption} />
            <figcaption>
              <span>{shot.caption}</span>
              <button type="button" className="btn btn-small" onClick={() => dlg.current?.close()}>
                Close
              </button>
            </figcaption>
          </figure>
        )}
      </dialog>
    </Panel>
  );
}

function stepText(s: Step): string {
  const tgt = s.target ? ` "${s.target}"` : "";
  const val = s.value ? (s.action === "fill" ? ` ← ${s.value}` : ` "${s.value}"`) : "";
  return `${s.action}${tgt}${val}`;
}

function TestEvidence({ t, onOpen }: { t: BrowserTestResult; onOpen: (s: { src: string; caption: string }) => void }) {
  const failed = t.steps.find((s) => !s.ok);
  return (
    <article className={`test ${t.passed ? "" : "test-fail"}`}>
      <header className="test-head">
        <h3>{t.name}</h3>
        <ResultPill passed={t.passed} />
      </header>
      <p className="muted small test-instr">{t.instructions}</p>
      {failed?.error && (
        <p className="step-error" role="note">
          <strong>Step {t.steps.indexOf(failed) + 1} failed:</strong> <span className="mono">{failed.error}</span>
        </p>
      )}
      <ol className="strip" aria-label={`Steps for ${t.name}`}>
        {t.steps.map((s, i) => (
          <li key={i} className={`step ${s.ok ? "" : "step-bad"}`}>
            {s.screenshot ? (
              <button
                type="button"
                className="thumb"
                onClick={() => onOpen({ src: imgSrc(s.screenshot!), caption: `${t.name} · step ${i + 1}: ${stepText(s)}` })}
                aria-label={`Enlarge screenshot for step ${i + 1}`}
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={imgSrc(s.screenshot)} alt="" loading="lazy" />
              </button>
            ) : (
              <div className="thumb thumb-empty">No screenshot</div>
            )}
            <div className="step-meta">
              <span className="step-top">
                <span className="step-n">{i + 1}</span>
                <span className={`kind kind-${s.kind.toLowerCase()}`}>{s.kind}</span>
                <span className={`mark ${s.ok ? "mark-pass" : "mark-fail"}`}>{s.ok ? "OK" : "FAIL"}</span>
                <span className="muted xsmall mono">{fmtMs(s.ms)}</span>
              </span>
              <span className="mono small step-act">{stepText(s)}</span>
              {s.why && <span className="muted xsmall">{s.why}</span>}
            </div>
          </li>
        ))}
      </ol>
    </article>
  );
}

// ---------------------------------------------------------------- live log

function LiveLog({ lines }: { lines: LiveLine[] }) {
  const ref = useRef<HTMLOListElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);
  return (
    <Panel title="Live events" id="log">
      <ol className="log" ref={ref} aria-live="polite">
        {lines.map((l, i) => (
          <li key={i} className={l.tone ? `${l.tone}-text` : ""}>
            <span className="muted">{new Date(l.t).toLocaleTimeString()}</span> {l.text}
          </li>
        ))}
      </ol>
    </Panel>
  );
}
