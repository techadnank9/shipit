"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { describeCron, fmtAgo, fmtTime, fmtTrigger, isValidCron, GATES, TERMINAL } from "@/lib/format";
import type { DbCheck, Run, SavedTest, SystemMap } from "@/lib/types";
import { Crumbs, Empty, ErrorNote, Panel, Skeleton, StatusPill, useLoad } from "./ui";

export default function ProjectView() {
  const id = useSearchParams().get("id") ?? "";
  const router = useRouter();
  const project = useLoad(() => (id ? api.getProject(id) : Promise.reject(new Error("No project id in the URL."))), [id]);
  const runs = useLoad(() => api.listRuns({ project_id: id, limit: 50 }), [id]);
  const [starting, setStarting] = useState(false);
  const [runErr, setRunErr] = useState<unknown>(null);

  // Refresh the runs table while anything is in flight.
  const active = runs.data?.some((r) => !TERMINAL.has(r.status) && !GATES.has(r.status)) ?? false;
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => void runs.reload(), 5000);
    return () => clearInterval(t);
  }, [active, runs.reload]);

  async function runTests() {
    setStarting(true);
    setRunErr(null);
    try {
      const r = await api.startTestRun(id, "manual");
      router.push(`/run?id=${encodeURIComponent(r.id)}`);
    } catch (e) {
      setRunErr(e);
      setStarting(false);
    }
  }

  const p = project.data;
  return (
    <>
      <Crumbs items={[{ href: "/", label: "Projects" }, { label: p?.name ?? id }]} />
      <div className="page-head">
        <div className="min0">
          <h1>{p?.name ?? (project.error ? "Project" : "Loading…")}</h1>
          {p && <p className="mono muted small ellipsis">{p.repo_path ?? p.git_url}</p>}
        </div>
        {p && (
          <button type="button" className="btn btn-primary" onClick={runTests} disabled={starting}>
            {starting ? "Starting…" : "Run tests"}
          </button>
        )}
      </div>
      <ErrorNote error={project.error} onRetry={project.reload} />
      <ErrorNote error={runErr} />

      {p && (
        <div className="project-grid">
          <div className="col-main">
            <div className="area-change">
              <RequestChange pid={id} />
            </div>
            <div className="area-runs">
              <Panel title="Runs" id="runs" sub={active ? "Refreshing while runs are in progress" : undefined}>
                <ErrorNote error={runs.error} onRetry={runs.reload} />
                {!runs.data && !runs.error && <Skeleton />}
                {runs.data && runs.data.length === 0 && <Empty>No runs yet. Run tests or request a change.</Empty>}
                {runs.data && runs.data.length > 0 && <RunsTable runs={runs.data} />}
              </Panel>
            </div>
            <div className="pair">
              <div className="area-tests">
                <TestsPanel pid={id} />
              </div>
              <div className="area-checks">
                <ChecksPanel pid={id} />
              </div>
            </div>
          </div>
          <div className="col-side">
            <OnboardingCard pid={id} />
            <div className="area-map">
              <MapSummary pid={id} />
            </div>
            <div className="area-schedule">
              <ScheduleEditor pid={id} />
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function OnboardingCard({ pid }: { pid: string }) {
  const { data } = useLoad(() => api.onboarding(pid), [pid]);
  if (!data) return null;
  const st = data.onboarding.status;
  const approved = !!data.profile;
  const t = data.onboarding.sweep;
  const label = approved ? "Approved" : st === "ready" ? "Waiting for approval" : st === "not_started" ? "Not onboarded" : st === "failed" ? "Failed" : "In progress";
  const tone = approved ? "pass" : st === "failed" ? "fail" : st === "ready" || st === "not_started" ? "gate" : "run";
  return (
    <Panel
      title="Onboarding"
      id="onboarding"
      tone={approved ? undefined : "gate"}
      actions={
        <Link className="btn btn-small" href={`/onboard?id=${encodeURIComponent(pid)}`}>
          {approved ? "View" : st === "not_started" ? "Start" : "Open"}
        </Link>
      }
    >
      <p className="row-gap small">
        <span className={`pill pill-${tone}`}>
          <span className="pill-mark" aria-hidden="true" />
          {label}
        </span>
        {data.approved_by && <span className="muted">by {data.approved_by.who}</span>}
      </p>
      {t && (
        <p className="small muted">
          Last page sweep: {t.pages} pages, {t.issues.high} high issues, {t.tests_passed}/{t.tests} tests passed.
        </p>
      )}
      {!approved && <p className="small muted">Onboard once so every copy of this system boots the same way.</p>}
    </Panel>
  );
}

function RequestChange({ pid }: { pid: string }) {
  const router = useRouter();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (text.trim().length < 8) {
      setErr(new Error("Describe the change in a sentence or two."));
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const r = await api.requestChange(pid, text.trim());
      router.push(`/run?id=${encodeURIComponent(r.id)}`);
    } catch (x) {
      setErr(x);
      setBusy(false);
    }
  }

  return (
    <Panel
      title="Request a change"
      id="change"
      sub="Carbon Copy writes requirements for your approval, changes the code on a copy, and waits for your ship approval."
    >
      <form className="form" onSubmit={submit}>
        <div className="field">
          <label htmlFor="cr-text" className="sr-only">
            Change request
          </label>
          <textarea
            id="cr-text"
            rows={3}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Make refunds safe to retry and cap them at $500"
          />
        </div>
        <ErrorNote error={err} />
        <div className="form-actions">
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? "Starting…" : "Start change run"}
          </button>
        </div>
      </form>
    </Panel>
  );
}

function RunsTable({ runs }: { runs: Run[] }) {
  return (
    <div className="table-wrap">
      <table className="table stack runs-table">
        <thead>
          <tr>
            <th scope="col">Status</th>
            <th scope="col">Run</th>
            <th scope="col">Kind</th>
            <th scope="col">Trigger</th>
            <th scope="col">Started</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id}>
              <td data-label="Status">
                <StatusPill status={r.status} />
              </td>
              <td data-label="Run" className="cell-main">
                <Link href={`/run?id=${encodeURIComponent(r.id)}`} className="run-link">
                  {r.title || r.id}
                </Link>
                <span className="mono muted xsmall">{r.id}</span>
              </td>
              <td data-label="Kind">{r.kind === "change" ? "Change" : "Test"}</td>
              <td data-label="Trigger" className="nowrap">{fmtTrigger(r.trigger)}</td>
              <td data-label="Started" className="nowrap">
                <time dateTime={new Date(r.created_at * 1000).toISOString()} title={fmtTime(r.created_at)}>
                  {fmtAgo(r.created_at)}
                </time>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MapSummary({ pid }: { pid: string }) {
  const { data, error, reload } = useLoad<SystemMap>(() => api.getMap(pid), [pid]);
  return (
    <Panel title="System map" id="map" sub={data?.repo ? <span className="mono">{data.repo}</span> : undefined}>
      <ErrorNote error={error} onRetry={reload} />
      {!data && !error && <Skeleton />}
      {data && (
        <div className="map">
          <div className="stats">
            <Stat n={data.routes?.length ?? 0} label="routes" />
            <Stat n={data.tables?.length ?? 0} label="tables" />
            <Stat n={data.aws_services?.length ?? 0} label="AWS services" />
            <Stat n={data.env_vars?.length ?? 0} label="env vars" />
          </div>
          {data.routes && data.routes.length > 0 && (
            <>
              <h3>Routes</h3>
              <ul className="routes">
                {data.routes.map((r, i) => (
                  <li key={i}>
                    <span className={`method m-${r.method.toLowerCase()}`}>{r.method}</span>
                    <span className="mono">{r.path}</span>
                    <span className="mono muted xsmall ellipsis">{r.handler}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
          <h3>Tables</h3>
          <Chips items={data.tables} empty="None found" />
          <h3>AWS services (fake AWS in the copy)</h3>
          <Chips items={data.aws_services} empty="None found" />
          {data.infrastructure && data.infrastructure.length > 0 && (
            <p className="muted small">
              {data.infrastructure.length} Terraform resource{data.infrastructure.length === 1 ? "" : "s"}:{" "}
              <span className="mono">{data.infrastructure.map((r) => `${r.type}.${r.name}`).join(", ")}</span>
            </p>
          )}
        </div>
      )}
    </Panel>
  );
}

function Stat({ n, label }: { n: number; label: string }) {
  return (
    <div className="stat">
      <span className="stat-n">{n}</span>
      <span className="stat-l">{label}</span>
    </div>
  );
}

function Chips({ items, empty }: { items?: string[]; empty: string }) {
  if (!items || !items.length) return <p className="muted small">{empty}</p>;
  return (
    <ul className="chips">
      {items.map((t) => (
        <li key={t} className="chip mono">
          {t}
        </li>
      ))}
    </ul>
  );
}

const PRESETS: { value: string; label: string; cron: string | null }[] = [
  { value: "manual", label: "Manual only", cron: null },
  { value: "hourly", label: "Every hour", cron: "0 * * * *" },
  { value: "6h", label: "Every 6 hours", cron: "0 */6 * * *" },
  { value: "daily8", label: "Daily at 8:00", cron: "0 8 * * *" },
  { value: "custom", label: "Custom cron", cron: "" },
];

function presetFor(cron: string | null): string {
  const p = PRESETS.find((x) => x.cron === cron);
  return p ? p.value : "custom";
}

function ScheduleEditor({ pid }: { pid: string }) {
  const { data, error, reload, setData } = useLoad(() => api.getSchedule(pid), [pid]);
  const [choice, setChoice] = useState("manual");
  const [custom, setCustom] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!data) return;
    const c = presetFor(data.cron);
    setChoice(c);
    if (c === "custom") setCustom(data.cron ?? "");
  }, [data]);

  const target = choice === "custom" ? custom.trim() : PRESETS.find((p) => p.value === choice)!.cron;
  const dirty = data ? (target || null) !== (data.cron || null) : false;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (choice === "custom" && !isValidCron(custom)) {
      setErr(new Error("Custom cron needs 5 fields, for example 30 7 * * 1-5."));
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const s = await api.putSchedule(pid, target || null);
      setData(s);
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    } catch (x) {
      setErr(x);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="Schedule" id="schedule" sub="Runs all saved tests and DB checks on a fresh copy.">
      <ErrorNote error={error} onRetry={reload} />
      {!data && !error && <Skeleton lines={2} />}
      {data && (
        <form className="form" onSubmit={save}>
          <fieldset className="field">
            <legend className="sr-only">Test schedule</legend>
            <div className="radio-list">
              {PRESETS.map((p) => (
                <label key={p.value} className="radio">
                  <input type="radio" name="sched" value={p.value} checked={choice === p.value} onChange={() => setChoice(p.value)} />
                  <span>{p.label}</span>
                  {p.cron && <span className="mono muted xsmall">{p.cron}</span>}
                </label>
              ))}
            </div>
          </fieldset>
          {choice === "custom" && (
            <div className="field">
              <label htmlFor="sched-cron">Cron expression (min hour day month weekday)</label>
              <input
                id="sched-cron"
                className="mono"
                value={custom}
                onChange={(e) => setCustom(e.target.value)}
                placeholder="30 7 * * 1-5"
                spellCheck={false}
                autoComplete="off"
              />
            </div>
          )}
          <p className="muted small">
            Current: <strong>{describeCron(data.cron)}</strong>
            {data.next_run_at ? <> · next run {fmtTime(data.next_run_at)}</> : null}
          </p>
          <ErrorNote error={err} />
          <div className="form-actions">
            <button type="submit" className="btn" disabled={busy || !dirty}>
              {busy ? "Saving…" : "Save schedule"}
            </button>
            <span className="small pass-text" role="status">
              {saved ? "Saved" : ""}
            </span>
          </div>
        </form>
      )}
    </Panel>
  );
}

function TestsPanel({ pid }: { pid: string }) {
  const { data, error, reload, setData } = useLoad<SavedTest[]>(() => api.listTests(pid), [pid]);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [instr, setInstr] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim() || !instr.trim()) {
      setErr(new Error("Give the test a name and plain-English instructions."));
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const t = await api.addTest(pid, { name: name.trim(), instructions: instr.trim() });
      setData([...(data ?? []), t]);
      setName("");
      setInstr("");
      setAdding(false);
    } catch (x) {
      setErr(x);
    } finally {
      setBusy(false);
    }
  }

  async function remove(t: SavedTest) {
    if (!window.confirm(`Delete the test "${t.name}"? Scheduled runs will stop running it.`)) return;
    setDeleting(t.id);
    try {
      await api.deleteTest(t.id);
      setData((data ?? []).filter((x) => x.id !== t.id));
    } catch (x) {
      setErr(x);
    } finally {
      setDeleting(null);
    }
  }

  return (
    <Panel
      title="Browser tests"
      id="tests"
      sub="Plain-English instructions. An AI tester follows them in a real browser on the copy."
      actions={
        !adding && (
          <button type="button" className="btn btn-small" onClick={() => setAdding(true)}>
            Add test
          </button>
        )
      }
    >
      <ErrorNote error={error} onRetry={reload} />
      {!data && !error && <Skeleton />}
      {data && data.length === 0 && !adding && <Empty>No saved tests. Add one to include it in every test run.</Empty>}
      {data && data.length > 0 && (
        <ul className="items">
          {data.map((t) => (
            <li key={t.id} className="item">
              <div className="item-body">
                <span className="item-title">{t.name}</span>
                <p className="item-text">{t.instructions}</p>
              </div>
              <button
                type="button"
                className="btn btn-small btn-quiet"
                onClick={() => remove(t)}
                disabled={deleting === t.id}
                aria-label={`Delete test ${t.name}`}
              >
                {deleting === t.id ? "Deleting…" : "Delete"}
              </button>
            </li>
          ))}
        </ul>
      )}
      {adding && (
        <form className="form add-form" onSubmit={add}>
          <div className="field">
            <label htmlFor="t-name">Test name</label>
            <input id="t-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Issue a $20 refund" autoComplete="off" />
          </div>
          <div className="field">
            <label htmlFor="t-instr">Instructions</label>
            <textarea
              id="t-instr"
              rows={3}
              value={instr}
              onChange={(e) => setInstr(e.target.value)}
              placeholder={'Type 20 in the Amount field for order #1, click Refund, expect "Refund issued".'}
            />
          </div>
          <ErrorNote error={err} />
          <div className="form-actions">
            <button type="submit" className="btn btn-primary" disabled={busy}>
              {busy ? "Saving…" : "Save test"}
            </button>
            <button type="button" className="btn" onClick={() => { setAdding(false); setErr(null); }}>
              Cancel
            </button>
          </div>
        </form>
      )}
      {!adding && <ErrorNote error={err} />}
    </Panel>
  );
}

function ChecksPanel({ pid }: { pid: string }) {
  const { data, error, reload, setData } = useLoad<DbCheck[]>(() => api.listChecks(pid), [pid]);
  const [adding, setAdding] = useState(false);
  const [desc, setDesc] = useState("");
  const [sql, setSql] = useState("");
  const [expect, setExpect] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!desc.trim() || !sql.trim() || !expect.trim()) {
      setErr(new Error("Description, SQL and expected value are all required."));
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const c = await api.addCheck(pid, { description: desc.trim(), sql: sql.trim(), expect: expect.trim() });
      setData([...(data ?? []), c]);
      setDesc("");
      setSql("");
      setExpect("");
      setAdding(false);
    } catch (x) {
      setErr(x);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      title="Database checks"
      id="checks"
      sub="SQL that returns one value, compared after the browser tests run. Catches what the screen hides."
      actions={
        !adding && (
          <button type="button" className="btn btn-small" onClick={() => setAdding(true)}>
            Add check
          </button>
        )
      }
    >
      <ErrorNote error={error} onRetry={reload} />
      {!data && !error && <Skeleton />}
      {data && data.length === 0 && !adding && <Empty>No database checks yet.</Empty>}
      {data && data.length > 0 && (
        <ul className="items">
          {data.map((c) => (
            <li key={c.id} className="item">
              <div className="item-body">
                <span className="item-title">{c.description}</span>
                <code className="sql">{c.sql}</code>
                <span className="small muted">
                  expect <span className="mono ink">{c.expect}</span>
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
      {adding && (
        <form className="form add-form" onSubmit={add}>
          <div className="field">
            <label htmlFor="c-desc">Description</label>
            <input id="c-desc" value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="Exactly one refund row for order 2" autoComplete="off" />
          </div>
          <div className="field">
            <label htmlFor="c-sql">SQL (returns one value)</label>
            <textarea
              id="c-sql"
              className="mono"
              rows={2}
              value={sql}
              onChange={(e) => setSql(e.target.value)}
              placeholder="SELECT count(*) FROM refunds WHERE order_id = 2"
              spellCheck={false}
            />
          </div>
          <div className="field field-short">
            <label htmlFor="c-exp">Expected value</label>
            <input id="c-exp" className="mono" value={expect} onChange={(e) => setExpect(e.target.value)} placeholder="1" autoComplete="off" />
          </div>
          <ErrorNote error={err} />
          <div className="form-actions">
            <button type="submit" className="btn btn-primary" disabled={busy}>
              {busy ? "Saving…" : "Save check"}
            </button>
            <button type="button" className="btn" onClick={() => { setAdding(false); setErr(null); }}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </Panel>
  );
}

