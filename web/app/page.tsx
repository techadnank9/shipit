"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { fmtAgo, fmtTrigger } from "@/lib/format";
import { Empty, ErrorNote, Panel, Skeleton, StatusPill, useLoad } from "@/components/ui";

export default function ProjectsPage() {
  const { data, error, reload } = useLoad(() => api.listProjects(), []);
  const [showForm, setShowForm] = useState(false);
  const open = showForm || (data !== null && data.length === 0);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Projects</h1>
          <p className="lede">Each project is a repo Carbon Copy can copy: app, database and fake AWS.</p>
        </div>
        {!open && (
          <button type="button" className="btn btn-primary" onClick={() => setShowForm(true)}>
            New project
          </button>
        )}
      </div>

      {open && <CreateProject onCancel={data && data.length ? () => setShowForm(false) : undefined} />}

      <ErrorNote error={error} onRetry={reload} />
      {!data && !error && <Skeleton lines={4} />}

      {data && data.length > 0 && (
        <ul className="project-list">
          {data.map((p) => (
            <li key={p.id}>
              <Link href={`/project?id=${encodeURIComponent(p.id)}`} className="project-row">
                <div className="project-main">
                  <span className="project-name">{p.name}</span>
                  <span className="mono muted small ellipsis">{p.repo_path ?? p.git_url ?? "—"}</span>
                </div>
                <div className="project-last">
                  {p.last_run ? (
                    <>
                      <StatusPill status={p.last_run.status} />
                      <span className="muted small">
                        {fmtTrigger(p.last_run.trigger)} · {fmtAgo(p.last_run.created_at)}
                      </span>
                    </>
                  ) : (
                    <span className="muted small">No runs yet</span>
                  )}
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
      {data && data.length === 0 && <Empty>No projects yet. Add a repo above to create the first copy.</Empty>}
    </>
  );
}

function CreateProject({ onCancel }: { onCancel?: () => void }) {
  const router = useRouter();
  const [name, setName] = useState("");
  const [source, setSource] = useState<"path" | "git">("path");
  const [loc, setLoc] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const n = name.trim();
    const l = loc.trim();
    if (!n || !l) {
      setErr(new Error("Name and repo location are both required."));
      return;
    }
    if (source === "git" && !/^(https?:\/\/|git@|ssh:\/\/)/.test(l)) {
      setErr(new Error("Git URL should start with https://, ssh:// or git@."));
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const p = await api.createProject(source === "path" ? { name: n, repo_path: l } : { name: n, git_url: l });
      router.push(`/project?id=${encodeURIComponent(p.id)}`);
    } catch (x) {
      setErr(x);
      setBusy(false);
    }
  }

  return (
    <Panel title="New project" id="new-project">
      <form className="form" onSubmit={submit} noValidate>
        <div className="field">
          <label htmlFor="np-name">Name</label>
          <input id="np-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Refunds desk" autoComplete="off" required />
        </div>
        <fieldset className="field">
          <legend>Repo source</legend>
          <div className="seg" role="radiogroup">
            <label className={source === "path" ? "on" : ""}>
              <input type="radio" name="np-src" checked={source === "path"} onChange={() => setSource("path")} />
              Local path
            </label>
            <label className={source === "git" ? "on" : ""}>
              <input type="radio" name="np-src" checked={source === "git"} onChange={() => setSource("git")} />
              Git URL
            </label>
          </div>
        </fieldset>
        <div className="field">
          <label htmlFor="np-loc">{source === "path" ? "Path on the Carbon Copy server" : "Git clone URL"}</label>
          <input
            id="np-loc"
            className="mono"
            value={loc}
            onChange={(e) => setLoc(e.target.value)}
            placeholder={source === "path" ? "/srv/repos/refunds-desk" : "https://github.com/acme/refunds-desk.git"}
            autoComplete="off"
            spellCheck={false}
            required
          />
        </div>
        <ErrorNote error={err} />
        <div className="form-actions">
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? "Creating…" : "Create project"}
          </button>
          {onCancel && (
            <button type="button" className="btn" onClick={onCancel}>
              Cancel
            </button>
          )}
        </div>
      </form>
    </Panel>
  );
}
