"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, MOCK } from "@/lib/api";

export function Shell({ children }: { children: React.ReactNode }) {
  const [health, setHealth] = useState<{ ok: boolean; ai?: string } | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .health()
      .then((h) => alive && setHealth({ ok: !!h.ok, ai: h.ai }))
      .catch(() => alive && setHealth({ ok: false }));
    return () => {
      alive = false;
    };
  }, []);

  return (
    <>
      <a className="skip" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <div className="topbar-inner">
          <Link href="/" className="brand" aria-label="Carbon Copy, all projects">
            <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
              <rect x="1.5" y="1.5" width="11" height="11" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
              <rect x="7.5" y="7.5" width="11" height="11" rx="1.5" fill="var(--accent)" />
            </svg>
            <span>Carbon Copy</span>
          </Link>
          <div className="topbar-meta">
            {MOCK && <span className="tag tag-gate">Mock data</span>}
            {health === null ? (
              <span className="muted small">Checking API…</span>
            ) : health.ok ? (
              <span className="small conn" title="API reachable">
                <span className="dot dot-pass" aria-hidden="true" />
                <span className="conn-text">API · AI {health.ai === "claude" ? "Claude" : health.ai ?? "?"}</span>
              </span>
            ) : (
              <span className="small conn" title="API unreachable">
                <span className="dot dot-fail" aria-hidden="true" />
                <span className="conn-text">API unreachable</span>
              </span>
            )}
          </div>
        </div>
      </header>
      <main id="main" className="page">
        {children}
      </main>
    </>
  );
}
